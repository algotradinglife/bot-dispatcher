#!/usr/bin/env python3
"""PI-GATE — machine-executable gates on PI operations.

Verifies PI's key operations against the workflow contract. Dispatcher is
a PROCESS VERIFIER, never a science decider: gates check legality/completeness
of operations, not the correctness of conclusions.

Design: docs/PI-GATE.md (2026-08-11, PI self-proposed).
"""
import json
import re
import subprocess
import sys
import time

GATE_NAMES = ["G01", "G02", "G03", "G04", "G05", "G06"]

# EV PASS 评论中的审计 SHA 标记 (auditor 交付规范)
EV_SHA_RE = re.compile(r"AUDITED_SHA=([0-9a-f]{7,40})", re.IGNORECASE)

# 结构化 EV-VERDICT 块 (PI review item 2):
# [EV-VERDICT] auditor=... sha=<40位> verdict=PASS timestamp=... [/EV-VERDICT]
EV_BLOCK_RE = re.compile(
    r"\[EV-VERDICT\](.*?)\[/EV-VERDICT\]", re.IGNORECASE | re.DOTALL)
EV_FIELD_RE = re.compile(r"^\s*(\w+)=(.+?)\s*$", re.MULTILINE)


def parse_ev_verdicts(text):
    """Parse all EV-VERDICT blocks; return list of dicts (oldest→newest)."""
    out = []
    for blk in EV_BLOCK_RE.findall(text):
        d = {}
        for m in EV_FIELD_RE.finditer(blk):
            d[m.group(1).lower()] = m.group(2).strip()
        if d.get("verdict") and d.get("sha"):
            out.append(d)
    return out


def latest_ev_pass(text):
    """Latest (by timestamp field) EV verdict that is PASS, or None.

    PI review P0-1: 必须按时间选择最新的有效 verdict，然后要求它是
    PASS。先过滤 REJECT 再选最新 PASS 会漏掉"10:00 PASS、11:00 REJECT"
    的场景 — 最新 verdict 是 REJECT, 必须拒绝.
    """
    verdicts = parse_ev_verdicts(text)
    if not verdicts:
        return None
    # 按 timestamp 排序取最新 (全局时间序, 不依赖文本位置)
    verdicts.sort(key=lambda v: v.get("timestamp", ""))
    latest = verdicts[-1]
    if latest.get("verdict", "").upper() == "PASS":
        return latest
    return None


# 可信 auditor 白名单 (独立审计账号; PI 不是独立 auditor — P0-2)
TRUSTED_AUDITORS = ("everything-bot-engineer",)


def _fetch_comments_meta(repo, issue_num, pr_num):
    """Fetch comments with REAL author.login + createdAt (P0-2).

    不信任正文自报的 auditor=/timestamp= — 读取 GitHub comment 的真实
    元数据. 返回 [{author, created_at, body}]. pr_num=None 时只查 issue.
    """
    out = []
    views = [["issue", "view", str(issue_num)]]
    if pr_num:
        views.append(["pr", "view", str(pr_num)])
    for view in views:
        r = _gh(view + ["--repo", repo, "--json", "comments",
                        "--jq", ".comments[] | {a: .author.login, t: .createdAt, b: .body}"])
        if r.returncode != 0:
            continue
        try:
            items = json.loads(r.stdout)
        except Exception:
            items = []
        for it in items if isinstance(items, list) else [items]:
            out.append({
                "author": (it.get("a") or "").lower(),
                "created_at": it.get("t") or "",
                "body": it.get("b") or "",
            })
    return out


def latest_ev_pass_meta(comments):
    """Latest (by real createdAt) EV verdict that is PASS, from trusted
    auditor only. P0-2: 身份/时间取自 comment 元数据, 非正文.

    Returns dict or None. 正文仍需含 EV-VERDICT 块 (sha/verdict), 但
    auditor 以 comment 真实 author 为准; timestamp 以 createdAt 为准.
    """
    candidates = []
    for cm in comments:
        if cm["author"] not in TRUSTED_AUDITORS:
            continue
        for v in parse_ev_verdicts(cm["body"]):
            v["_author"] = cm["author"]
            v["_created_at"] = cm["created_at"]
            candidates.append(v)
    if not candidates:
        return None
    candidates.sort(key=lambda v: v.get("_created_at", ""))
    latest = candidates[-1]
    if latest.get("verdict", "").upper() == "PASS":
        return latest
    return None

# REQ 编号 (契约 body) — E01-E08 + issue 特定 (如 REQ-207-01)
# 支持 REQ-E01 和 REQ-E-01 两种写法; 捕获完整 REQ-xxx (统一格式)
REQ_RE = re.compile(r"\b(REQ-[A-Z][A-Z0-9]*-?\d{1,2})\b")

# worker 交付表行: | REQ-E01 | evidence | PASS |
REQ_ROW_RE = re.compile(r"\|\s*(REQ-[A-Z][A-Z0-9]*-?\d{1,2})\s*\|")

# PI 对抗性标记: [ADVERSARIAL] baseline=pass leak=pass ...
ADVERSARIAL_RE = re.compile(r"\[ADVERSARIAL\]\s*([^\]]+)", re.IGNORECASE)


def _gh(args, timeout=20):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)
    return r


def _issue_graph(repo, issue_num):
    """Live graph receipt: project/milestone/blockedBy/blocking/parent/owner.

    PI-GATE G01 增强 (PI review item 3): blocker 状态 OPEN/CLOSED,
    Milestone, 唯一 Project 都解析; 只把 OPEN 的 blocker 视为阻塞.
    """
    r = _gh(["issue", "view", str(issue_num), "--repo", repo,
             "--json", "projectItems,blockedBy,blocking,parent,assignees,milestone"])
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    projects = [p for p in d.get("projectItems", []) if p.get("status")]
    # blockedBy/blocking 结构: list / dict / {nodes: [...]} (gh 版本差异)
    def _blockers(field):
        v = d.get(field)
        items = []
        if isinstance(v, list):
            items = v
        elif isinstance(v, dict):
            items = v.get("nodes") if isinstance(v.get("nodes"), list) else [v]
        out = []
        for b in items:
            if not isinstance(b, dict):
                continue
            num = b.get("number")
            st = (b.get("state") or b.get("status") or "").upper()
            # 只统计 OPEN 的 blocker (CLOSED 不算阻塞)
            out.append({"number": num, "open": st in ("", "OPEN")})
        return out
    return {
        "projects": projects,
        "blocked_by": _blockers("blockedBy"),
        "blocking": _blockers("blocking"),
        "parent": (d.get("parent") or {}).get("number"),
        "milestone": (d.get("milestone") or {}).get("title"),
        "assignees": [a.get("login") if isinstance(a, dict) else a
                      for a in d.get("assignees", [])],
    }


def _pr_head(repo, pr_num):
    r = _gh(["pr", "view", str(pr_num), "--repo", repo,
             "--json", "headRefOid,state,reviewDecision"])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def check_g05_only(repo, issue_num):
    """轻量 G05 对账 (P0-D): 单次 issue 查询, 不跑完整 gate.

    Returns ("PASS"|"REMIND"|"FAIL", evidence). dispatcher 用它替代
    完整 check_pi_gates — 避免每分钟 ~150 次 gh 调用触发 API 限流.
    """
    r = _gh(["issue", "view", str(issue_num), "--repo", repo,
             "--json", "state,projectItems"])
    if r.returncode != 0:
        return ("FAIL", "issue unavailable")
    try:
        d = json.loads(r.stdout)
    except Exception:
        return ("FAIL", "issue unavailable")
    closed = d.get("state") == "CLOSED"
    done = any((p.get("status") or {}).get("name") == "Done"
               for p in d.get("projectItems", []))
    if closed and done:
        return ("PASS", "issue closed=True project Done=True")
    return ("REMIND", "issue closed=%s project Done=%s" % (closed, done))


def check_pi_gates(repo, issue_num=None, pr_num=None, operation="merge",
                   title="", url=""):
    """Run the 6 PI gates. Returns {gate: (PASS|FAIL|REMIND|SKIP, evidence)}."""
    result = {}

    # G01 — Graph: 唯一 owner + 无未关 blocker + milestone 存在
    graph = _issue_graph(repo, issue_num) if issue_num else None
    if graph is None:
        result["G01"] = ("FAIL", "graph unavailable (control plane)")
    else:
        owners = {p.get("title") for p in graph["projects"]}
        open_blockers = [b["number"] for b in graph["blocked_by"]
                         if b.get("open")]
        has_milestone = bool(graph.get("milestone"))
        evidence = "projects=%s blockedBy(open)=%s milestone=%s" % (
            sorted(owners), open_blockers, graph.get("milestone") or "MISSING")
        ok = len(owners) == 1 and not open_blockers and has_milestone
        result["G01"] = ("PASS" if ok else "FAIL", evidence)

    # G02 — Contract: REQ 完整性 (机器可解析: 契约 REQ 编号 vs 交付表)
    if issue_num:
        r = _gh(["issue", "view", str(issue_num), "--repo", repo,
                 "--json", "body", "--jq", ".body"])
        body = r.stdout if r.returncode == 0 else ""
        contract_reqs = sorted(set(REQ_RE.findall(body)))
        # worker 交付表: REQ→evidence (PR/issue 评论里的表格行)
        r2 = _gh(["issue", "view", str(issue_num), "--repo", repo,
                  "--json", "comments", "--jq", ".comments[].body"])
        delivered_reqs = set()
        if r2.returncode == 0:
            for line in r2.stdout.splitlines():
                m = REQ_ROW_RE.match(line.strip())
                if m:
                    # P1-A: 解析 Markdown 列, 要求【最终状态列】== PASS
                    # (| REQ-E01 | evidence | FAIL | 不算; not PASS 不算)
                    cols = [c.strip().upper() for c in line.strip().strip("|").split("|")]
                    if cols and cols[-1] == "PASS":
                        delivered_reqs.add(m.group(1))
        missing = [r for r in contract_reqs if r not in delivered_reqs]
        if not contract_reqs:
            result["G02"] = ("REMIND",
                             "no REQ referenced (template E01-E08 recommended)")
        elif missing:
            result["G02"] = ("FAIL",
                             "REQ missing evidence: %s" % ",".join(missing))
        else:
            result["G02"] = ("PASS",
                             "REQ covered: %s" % ",".join(contract_reqs))
    else:
        result["G02"] = ("SKIP", "no issue")

    # G03 — EV binding: 最新 EV PASS 的完整 SHA == PR HEAD
    # (P0-A: HEAD 两次读取都必须成功且相等 — fail-closed;
    #  P0-B: 结构化契约已启用, 删除 legacy AUDITED_SHA fallback)
    if pr_num:
        head_before = _pr_head(repo, pr_num)
        head = (head_before or {}).get("headRefOid", "")
        comments_meta = _fetch_comments_meta(repo, issue_num, pr_num)
        latest = latest_ev_pass_meta(comments_meta)
        # P0-A: fail-closed — 两次读取都必须成功且相等
        head_after = _pr_head(repo, pr_num)
        head_after_val = (head_after or {}).get("headRefOid", "")
        head_stable = bool(head) and bool(head_after_val) \
            and head == head_after_val

        if latest is None:
            # P0-B: 结构化契约已启用, 无 legacy fallback
            result["G03"] = ("FAIL",
                             "no trusted structured EV PASS (EV-VERDICT required)")
        else:
            sha = latest.get("sha", "")
            # 只接受正则严格匹配的完整 40 位 SHA, 用 ==
            full_sha = bool(re.fullmatch(r"[0-9a-f]{40}", sha))
            auditor = latest.get("_author", "")
            ok = head_stable and full_sha \
                and head == sha and auditor in TRUSTED_AUDITORS
            result["G03"] = (
                "PASS" if ok else "FAIL",
                "EV sha=%s... head=%s auditor=%s full40=%s stable=%s ts=%s"
                % (sha[:12], head[:12], auditor or "-",
                   full_sha, head_stable,
                   (latest.get("_created_at") or "-")[:19]))
    else:
        result["G03"] = ("SKIP", "no PR")

    # G04 — Adversarial: PI 终审评论的 [ADVERSARIAL] 结构化标记
    # P1-7: 标记必须含有效键值 (baseline=/leak=/selection=/overfit=)
    # P1-B: 必须是 PI (hh1985) 的真实评论 — 验证 comment author
    if issue_num:
        # 用带元数据的评论获取 — 验证作者
        meta = _fetch_comments_meta(repo, issue_num, None)
        valid_markers = []
        for cm in meta:
            if cm["author"] not in ("hh1985",):
                continue  # 只信 PI 的评论
            for m in ADVERSARIAL_RE.findall(cm["body"]):
                if re.search(r"\b(baseline|leak|selection|overfit|confound|robust)"
                             r"\s*=\s*\S+", m, re.IGNORECASE):
                    valid_markers.append((cm["created_at"][:19], m.strip()[:30]))
        if valid_markers:
            # 取最新的 PI 对抗标记
            valid_markers.sort()
            latest_marker = valid_markers[-1]
            result["G04"] = ("PASS",
                             "PI adversarial @%s: %s" % latest_marker)
        else:
            result["G04"] = ("REMIND", "no PI structured adversarial evidence")
    else:
        result["G04"] = ("SKIP", "no issue")

    # G05 — Terminal reconciliation (merge 后): issue closed + project Done
    if issue_num:
        r = _gh(["issue", "view", str(issue_num), "--repo", repo,
                 "--json", "state,projectItems"])
        if r.returncode == 0:
            d = json.loads(r.stdout)
            closed = d.get("state") == "CLOSED"
            done = any((p.get("status") or {}).get("name") == "Done"
                       for p in d.get("projectItems", []))
            ok = closed and done
            result["G05"] = ("PASS" if ok else "REMIND",
                             "issue closed=%s project Done=%s" % (closed, done))
        else:
            result["G05"] = ("FAIL", "issue unavailable")
    else:
        result["G05"] = ("SKIP", "no issue")

    # G06 — Downstream activation: 下游 issue 是否在 PI 授权前置 Ready
    # P1-8: 只把"Ready 且无 PI [ACTIVATE] receipt"视为 premature
    # P1-C: [ACTIVATE] 必须来自 PI (hh1985) 的真实评论 — 不能伪造
    if graph and graph["blocking"]:
        premature = []
        for child in graph["blocking"]:
            num = child.get("number")
            if num is None or not child.get("open"):
                continue
            r = _gh(["issue", "view", str(num), "--repo", repo,
                     "--json", "projectItems",
                     "--jq", ".projectItems[].status.name"])
            if r.returncode != 0:
                continue
            status = r.stdout
            # [ACTIVATE] receipt: 只信 hh1985 的真实评论 (P1-C)
            child_meta = _fetch_comments_meta(repo, num, None)
            activated = False
            for cm in child_meta:
                if cm["author"] != "hh1985":
                    continue
                if re.search(r"\[ACTIVATE\]\s*(?:issue\s*[=:]?\s*)?%d\b" % num,
                             cm["body"], re.IGNORECASE):
                    activated = True
                    break
            if "Ready" in status and not activated:
                premature.append(num)
        result["G06"] = ("FAIL" if premature else "PASS",
                         "downstream premature Ready: %s" % premature
                         if premature else "no premature activation")
    else:
        result["G06"] = ("SKIP", "no blocking children")

    return result


def render_receipt(result, operation, title="", url="", as_json=False):
    """Render a gate receipt — human table or machine JSON.

    as_json=True → {"operation", "title", "gates": {G01: {status,
    evidence}}, "blocked": [..], "timestamp"}. PI 工具/CI 可直接消费.
    """
    if as_json:
        gates = {g: {"status": s, "evidence": e}
                 for g, (s, e) in result.items()}
        blocked = sorted(g for g, (s, _) in result.items() if s == "FAIL")
        return json.dumps({
            "operation": operation,
            "title": title,
            "url": url,
            "gates": gates,
            "blocked": blocked,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False)

    lines = ["## PI-GATE receipt — %s" % operation]
    if title:
        lines.append("> %s" % title)
    lines.append("")
    lines.append("| Gate | Result | Evidence |")
    lines.append("|---|---|---|")
    for g in GATE_NAMES:
        status, ev = result.get(g, ("SKIP", ""))
        icon = {"PASS": "✅", "FAIL": "⛔", "REMIND": "⚠️", "SKIP": "—"}.get(status, "—")
        lines.append("| PI-%s | %s %s | %s |" % (g, icon, status, ev[:80]))
    lines.append("")
    blocks = [g for g, (s, _) in result.items() if s == "FAIL"]
    lines.append("**阻断**" if blocks else "**全部通过/提醒**")
    if blocks:
        lines.append("FAIL gates: %s — 操作被拒绝，需先解决。" % ", ".join(blocks))
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="PI-GATE check")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--issue", type=int)
    ap.add_argument("--pr", type=int)
    ap.add_argument("--operation", default="merge")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable JSON receipt")
    ap.add_argument("--exit-on-block", action="store_true",
                    help="exit non-zero when any gate FAILs (P0-C: live gate)")
    args = ap.parse_args()
    res = check_pi_gates(args.repo, args.issue, args.pr, args.operation)
    print(render_receipt(res, args.operation, as_json=args.json))
    if args.exit_on_block:
        blocked = [g for g, (s, _) in res.items() if s == "FAIL"]
        if blocked:
            sys.exit(1)
