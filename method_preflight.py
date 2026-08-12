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
    stages = m.get("stages", [])
    if stages is not None and not isinstance(stages, list):
        errors.append("stages 必须是列表")
    elif isinstance(stages, list):
        for i, st in enumerate(stages):
            for k in STAGE_KEYS:
                if k not in st:
                    errors.append("stages[%d] 缺少字段: %s" % (i, k))
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


def check_holdout(m):
    """holdout 隔离声明: true=隔离; false=剩余风险必须记录. Returns (ok, msg)."""
    isolated = (m.get("splits") or {}).get("holdout_isolated")
    if isolated is True:
        return True, "holdout 隔离已声明"
    if isolated is False:
        return True, "holdout 未隔离 — 剩余风险需在 report 显式记录（advisory）"
    return False, "splits.holdout_isolated 必须是布尔"


def run(worktree, verbose=True):
    results = {}
    mpath = os.path.join(worktree, "method.yaml")
    if not os.path.exists(mpath):
        return {"status": "FAIL", "checks": {
            "schema": (False, "method.yaml 缺失")}}, 1
    m = load_yaml(mpath)
    checks = {}
    checks["schema"] = check_schema(m)
    if checks["schema"][0]:
        checks["artifacts"] = check_artifacts(worktree, m.get("required_artifacts", []))
        checks["receipt_hash"] = check_receipt_hash(m, worktree)
        checks["protocol_log"] = check_protocol_log(worktree)
        checks["holdout"] = check_holdout(m)
    else:
        checks["artifacts"] = (False, ["schema FAIL — 跳过工件检查"])
        checks["receipt_hash"] = (False, ["schema FAIL — 跳过 hash 检查"])
        checks["protocol_log"] = (True, "schema FAIL — 跳过")
        checks["holdout"] = (False, ["schema FAIL — 跳过"])
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
