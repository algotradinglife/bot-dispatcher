#!/usr/bin/env python3
"""post_handoff — worker 交付交接评论 ([SESSION] + HANDOFF).

在 issue 上发布结构化交接块评论:
  [SESSION] <session_id>
  [HANDOFF]
  - HEAD: <sha>
  - 已完成: <done>
  - 关键决策: <decisions>
  - 已知问题: <known_issues>
  - 下一步: <next>
发布后读回确认 (id + 正文摘要), 输出机器可读 receipt. 任一步失败 →
exit 非 0 → worker 不得宣称已交接 (fail-closed).

--check-only 模式: 只检查不写评论 — dispatcher 断链监控复用同一逻辑.

用法:
  python3 post_handoff.py --repo <repo> --issue <N> --session <id> \\
      --head <sha> --done <text> --decisions <text> --known-issues <text> \\
      --next <text> [--check-only] [--json]
"""
import argparse
import json
import subprocess
import sys
import time

SESSION_MARKER = "[SESSION]"
HANDOFF_MARKER = "[HANDOFF]"


def _gh(args, timeout=20):
    return subprocess.run(["gh"] + args, capture_output=True,
                          text=True, timeout=timeout)


def build_handoff_body(session_id, head, done, decisions, known_issues, nxt, key=None):
    """构造交接块 (机器可读 + 人类可读, REQ-E08 五要素).

    key: 幂等键 (attempt 摘要), 重试不重复发评论.
    """
    idem = "- IDEM: %s\n" % key if key else ""
    return (
        "%s %s\n"
        "%s\n"
        "%s"
        "- HEAD: %s\n"
        "- 已完成: %s\n"
        "- 关键决策: %s\n"
        "- 已知问题: %s\n"
        "- 下一步: %s\n"
        "- 返工: 按 REWORK_TYPE 分支 (method → fresh / minor → resume)"
    ) % (SESSION_MARKER, session_id, HANDOFF_MARKER, idem,
         head, done, decisions, known_issues, nxt)


def idempotency_key(session_id, head):
    """HANDOFF 幂等键: session+head 的稳定摘要."""
    import hashlib
    return hashlib.sha256(("%s|%s" % (session_id, head)).encode("utf-8")).hexdigest()[:16]


def find_handoff_by_key(comments, key):
    """按 IDEM key 精确定位 HANDOFF 评论 (防旧评论子串误匹配). Returns comment_id or None."""
    for c in reversed(comments or []):
        body = c.get("body") or ""
        if HANDOFF_MARKER in body and ("IDEM: %s" % key) in body:
            return c.get("id")
    return None


def list_comments(repo, issue_num):
    """读 issue 评论. Returns (list_of_dict, err)."""
    r = _gh(["issue", "view", str(issue_num), "--repo", "algotradinglife/" + repo,
             "--json", "comments"])
    if r.returncode != 0:
        return None, r.stderr.strip()[:120]
    try:
        return json.loads(r.stdout).get("comments", []), None
    except Exception as e:
        return None, "parse error: %s" % e


def post_handoff(repo, issue_num, session_id, head, done, decisions,
                 known_issues, nxt, check_only=False, verbose=True):
    """事务式交接评论. Returns (ok, steps, receipt)."""
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    # 1. 输入校验 (strip 后非空, REQ-E08 五要素)
    for field, val in [("session_id", session_id), ("head", head),
                       ("done", done), ("decisions", decisions),
                       ("known_issues", known_issues), ("next", nxt)]:
        if not step("input_" + field, bool((val or "").strip()), "%s 非空白" % field):
            return False, steps, None

    key = idempotency_key(session_id, head)
    body = build_handoff_body(session_id, head, done, decisions,
                              known_issues, nxt, key)

    if check_only:
        step("write_skipped", True, "--check-only 不写评论")
        return True, steps, {"mode": "check-only",
                             "would_post": "%s + %s (idem=%s)" % (SESSION_MARKER, HANDOFF_MARKER, key)}

    # 2. 幂等: 已有同 IDEM key 的评论 → 复用, 不重复发
    comments0, err = list_comments(repo, issue_num)
    if not step("read_existing", comments0 is not None, err or "现有评论读取成功"):
        return False, steps, None
    existing_id = find_handoff_by_key(comments0, key)
    if existing_id is not None:
        step("comment_created", True, "幂等复用已有评论 id=%s" % existing_id)
        receipt = {
            "repo": repo, "issue": issue_num,
            "comment_id": existing_id, "comment_url": "idempotent-reuse",
            "session_id": session_id, "head": head, "idem": key,
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return True, steps, receipt

    # 3. 发布评论 (gh issue comment <N> --body — 无 create 子命令)
    r = _gh(["issue", "comment", str(issue_num), "--repo",
             "algotradinglife/" + repo, "--body", body])
    if r.returncode != 0:
        return False, steps + [{"step": "comment_created", "ok": False,
                                "detail": "gh comment failed: %s" % r.stderr.strip()[:120]}], None
    url = (r.stdout or "").strip()
    if not url:
        return False, steps + [{"step": "comment_created", "ok": False,
                                "detail": "gh comment 无输出 (评论未创建?)"}], None
    step("comment_created", True, "url=%s" % url[:80])

    # 4. 读回确认 (存在且 IDEM key 匹配)
    comments, err = list_comments(repo, issue_num)
    if not step("readback", comments is not None, err or "评论读回成功"):
        return False, steps, None
    body_found = find_handoff_by_key(comments, key)
    if not step("readback_body", body_found is not None,
                "评论正文含 [SESSION]/[HANDOFF]/idem=%s (id=%s)" % (key, body_found)):
        return False, steps, None

    receipt = {
        "repo": repo, "issue": issue_num,
        "comment_id": body_found, "comment_url": url,
        "session_id": session_id, "head": head,
        "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return True, steps, receipt


def main():
    ap = argparse.ArgumentParser(description="post worker handoff comment")
    ap.add_argument("--repo", required=True, help="repo name (algotradinglife/<repo>)")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--session", required=True, help="worker session id")
    ap.add_argument("--head", required=True, help="current HEAD sha")
    ap.add_argument("--done", required=True, help="已完成")
    ap.add_argument("--decisions", required=True, help="关键决策")
    ap.add_argument("--known-issues", required=True, dest="known_issues", help="已知问题")
    ap.add_argument("--next", required=True, help="下一步")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ok, steps, receipt = post_handoff(
        args.repo, args.issue, args.session, args.head, args.done,
        args.decisions, args.known_issues, args.next, args.check_only)

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
