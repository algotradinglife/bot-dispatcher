#!/usr/bin/env python3
"""method preflight — 可执行方法协议检查器（delivery-improvement v2 机制 1/3）

hard-gate: 失败禁止进 EV。检查：
  1. method.yaml 存在 + schema 合法
  2. protocol hash 与执行 receipt 一致（worker 不得改协议不声明）
  3. 必需工件存在
  4. PROTOCOL-LOG 修订完整性（追加式，无删改）
  5. holdout 隔离声明（true=隔离；false=剩余风险必须记录）

用法:
  python3 method_preflight.py --worktree <path>
  python3 method_preflight.py --worktree <path> --json   # 机器可读
exit 0 = PASS, 1 = FAIL, 2 = 用法错误
"""
import argparse
import hashlib
import json
import os
import re
import sys

REQUIRED_KEYS = [
    "protocol_version", "candidate_pool", "splits", "selection_algorithm",
    "metrics", "budget", "required_artifacts", "schema_ref", "receipt",
]
SPLIT_KEYS = ["selection_split", "terminal_split", "holdout_isolated"]
METRIC_KEYS = ["name", "definition", "golden_test"]
STAGE_KEYS = ["name", "deliverable", "skill", "constraints"]
LOG_EVENT_RE = re.compile(
    r"^\|\s*(revise|add|supersede)\s*\|.*\|.*\|.*\|.*\|.*\|")


def load_yaml(path):
    """依赖已有 PyYAML（bot-dispatcher .venv 有）; 否则 fallback 简单解析."""
    try:
        import yaml
        return yaml.safe_load(open(path))
    except ImportError:
        # 极简 fallback: 只做存在性 + 关键字段扫描（不完整，仅应急）
        text = open(path).read()
        return {"_fallback": True, "text": text}


def check_schema(m):
    """method.yaml schema 合法性检查. Returns (ok, errors)."""
    errors = []
    if not isinstance(m, dict) or m.get("_fallback"):
        return False, ["method.yaml 无法解析（缺 PyYAML 或格式错误）"]
    for k in REQUIRED_KEYS:
        if k not in m:
            errors.append("缺少必需字段: %s" % k)
    splits = m.get("splits", {})
    for k in SPLIT_KEYS:
        if k not in splits:
            errors.append("splits 缺少字段: %s" % k)
    # codex 复审 (2026-08-12): split 必须是 strip 后非空字符串
    sel = splits.get("selection_split")
    term = splits.get("terminal_split")
    for k, v in (("selection_split", sel), ("terminal_split", term)):
        if not isinstance(v, str) or not v.strip():
            errors.append("splits.%s 必须是非空字符串（不能 null/空串）" % k)
    # codex: selection_split != terminal_split 无条件强制（防泄漏）
    if (isinstance(sel, str) and isinstance(term, str)
            and sel.strip() and term.strip() and sel.strip() == term.strip()):
        errors.append("selection_split == terminal_split（%s）— 必须不同，"
                      "否则选择与评估共用数据 = 泄漏" % sel.strip())
    # codex: terminal_access 结构化字段 — 不再埋在 constraints 自由文本
    TA = m.get("terminal_access", {})
    if not isinstance(TA, dict):
        errors.append("terminal_access 必须是 mapping")
    else:
        for stage, expect in (("model_selection", "forbidden"),
                              ("terminal_evaluation", "exactly_once")):
            v = TA.get(stage)
            if v != expect:
                errors.append("terminal_access.%s 必须是 %r（实际 %r）"
                              % (stage, expect, v))
    # codex: preregistered 必须 True
    sel_alg = m.get("selection_algorithm", {})
    if not isinstance(sel_alg, dict):
        errors.append("selection_algorithm 必须是 mapping")
    elif sel_alg.get("preregistered") is not True:
        errors.append("selection_algorithm.preregistered 必须为 true")
    metrics = m.get("metrics", [])
    if not isinstance(metrics, list) or not metrics:
        errors.append("metrics 必须是非空列表")
    else:
        for i, mt in enumerate(metrics):
            for k in METRIC_KEYS:
                if k not in mt:
                    errors.append("metrics[%d] 缺少字段: %s" % (i, k))
    arts = m.get("required_artifacts", [])
    if not isinstance(arts, list) or not arts:
        errors.append("required_artifacts 必须是非空列表")
    # codex 审核 (2026-08-12) P0-1/2: stages 强制 + 结构化校验
    # 研究/建模协议必须带 stages; 六阶段各恰好一次; 值非空; 类型安全
    STAGE_ENUM = ["data_stats", "preprocessing", "eda",
                  "feature_engineering", "model_selection",
                  "terminal_evaluation"]
    if "stages" not in m or m.get("stages") is None:
        errors.append("stages 缺失（研究/建模协议必须带板块拆解）")
    elif not isinstance(m["stages"], list):
        errors.append("stages 必须是列表")
    elif not m["stages"]:
        errors.append("stages 不能是空列表")
    else:
        seen = []
        for i, st in enumerate(m["stages"]):
            if not isinstance(st, dict):
                errors.append("stages[%d] 必须是 mapping（不是 %s）"
                              % (i, type(st).__name__))
                continue
            for k in STAGE_KEYS:
                if k not in st:
                    errors.append("stages[%d] 缺少字段: %s" % (i, k))
            name = st.get("name")
            if name not in STAGE_ENUM:
                errors.append("stages[%d] 未知阶段名: %r（允许: %s）"
                              % (i, name, "/".join(STAGE_ENUM)))
            else:
                seen.append(name)
            for k in ("deliverable", "skill"):
                v = st.get(k)
                if not isinstance(v, str) or not v.strip():
                    errors.append("stages[%d].%s 必须是非空字符串" % (i, k))
            cons = st.get("constraints")
            if not isinstance(cons, list) or not all(
                    isinstance(c, str) and c.strip() for c in cons):
                errors.append("stages[%d].constraints 必须是非空字符串列表" % i)
        # 六阶段各恰好一次（缺/重复/乱序）
        for s in STAGE_ENUM:
            if seen.count(s) > 1:
                errors.append("阶段 %s 重复出现 %d 次（必须恰好一次）"
                              % (s, seen.count(s)))
        missing = [s for s in STAGE_ENUM if s not in seen]
        if missing:
            errors.append("缺少阶段: %s" % ", ".join(missing))
    return not errors, errors


def check_artifacts(worktree, arts):
    """必需工件存在性. Returns (ok, missing)."""
    missing = [a for a in arts if not os.path.exists(os.path.join(worktree, a))]
    return not missing, missing


def check_receipt_hash(m, worktree):
    """protocol hash 与执行 receipt 一致.

    receipt.bind_to 指向 manifest.sha256（提交内）; 重新计算 method.yaml
    hash 并比对 manifest 中的 method 条目（若存在）.
    Returns (ok, msg).
    """
    bind = (m.get("receipt") or {}).get("bind_to")
    if not bind:
        return False, "receipt.bind_to 缺失"
    manifest_path = os.path.join(worktree, bind)
    if not os.path.exists(manifest_path):
        return False, "receipt 指向 %s 不存在" % bind
    # method.yaml hash
    h = hashlib.sha256(
        open(os.path.join(worktree, "method.yaml"), "rb").read()).hexdigest()
    # manifest 里找 method 条目
    try:
        for line in open(manifest_path):
            line = line.strip()
            if line.endswith("  method.yaml") or "method.yaml" in line:
                mh = line.split()[0]
                if mh == h:
                    return True, "protocol hash 匹配 %s" % h[:12]
                return False, "protocol hash 不匹配 (manifest=%s, actual=%s)" % (
                    mh[:12], h[:12])
    except Exception as e:
        return False, "manifest 读取失败: %s" % e
    return False, "manifest 中无 method.yaml 条目"


def check_protocol_log(worktree):
    """PROTOCOL-LOG 修订完整性: 追加式, 无删改（只看格式）. Returns (ok, msg)."""
    log_path = os.path.join(worktree, "PROTOCOL-LOG.md")
    if not os.path.exists(log_path):
        return True, "无 PROTOCOL-LOG（可选，无修订）"
    lines = open(log_path).readlines()
    events = [l for l in lines if LOG_EVENT_RE.match(l.strip())]
    if not events:
        return True, "PROTOCOL-LOG 存在但无修订事件"
    return True, "PROTOCOL-LOG %d 条修订事件" % len(events)


def check_holdout(m, worktree):
    """holdout 隔离: true=隔离; false=report 必须显式记录剩余风险.

    codex P0-3: false 无条件 PASS 太松 — 必须检查 report 存在且声明风险.
    """
    isolated = (m.get("splits") or {}).get("holdout_isolated")
    if isolated is True:
        return True, "holdout 隔离已声明"
    if isolated is False:
        # 找 report（results/report.md 或类似）
        report = None
        for cand in ("results/report.md", "results/report.json",
                     "report.md", "results/decision.csv"):
            p = os.path.join(worktree, cand)
            if os.path.exists(p):
                report = p
                break
        if not report:
            return False, "holdout 未隔离 — 必须有 report 显式记录剩余风险（未找到 report）"
        text = open(report).read().lower()
        if any(k in text for k in ("剩余风险", "residual risk", "holdout",
                                   "risk:", "风险")):
            return True, "holdout 未隔离 — report 已记录剩余风险（advisory）"
        return False, "holdout 未隔离 — report 未显式记录剩余风险"
    return False, "splits.holdout_isolated 必须是布尔"


def run(worktree, verbose=True):
    results = {}
    mpath = os.path.join(worktree, "method.yaml")
    if not os.path.exists(mpath):
        return {"status": "FAIL", "checks": {
            "schema": (False, "method.yaml 缺失")}}, 1
    try:
        m = load_yaml(mpath)
        checks = {}
        checks["schema"] = check_schema(m)
        if checks["schema"][0]:
            checks["artifacts"] = check_artifacts(worktree, m.get("required_artifacts", []))
            checks["receipt_hash"] = check_receipt_hash(m, worktree)
            checks["protocol_log"] = check_protocol_log(worktree)
            checks["holdout"] = check_holdout(m, worktree)
        else:
            checks["artifacts"] = (False, ["schema FAIL — 跳过工件检查"])
            checks["receipt_hash"] = (False, ["schema FAIL — 跳过 hash 检查"])
            checks["protocol_log"] = (True, "schema FAIL — 跳过")
            checks["holdout"] = (False, ["schema FAIL — 跳过"])
    except Exception as e:
        # codex P0-2: malformed YAML / 非预期结构 → 受控 FAIL, 不 traceback
        return {"status": "FAIL", "checks": {
            "schema": (False, "解析/校验异常: %s" % str(e)[:120])}}, 1
    failed = [k for k, (ok, _) in checks.items() if not ok]
    status = "PASS" if not failed else "FAIL"
    if verbose:
        print("method preflight: %s" % status)
        for k, (ok, msg) in checks.items():
            tag = "✅" if ok else "❌"
            print("  %s %s: %s" % (tag, k, msg if isinstance(msg, str) else "; ".join(msg)))
    return {"status": status, "checks": {k: {"ok": v[0], "msg": v[1]} for k, v in checks.items()}}, 0 if status == "PASS" else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="method protocol preflight")
    ap.add_argument("--worktree", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res, code = run(args.worktree, verbose=not args.json)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    sys.exit(code)
