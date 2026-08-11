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

GATE_NAMES = ["G01", "G02", "G03", "G04", "G05", "G06"]

# EV PASS 评论中的审计 SHA 标记 (auditor 交付规范)
EV_SHA_RE = re.compile(r"AUDITED_SHA=([0-9a-f]{7,40})", re.IGNORECASE)


def _gh(args, timeout=20):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)
    return r


def _issue_graph(repo, issue_num):
    """Live graph receipt: project/milestone/blockedBy/blocking/parent/owner."""
    r = _gh(["issue", "view", str(issue_num), "--repo", repo,
             "--json", "projectItems,blockedBy,blocking,parent,assignees"])
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    projects = [p for p in d.get("projectItems", []) if p.get("status")]
    # blockedBy/blocking 可能是 dict 或 list (gh 版本差异)
    def _nums(field):
        v = d.get(field)
        if isinstance(v, list):
            return [b.get("number") for b in v if isinstance(b, dict)]
        if isinstance(v, dict):
            return [v.get("number")] if v.get("number") else []
        return []
    return {
        "projects": projects,
        "blocked_by": _nums("blockedBy"),
        "blocking": _nums("blocking"),
        "parent": (d.get("parent") or {}).get("number"),
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


def _latest_ev_sha(repo, issue_num, pr_num):
    """Scan issue + PR comments for the newest EV PASS with AUDITED_SHA."""
    shas = []
    for view in (["issue", "view", str(issue_num)],
                 ["pr", "view", str(pr_num)]):
        r = _gh(view + ["--repo", repo, "--json", "comments",
                        "--jq", ".comments[].body"])
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            m = EV_SHA_RE.search(line)
            if m:
                shas.append((m.group(1), line[:60]))
    return shas[-1][0] if shas else None


def check_pi_gates(repo, issue_num=None, pr_num=None, operation="merge",
                   title="", url=""):
    """Run the 6 PI gates. Returns {gate: (PASS|FAIL|REMIND|SKIP, evidence)}."""
    result = {}

    # G01 — Graph: 唯一 owner + 无未关 blocker
    graph = _issue_graph(repo, issue_num) if issue_num else None
    if graph is None:
        result["G01"] = ("FAIL", "graph unavailable (control plane)")
    else:
        owners = {p.get("title") for p in graph["projects"]}
        open_blockers = [b for b in graph["blocked_by"]]
        evidence = "projects=%s blockedBy=%s" % (
            sorted(owners), open_blockers)
        ok = len(owners) == 1 and not open_blockers
        result["G01"] = ("PASS" if ok else "FAIL", evidence)

    # G02 — Contract: REQ 完整性 (通过 issue body 是否含 REQ 引用粗略检查)
    if issue_num:
        r = _gh(["issue", "view", str(issue_num), "--repo", repo,
                 "--json", "body", "--jq", ".body"])
        body = r.stdout if r.returncode == 0 else ""
        has_req = "REQ-" in body
        result["G02"] = ("PASS" if has_req else "REMIND",
                         "REQ referenced in contract" if has_req
                         else "no REQ reference (template E01-E08 recommended)")
    else:
        result["G02"] = ("SKIP", "no issue")

    # G03 — EV binding: 最新 EV PASS 的 AUDITED_SHA == PR HEAD
    if pr_num:
        pr = _pr_head(repo, pr_num)
        ev_sha = _latest_ev_sha(repo, issue_num, pr_num) if issue_num else None
        head = (pr or {}).get("headRefOid", "")
        if head and ev_sha:
            ok = head.startswith(ev_sha) or ev_sha.startswith(head[:7])
            result["G03"] = ("PASS" if ok else "FAIL",
                             "EV SHA %s vs PR HEAD %s" % (ev_sha, head[:12]))
        elif head:
            result["G03"] = ("FAIL", "no AUDITED_SHA in EV comments (stale?)")
        else:
            result["G03"] = ("FAIL", "PR unavailable")
    else:
        result["G03"] = ("SKIP", "no PR")

    # G04 — Adversarial: 终审评论是否含对抗性证据 (粗略)
    if issue_num:
        r = _gh(["issue", "view", str(issue_num), "--repo", repo,
                 "--json", "comments", "--jq", ".comments[].body"])
        comments = r.stdout if r.returncode == 0 else ""
        adv_kw = any(k in comments for k in
                     ("baseline", "泄漏", "selection bias", "对抗", "adversarial"))
        result["G04"] = ("PASS" if adv_kw else "REMIND",
                         "adversarial keywords found" if adv_kw
                         else "no adversarial review evidence")
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
    # (扫描 blocking 子项的状态 — 简化: 依赖 G01 的 blocking 列表)
    if graph and graph["blocking"]:
        premature = []
        for child in graph["blocking"]:
            r = _gh(["issue", "view", str(child), "--repo", repo,
                     "--json", "projectItems",
                     "--jq", ".projectItems[].status.name"])
            if r.returncode == 0 and "Ready" in r.stdout:
                premature.append(child)
        result["G06"] = ("FAIL" if premature else "PASS",
                         "downstream premature Ready: %s" % premature
                         if premature else "no premature activation")
    else:
        result["G06"] = ("SKIP", "no blocking children")

    return result


def render_receipt(result, operation, title="", url=""):
    """Render a gate receipt table (PI 终审前展示)."""
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
    args = ap.parse_args()
    res = check_pi_gates(args.repo, args.issue, args.pr, args.operation)
    print(render_receipt(res, args.operation))
