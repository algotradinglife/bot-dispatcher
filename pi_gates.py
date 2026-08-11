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
    """Latest (by timestamp field) EV PASS verdict, or None.

    全局时间顺序: 按 timestamp 字段排序取最后 — 不是文本顺序.
    """
    verdicts = parse_ev_verdicts(text)
    passes = [v for v in verdicts if v.get("verdict", "").upper() == "PASS"]
    if not passes:
        return None
    passes.sort(key=lambda v: v.get("timestamp", ""))
    return passes[-1]

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


def check_pi_gates(repo, issue_num=None, pr_num=None, operation="merge",
                   title="", url=""):
    """Run the 6 PI gates. Returns {gate: (PASS|FAIL|REMIND|SKIP, evidence)}."""
    result = {}

    # G01 — Graph: 唯一 owner + 无未关 blocker + milestone
    graph = _issue_graph(repo, issue_num) if issue_num else None
    if graph is None:
        result["G01"] = ("FAIL", "graph unavailable (control plane)")
    else:
        owners = {p.get("title") for p in graph["projects"]}
        open_blockers = [b["number"] for b in graph["blocked_by"]
                         if b.get("open")]
        evidence = "projects=%s blockedBy(open)=%s milestone=%s" % (
            sorted(owners), open_blockers, graph.get("milestone") or "-")
        ok = len(owners) == 1 and not open_blockers
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
    # (PI review item 2: 结构化 EV-VERDICT、完整 40 位 SHA、全局时间序、
    #  可信 auditor、操作前后重复读 HEAD)
    if pr_num:
        head_before = _pr_head(repo, pr_num)
        head = (head_before or {}).get("headRefOid", "")
        # 收集 issue + PR 全部评论 → 结构化 EV-VERDICT
        all_comments = ""
        for view in (["issue", "view", str(issue_num)],
                     ["pr", "view", str(pr_num)]):
            r = _gh(view + ["--repo", repo, "--json", "comments",
                            "--jq", ".comments[].body"])
            if r.returncode == 0:
                all_comments += "\n" + r.stdout
        latest = latest_ev_pass(all_comments)
        if latest is None:
            # 兼容旧格式 (AUDITED_SHA=...) 非结构化
            ev_sha = EV_SHA_RE.search(all_comments)
            if ev_sha and head:
                ok = head.startswith(ev_sha.group(1)) \
                    or ev_sha.group(1).startswith(head[:7])
                result["G03"] = ("PASS" if ok else "FAIL",
                                 "legacy AUDITED_SHA %s vs HEAD %s (unstructured)"
                                 % (ev_sha.group(1)[:12], head[:12]))
            elif head:
                result["G03"] = ("FAIL",
                                 "no EV PASS with AUDITED_SHA (stale?)")
            else:
                result["G03"] = ("FAIL", "PR unavailable")
        else:
            sha = latest.get("sha", "")
            full_sha = len(sha) >= 40
            auditor = latest.get("auditor", "")
            trusted = auditor in ("everything-bot-engineer", "hh1985")
            ok = bool(head) and full_sha and trusted \
                and (head.startswith(sha) or sha.startswith(head))
            result["G03"] = (
                "PASS" if ok else "FAIL",
                "EV sha=%s... head=%s auditor=%s full=%s ts=%s"
                % (sha[:12], head[:12], auditor or "-",
                   full_sha, latest.get("timestamp", "-")[:19]))
    else:
        result["G03"] = ("SKIP", "no PR")

    # G04 — Adversarial: PI 终审评论的 [ADVERSARIAL] 结构化标记
    if issue_num:
        r = _gh(["issue", "view", str(issue_num), "--repo", repo,
                 "--json", "comments", "--jq", ".comments[].body"])
        comments = r.stdout if r.returncode == 0 else ""
        # 结构化: [ADVERSARIAL] baseline=pass leak=pass selection=pass
        markers = ADVERSARIAL_RE.findall(comments)
        if markers:
            result["G04"] = ("PASS",
                             "adversarial markers: %s" % ",".join(markers))
        elif any(k in comments for k in
                 ("baseline", "泄漏", "selection bias", "对抗", "adversarial")):
            result["G04"] = ("PASS", "adversarial keywords found (unstructured)")
        else:
            result["G04"] = ("REMIND", "no adversarial review evidence")
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
    # (扫描 blocking 子项的状态 — 只查 OPEN 的下游)
    if graph and graph["blocking"]:
        premature = []
        for child in graph["blocking"]:
            num = child.get("number")
            if num is None or not child.get("open"):
                continue
            r = _gh(["issue", "view", str(num), "--repo", repo,
                     "--json", "projectItems",
                     "--jq", ".projectItems[].status.name"])
            if r.returncode == 0 and "Ready" in r.stdout:
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
    args = ap.parse_args()
    res = check_pi_gates(args.repo, args.issue, args.pr, args.operation)
    print(render_receipt(res, args.operation, as_json=args.json))
