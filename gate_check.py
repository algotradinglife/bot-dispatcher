#!/usr/bin/env python3
"""Live PI-GATE status publisher — external advisory gate (C3).

PI review 收口: live gate 不应由 PR 内 workflow 自证 (可被 PR 改
gate/泄露 PAT/评论时序问题). 受保护的 dispatcher 读取实时状态, 用
Status API 向 PR HEAD 发布 advisory commit status:

  pi-gates-live  → success/failure/pending (advisory — 不阻断 merge)

Hard gate (required CheckRun) 需 GitHub App, 已单独立项延期.
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


def publish_status(repo, head_sha, conclusion, title, summary,
                   details_url=""):
    """Publish a commit status (pi-gates-live) — Status API.

    P1-D: 命名 publish_status (语义 = 一次性 commit status, 非 CheckRun).
    P1-B: 返回 True/False — 失败显式返回 False (调用方必须报警).
    Checks API (CheckRun) 需 GitHub App token; Status API 接受普通
    PAT/OAuth, 分支保护 required status checks 同样识别.
    """
    state = {"success": "success", "failure": "failure"}.get(
        conclusion, "pending")
    cmd = ["gh", "api", "--method", "POST",
           "repos/%s/statuses/%s" % (repo, head_sha),
           "-f", "state=%s" % state,
           "-f", "context=%s" % CHECK_NAME,
           "-f", "description=%s" % title[:140]]
    # 硬编码 wrapper 违反"不得提交机器路径"原则 (C3) — 改为环境变量
    # GH_PUSH_WRAPPER (由调度 wrapper 注入, 不提交机器路径).
    if repo == "algotradinglife/bot-dispatcher":
        wrapper = os.environ.get("GH_PUSH_WRAPPER", "")
        if wrapper and os.path.exists(wrapper):
            cmd = [wrapper] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print("⚠️ status publish failed (%s): %s"
              % (head_sha[:12], r.stderr[:150]), file=sys.stderr)
        return False
    return True


def gate_for_pr(repo, pr_num):
    """Run gates for a PR; returns (conclusion, title, summary, head_sha).

    P1-3: 返回实际评估的 HEAD SHA, 由调用方用它发布 (避免 push 竞态).
    P1-2: 评估所有 closing issues (任一 FAIL 即 FAIL).
    P1-1: 无 closing issue → failure (advisory 也不显示绿 — 与
    "每个变更对应 issue" 治理契约一致).
    """
    # PR 信息
    pr = _gh(["pr", "view", str(pr_num), "--repo", repo,
              "--json", "headRefOid,title,closingIssuesReferences,state"])
    if pr.returncode != 0:
        return ("action_required", "PI-GATE: PR unavailable",
                "Cannot read PR #%d" % pr_num, None)
    try:
        d = json.loads(pr.stdout)
    except Exception:
        return ("action_required", "PI-GATE: parse error", "bad PR JSON", None)
    head = d.get("headRefOid", "")
    title = d.get("title") or ""
    closing = [c.get("number") for c in d.get("closingIssuesReferences", [])]

    if not closing:
        # P1-1: 无 closing issue = 违反治理契约 → failure (不显示绿)
        return ("failure", "PI-GATE: no closing issue linked",
                "PR #%d has no closing issue. Every change must map to an "
                "issue ('Closes #N' in PR body)." % pr_num, head)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pi_gates import check_pi_gates, render_receipt

    all_receipts = []
    blocked = []
    reminds = []
    for ci in closing:  # P1-2: 所有 closing issues
        res = check_pi_gates(repo, issue_num=ci, pr_num=pr_num,
                             operation="merge")
        all_receipts.append(render_receipt(
            res, "merge", title=title,
            url="https://github.com/%s/pull/%d" % (repo, pr_num)))
        blocked += [g for g, (s, _) in res.items() if s == "FAIL"]
        reminds += [g for g, (s, _) in res.items() if s == "REMIND"]
    receipt = "\n\n".join(all_receipts)

    if blocked:
        return ("failure", "PI-GATE: blocked by %s" % ",".join(sorted(set(blocked))),
                receipt, head)
    if reminds:
        return ("success", "PI-GATE: PASS (reminders: %s)"
                % ",".join(sorted(set(reminds))), receipt, head)
    return ("success", "PI-GATE: PASS", receipt, head)


def scan_and_publish(repo, limit=10, rotate_key="gate_rotate"):
    """Scan open PRs and publish/update pi-gates-live statuses.

    P1-3: 用 gate_for_pr 返回的实际评估 SHA 发布 (防 push 竞态).
    P1-4: 轮转 — 从上次位置继续, 避免固定取前 N 个导致旧 PR 饥饿.
    P1-A: count = min(limit, len(prs)) — 单 tick 每个 PR 最多一次.
    """
    r = _gh(["pr", "list", "--repo", repo, "--state", "open",
             "--limit", "30", "--json", "number,headRefOid,updatedAt"])
    if r.returncode != 0:
        return []
    try:
        prs = json.loads(r.stdout)
    except Exception:
        return []
    if not prs:
        return []
    # 按更新时间排序 (最活跃优先)
    prs.sort(key=lambda p: p.get("updatedAt") or "", reverse=True)
    # 轮转起点 (跨 tick 记忆)
    rotate = {}
    rp = os.path.expanduser("~/.local/state/bot-dispatcher/gate_rotate.json")
    try:
        with open(rp) as f:
            rotate = json.load(f)
    except Exception:
        rotate = {}
    start = rotate.get(repo, 0)
    # P1-A: 取 min(limit, len(prs)) 个 (循环, 但不多于 PR 总数)
    count = min(limit, len(prs))
    picked = [(prs[(start + i) % len(prs)]) for i in range(count)]
    # 更新轮转位置
    rotate[repo] = (start + count) % max(len(prs), 1)
    try:
        os.makedirs(os.path.dirname(rp), exist_ok=True)
        with open(rp, "w") as f:
            json.dump(rotate, f)
    except Exception:
        pass

    published = []
    for pr in picked:
        num = pr["number"]
        conclusion, title, summary, head = gate_for_pr(repo, num)
        if head:  # P1-3: 用实际评估的 SHA
            ok = publish_status(repo, head, conclusion, title, summary)
        else:
            ok = False
        # P1-B: 发布失败必须显式记录 (check_id=None 不可静默)
        published.append({"pr": num, "conclusion": conclusion,
                          "published": ok,
                          "head": (head or pr.get("headRefOid", ""))[:12]})
    return published


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Publish pi-gates-live CheckRun")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int)
    args = ap.parse_args()
    if args.pr:
        conclusion, title, summary, head = gate_for_pr(args.repo, args.pr)
        ok = False
        if head:
            ok = publish_status(args.repo, head, conclusion, title, summary)
        print(json.dumps({"pr": args.pr, "conclusion": conclusion,
                          "published": ok}))
    else:
        print(json.dumps(scan_and_publish(args.repo), ensure_ascii=False))
