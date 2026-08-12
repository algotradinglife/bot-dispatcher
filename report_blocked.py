#!/usr/bin/env python3
"""report_blocked — worker 诉求上报 (执行态 → Blocked + 结构化原因).

把"worker 需要 PI 决策/解除依赖"绑定为单一事务: 先发布结构化原因评论
(幂等, 同 reason 不重复), 再把状态写入 Blocked (最后发布动作),
写后读回确认 (状态 + 评论), 输出机器可读 receipt. 任一步失败 →
exit 非 0 → worker 不得宣称已上报 (fail-closed).

评论先于状态写的原因: 若评论失败则状态不动 (无"Blocked 但无原因"半事务);
评论成功但状态失败 = 仅多一条诉求评论, 可重试, 幂等键防重复.

--check-only 模式: 只检查不写状态 — dispatcher 断链监控复用同一逻辑.

用法:
  python3 report_blocked.py --repo <repo> --issue <N> --reason <text> \\
      [--check-only] [--json]
"""
import argparse
import json
import subprocess
import sys
import time

from finalize_delivery import get_project_item, get_status_field_ids, set_status

# worker 诉求白名单: 只允许 In Progress → Blocked
BLOCKED_FROM = {"In Progress": "Blocked"}
BLOCKED_MARKER = "[BLOCKED]"


def _gh(args, timeout=20):
    return subprocess.run(["gh"] + args, capture_output=True,
                          text=True, timeout=timeout)


def _reason_key(reason):
    """幂等键: reason 的稳定摘要 (去空白 + 取前 64 字符哈希)."""
    import hashlib
    return hashlib.sha256(reason.strip().encode("utf-8")).hexdigest()[:16]


def list_comments(repo, issue_num):
    r = _gh(["issue", "view", str(issue_num), "--repo", "algotradinglife/" + repo,
             "--json", "comments"])
    if r.returncode != 0:
        return None, r.stderr.strip()[:120]
    try:
        return json.loads(r.stdout).get("comments", []), None
    except Exception as e:
        return None, "parse error: %s" % e


def find_existing_blocked(comments, key):
    """找同 reason 幂等键的已有评论. Returns (comment_id, None) or (None, None)."""
    for c in reversed(comments or []):
        body = c.get("body") or ""
        if BLOCKED_MARKER in body and ("key=%s" % key) in body:
            return c.get("id"), None
    return None, None


def post_blocked_comment(repo, issue_num, reason, key):
    """发 [BLOCKED] 原因评论 (幂等). Returns (comment_id, err)."""
    body = ("%s key=%s\n诉求: %s\n"
            "规则: Blocked = AI 循环内结构化诉求 (PI 可决策); "
            "PI 解阻后回原状态继续。" % (BLOCKED_MARKER, key, reason.strip()))
    r = _gh(["issue", "comment", str(issue_num), "--repo",
             "algotradinglife/" + repo, "--body", body])
    if r.returncode != 0:
        return None, r.stderr.strip()[:120]
    return "posted", None  # gh issue comment 输出 URL 非 id; 读回时按 key 匹配


def report_blocked(repo, issue_num, reason, check_only=False, verbose=True):
    """事务式 Blocked 上报. Returns (ok, steps, receipt)."""
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    # 1. 输入校验 (strip 后非空)
    if not step("reason", bool((reason or "").strip()), "reason 非空白"):
        return False, steps, None
    key = _reason_key(reason)

    # 2. project item (单 Project + 当前状态)
    item_id, project_id, status, err = get_project_item(repo, issue_num)
    if not step("issue_project", item_id is not None, err or "project item 读取成功"):
        return False, steps, None

    # 3. 状态转换合法性 (worker 诉求白名单)
    target = BLOCKED_FROM.get(status)
    if not step("transition_allowed", target == "Blocked",
                "worker 诉求只允许 In Progress→Blocked, 当前 %s" % status):
        return False, steps, None

    if check_only:
        step("write_skipped", True, "--check-only 不写状态")
        return True, steps, {"mode": "check-only", "would_set": "Blocked"}

    # 4. 发布结构化原因评论 (先于状态写; 幂等)
    comments, err = list_comments(repo, issue_num)
    if not step("read_existing", comments is not None, err or "现有评论读取成功"):
        return False, steps, None
    existing_id, _ = find_existing_blocked(comments, key)
    if existing_id is not None:
        step("blocked_comment", True, "幂等复用已有评论 id=%s" % existing_id)
    else:
        cid, err = post_blocked_comment(repo, issue_num, reason, key)
        if not step("blocked_comment", cid is not None, err or "评论发布成功"):
            return False, steps, None

    # 5. 写入 Blocked (最后发布动作)
    field_id, opts, err = get_status_field_ids(project_id)
    if not step("field_ids", field_id is not None, err or "Status 字段解析成功"):
        return False, steps, None
    if not step("field_opts", opts is not None, "Status options 解析成功"):
        return False, steps, None
    blk_opt = opts.get("Blocked")
    if not step("blocked_option_exists", blk_opt is not None, "Blocked option 存在"):
        return False, steps, None
    ok, err = set_status(project_id, item_id, field_id, blk_opt)
    if not step("write_blocked", ok, err or "已写入 Blocked"):
        return False, steps, None

    # 6. 读回确认 (状态 + 评论)
    _, _, status2, err2 = get_project_item(repo, issue_num)
    if not step("readback_status", status2 == "Blocked",
                "读回 status=%s" % status2):
        return False, steps, None
    comments2, err = list_comments(repo, issue_num)
    if not step("readback_comment", comments2 is not None, err or "评论读回成功"):
        return False, steps, None
    cid2, _ = find_existing_blocked(comments2, key)
    if not step("readback_comment_body", cid2 is not None, "评论含 [BLOCKED]+key"):
        return False, steps, None

    receipt = {
        "repo": repo, "issue": issue_num,
        "status_before": status, "status_after": status2,
        "project_id": project_id, "item_id": item_id, "reason": reason.strip(),
        "reason_key": key,
        "blocked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return True, steps, receipt


def main():
    ap = argparse.ArgumentParser(description="worker blocked report transaction")
    ap.add_argument("--repo", required=True, help="repo name (algotradinglife/<repo>)")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--reason", required=True, help="structured blocked reason")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ok, steps, receipt = report_blocked(
        args.repo, args.issue, args.reason, args.check_only)

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
