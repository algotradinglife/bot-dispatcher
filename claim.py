#!/usr/bin/env python3
"""claim — worker 认领事务 (Ready → In Progress).

把"认领"绑定为单一事务: 只有当前状态是 Ready 才写入 In Progress,
同时发布 [CLAIM] 认领评论 (session 标识, 并发互斥依据),
写后读回确认, 输出机器可读 receipt. 任一步失败 → exit 非 0 →
worker 不得宣称已认领 (fail-closed).

并发互斥 (GitHub 无 CAS, 用评论做 claim 依据):
- Ready → 写 [CLAIM] 评论 (幂等 key=session) → 写 In Progress → 读回
- 已 In Progress: 最近的 [CLAIM] 评论 session 与当前一致 → 视为本会话已认领
  (可续跑); 不一致或缺失 → 拒绝 (fail-closed, 不覆盖他人认领)

--check-only 模式: 只检查不写状态 — dispatcher 断链监控复用同一逻辑.

用法:
  python3 claim.py --repo <repo> --issue <N> --session <id> [--check-only] [--json]
"""
import argparse
import json
import subprocess
import sys
import time

from finalize_delivery import (
    get_project_item, get_status_field_ids, set_status,
)

# worker 认领白名单: 只允许 Ready → In Progress
CLAIM_FROM = {"Ready": "In Progress"}
CLAIM_MARKER = "[CLAIM] session="


def _gh(args, timeout=20):
    return subprocess.run(["gh"] + args, capture_output=True,
                          text=True, timeout=timeout)


def _gh_api(method, path, body=None, timeout=25):
    cmd = ["gh", "api", "--method", method, path]
    if body is not None:
        cmd += ["-f", "body=%s" % body]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def list_comments(repo, issue_num):
    """读 issue 评论列表. Returns (comments_json_list, err)."""
    r = _gh_api("GET", "repos/algotradinglife/%s/issues/%d/comments?per_page=100"
                % (repo, issue_num))
    if r.returncode != 0:
        return None, r.stderr.strip()[:120]
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, "parse error: %s" % e


def find_claim_session(comments):
    """在评论里找最近的 [CLAIM] session=... 标记. Returns (session_id, None) or (None, err)."""
    for c in reversed(comments or []):
        body = c.get("body") or ""
        for line in body.splitlines():
            if line.startswith(CLAIM_MARKER):
                return line[len(CLAIM_MARKER):].strip(), None
    return None, None


def post_claim_comment(repo, issue_num, session_id):
    """发 [CLAIM] 评论 (幂等: 已存在同 session 则复用). Returns (comment_id, err)."""
    comments, err = list_comments(repo, issue_num)
    if err:
        return None, err
    existing, _ = find_claim_session(comments)
    if existing == session_id:
        for c in reversed(comments or []):
            if CLAIM_MARKER + session_id in (c.get("body") or ""):
                return c.get("id"), None
    body = "%s%s\n(认领事务: 并发互斥依据 — 非本 session 的 In Progress 拒绝续跑)"
    body = body % (CLAIM_MARKER, session_id)
    r = _gh_api("POST", "repos/algotradinglife/%s/issues/%d/comments"
                % (repo, issue_num), body)
    if r.returncode != 0:
        return None, r.stderr.strip()[:120]
    try:
        return json.loads(r.stdout).get("id"), None
    except Exception as e:
        return None, "parse error: %s" % e


def claim(repo, issue_num, session_id, check_only=False, verbose=True):
    """事务式认领. Returns (ok, steps, receipt)."""
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    # 0. 输入校验
    if not step("session_id", bool(session_id), "session_id 非空"):
        return False, steps, None

    # 1. project item (单 Project + 当前状态)
    item_id, project_id, status, err = get_project_item(repo, issue_num)
    if not step("issue_project", item_id is not None, err or "project item 读取成功"):
        return False, steps, None

    # 2. 状态分派
    if status == "In Progress":
        # 已 In Progress: 只有本 session 认领过才可续跑 (fail-closed)
        comments, err = list_comments(repo, issue_num)
        if not step("claim_comments", comments is not None, err or "评论读取成功"):
            return False, steps, None
        owner, _ = find_claim_session(comments)
        if not step("claim_owner", owner == session_id,
                    "In Progress 认领者=%s 当前=%s (不一致拒绝续跑)" % (owner, session_id)):
            return False, steps, None
        return True, steps, {"mode": "resume", "status_before": "In Progress",
                             "status_after": "In Progress",
                             "session_id": session_id, "claim_owner": owner}

    # 3. 状态转换合法性 (worker 认领白名单)
    target = CLAIM_FROM.get(status)
    if not step("transition_allowed", target == "In Progress",
                "worker 认领只允许 Ready→In Progress, 当前 %s" % status):
        return False, steps, None

    if check_only:
        step("write_skipped", True, "--check-only 不写状态")
        return True, steps, {"mode": "check-only", "would_set": "In Progress"}

    # 4. 写 [CLAIM] 认领评论 (并发互斥依据, 先于状态写)
    claim_id, err = post_claim_comment(repo, issue_num, session_id)
    if not step("claim_comment", claim_id is not None, err or "claim 评论 id=%s" % claim_id):
        return False, steps, None

    # 5. 写入 In Progress
    field_id, opts, err = get_status_field_ids(project_id)
    if not step("field_ids", field_id is not None, err or "Status 字段解析成功"):
        return False, steps, None
    if not step("field_opts", opts is not None, "Status options 解析成功"):
        return False, steps, None
    ip_opt = opts.get("In Progress")
    if not step("in_progress_option_exists", ip_opt is not None, "In Progress option 存在"):
        return False, steps, None
    ok, err = set_status(project_id, item_id, field_id, ip_opt)
    if not step("write_in_progress", ok, err or "已写入 In Progress"):
        return False, steps, None

    # 6. 读回确认 (状态 + claim 评论)
    _, _, status2, err2 = get_project_item(repo, issue_num)
    if not step("readback_status", status2 == "In Progress",
                "读回 status=%s" % status2):
        return False, steps, None
    comments, err = list_comments(repo, issue_num)
    if not step("readback_claim", comments is not None, err or "评论读回成功"):
        return False, steps, None
    owner, _ = find_claim_session(comments)
    if not step("readback_owner", owner == session_id,
                "claim 评论读回 session=%s" % owner):
        return False, steps, None

    receipt = {
        "repo": repo, "issue": issue_num,
        "status_before": status, "status_after": status2,
        "project_id": project_id, "item_id": item_id, "session_id": session_id,
        "claim_comment_id": claim_id,
        "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return True, steps, receipt


def main():
    ap = argparse.ArgumentParser(description="worker claim transaction")
    ap.add_argument("--repo", required=True, help="repo name (algotradinglife/<repo>)")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--session", required=True, help="worker session id")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ok, steps, receipt = claim(args.repo, args.issue, args.session, args.check_only)

    if args.json:
        print(json.dumps({"ok": ok, "steps": steps, "receipt": receipt},
                         ensure_ascii=False))
    else:
        for s in steps:
            mark = "✅" if s["ok"] else "❌"
            print("%s %s — %s" % (mark, s["step"], s["detail"]))
        if ok and receipt:
            print("RECEIPT: %s" % json.dumps(receipt, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
