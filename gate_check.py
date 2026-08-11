#!/usr/bin/env python3
"""Live PI-GATE CheckRun publisher — external authoritative gate.

PI review round-4 收口: live gate 不应由 PR 内 workflow 自证 (可被
PR 改 gate/泄露 PAT/评论时序问题). 改为受保护的 dispatcher 读取实时
状态, 用 GitHub Checks API 向 PR HEAD 发布 required CheckRun:

  pi-gates-live  → 结论反映 check_pi_gates 结果 (PASS/FAIL/NEUTRAL)

分支保护 required checks 包含 pi-gates-live → 只有 dispatcher 发布
的 CheckRun 通过才允许 merge.
"""
import json
import os
import subprocess
import sys
import time

CHECK_NAME = "pi-gates-live"


def _gh(args, timeout=30):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       timeout=timeout)
    return r


def publish_checkrun(repo, head_sha, conclusion, title, summary,
                     details_url=""):
    """Create/update a CheckRun on a commit via Checks API.

    conclusion: success | failure | neutral | cancelled | timed_out |
                action_required
    Returns the check-run id or None.
    """
    payload = {
        "name": CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {
            "title": title[:140],
            "summary": summary[:64000],
        },
    }
    if details_url:
        payload["details_url"] = details_url
    # stdin 传 payload (--input -)
    r = subprocess.run(
        ["gh", "api", "--method", "POST",
         "-H", "Accept: application/vnd.github+json",
         "repos/%s/check-runs" % repo, "--input", "-"],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print("⚠️ check-run publish failed: %s" % r.stderr[:150],
              file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout).get("id")
    except Exception:
        return None


def gate_for_pr(repo, pr_num):
    """Run gates for a PR; returns (conclusion, title, summary)."""
    # PR 信息
    pr = _gh(["pr", "view", str(pr_num), "--repo", repo,
              "--json", "headRefOid,title,closingIssuesReferences,state"])
    if pr.returncode != 0:
        return ("action_required", "PI-GATE: PR unavailable",
                "Cannot read PR #%d" % pr_num)
    try:
        d = json.loads(pr.stdout)
    except Exception:
        return ("action_required", "PI-GATE: parse error", "bad PR JSON")
    head = d.get("headRefOid", "")
    title = d.get("title") or ""
    closing = [c.get("number") for c in d.get("closingIssuesReferences", [])]

    if not closing:
        # 无 closing issue: G02/G05 无法评估 — 用 neutral (不阻断)
        # 文档/CI-only PR 允许通过; 有 issue 的 PR 必须完整 gate
        return ("success", "PI-GATE: no linked issue (docs/CI-only)",
                "PR #%d has no closing issue; gate not applicable. "
                "If this PR resolves an issue, link it with 'Closes #N'."
                % pr_num)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pi_gates import check_pi_gates, render_receipt

    res = check_pi_gates(repo, issue_num=closing[0], pr_num=pr_num,
                         operation="merge")
    blocked = [g for g, (s, _) in res.items() if s == "FAIL"]
    receipt = render_receipt(res, "merge", title=title,
                             url="https://github.com/%s/pull/%d" % (repo, pr_num))

    if blocked:
        return ("failure", "PI-GATE: blocked by %s" % ",".join(blocked),
                receipt)
    # REMIND 不算阻断, 但提示
    reminds = [g for g, (s, _) in res.items() if s == "REMIND"]
    if reminds:
        return ("success", "PI-GATE: PASS (reminders: %s)" % ",".join(reminds),
                receipt)
    return ("success", "PI-GATE: PASS", receipt)


def scan_and_publish(repo, limit=10):
    """Scan open PRs and publish/update pi-gates-live CheckRuns."""
    r = _gh(["pr", "list", "--repo", repo, "--state", "open",
             "--limit", str(limit), "--json", "number,headRefOid"])
    if r.returncode != 0:
        return []
    published = []
    try:
        prs = json.loads(r.stdout)
    except Exception:
        return []
    for pr in prs:
        num = pr["number"]
        head = pr.get("headRefOid", "")
        conclusion, title, summary = gate_for_pr(repo, num)
        cid = publish_checkrun(repo, head, conclusion, title, summary)
        published.append({"pr": num, "conclusion": conclusion,
                          "check_id": cid, "head": head[:12]})
    return published


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Publish pi-gates-live CheckRun")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int)
    args = ap.parse_args()
    if args.pr:
        conclusion, title, summary = gate_for_pr(args.repo, args.pr)
        pr = _gh(["pr", "view", str(args.pr), "--repo", args.repo,
                  "--json", "headRefOid", "--jq", ".headRefOid"])
        cid = publish_checkrun(args.repo, pr.stdout.strip(), conclusion,
                               title, summary)
        print(json.dumps({"pr": args.pr, "conclusion": conclusion,
                          "check_id": cid}))
    else:
        print(json.dumps(scan_and_publish(args.repo), ensure_ascii=False))
