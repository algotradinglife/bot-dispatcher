#!/usr/bin/env python3
"""finalize_delivery — worker 交付事务式 finalizer (codex 审核 B 项).

把"交付完成"绑定为单一事务: 只有全部前置条件满足才写入 EV Review,
写后读回确认, 输出机器可读 receipt. 任一步失败 → exit 非 0 →
worker 不得宣称交付完成 (fail-closed).

--check-only 模式: 只检查不写状态 — dispatcher 断链监控复用同一逻辑.

用法:
  python3 finalize_delivery.py --repo <repo> --issue <N> --pr <N> \
      --expected-head <sha> [--check-only] [--json]
"""
import argparse
import json
import os
import subprocess
import sys

# worker 允许写入的状态白名单 (codex f 项: 角色感知拒绝非法转换)
WORKER_ALLOWED_FROM = {"In Progress": "EV Review"}  # 执行态 → EV Review
WORKER_BLOCKED_TO = {"Ready", "Done", "Human", "PI Review", "Inbox", "Cancelled"}


def _gh(args, timeout=20):
    return subprocess.run(["gh"] + args, capture_output=True,
                          text=True, timeout=timeout)


def _gql(query, timeout=25):
    r = subprocess.run(["gh", "api", "graphql", "-f", "query=%s" % query],
                       capture_output=True, text=True, timeout=timeout)
    return r


def get_project_item(repo, issue_num):
    """查 issue 的 project item (单 Project 校验). Returns (item_id, project_id, status_name)."""
    q = """query { repository(owner:"algotradinglife", name:"%s") {
      issue(number:%d) { projectItems(first:5) {
        nodes { id project { id } fieldValueByName(name:"Status") {
          ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }""" % (repo, issue_num)
    r = _gql(q)
    if r.returncode != 0:
        return None, None, None, "graphql error: %s" % r.stderr.strip()[:120]
    try:
        nodes = json.loads(r.stdout)["data"]["repository"]["issue"]["projectItems"]["nodes"]
    except Exception as e:
        return None, None, None, "parse error: %s" % e
    if len(nodes) != 1:
        return None, None, None, "issue 属于 %d 个 Project（必须恰好 1 个）" % len(nodes)
    n = nodes[0]
    status = None
    fv = n.get("fieldValueByName") or {}
    if fv:
        status = fv.get("name")
    return n["id"], n["project"]["id"], status, None


def get_pr_info(repo, pr_num):
    """PR 信息: state/headRefOid/closingIssues. Returns (dict, err)."""
    r = _gh(["pr", "view", str(pr_num), "--repo", "algotradinglife/" + repo,
             "--json", "state,headRefOid,isDraft,closingIssuesReferences"])
    if r.returncode != 0:
        return None, "pr view failed: %s" % r.stderr.strip()[:120]
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, "parse error: %s" % e


def set_status(project_id, item_id, field_id, option_id):
    """写入状态. Returns (ok, err)."""
    q = """mutation { updateProjectV2ItemFieldValue(input: {
      projectId: "%s", itemId: "%s", fieldId: "%s",
      value: {singleSelectOptionId: "%s"}}) { projectV2Item { id } } }""" % (
        project_id, item_id, field_id, option_id)
    r = _gql(q)
    if r.returncode != 0:
        return False, r.stderr.strip()[:120]
    if '"errors"' in r.stdout:
        return False, r.stdout[:200]
    return True, None


def get_status_field_ids(project_id):
    """查 Status 字段 ID + option IDs. Returns (field_id, {name: option_id}, err)."""
    q = """query { node(id:"%s") { ... on ProjectV2 {
      field(name:"Status") { ... on ProjectV2SingleSelectField {
        id options { id name } } } } } }""" % project_id
    r = _gql(q)
    if r.returncode != 0:
        return None, None, r.stderr.strip()[:120]
    try:
        f = json.loads(r.stdout)["data"]["node"]["field"]
        opts = {o["name"]: o["id"] for o in f["options"]}
        return f["id"], opts, None
    except Exception as e:
        return None, None, "parse error: %s" % e


def finalize(repo, issue_num, pr_num, expected_head, check_only=False,
             verbose=True):
    """事务式 finalize. Returns (ok, steps, receipt)."""
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    # 1. PR 信息 (state/head/closing)
    pr, err = get_pr_info(repo, pr_num)
    if not step("pr_read", pr is not None, err or "PR 读取成功"):
        return False, steps, None
    if not step("pr_ready", pr["state"] == "OPEN" and not pr["isDraft"],
                "state=%s draft=%s" % (pr["state"], pr["isDraft"])):
        return False, steps, None
    closing = [c.get("number") for c in pr.get("closingIssuesReferences", [])]
    if not step("pr_closes_issue", issue_num in closing,
                "closing=%s" % closing):
        return False, steps, None
    head = pr.get("headRefOid", "")
    if expected_head and not step("head_match", head == expected_head,
                                  "expected=%s actual=%s" % (expected_head[:12], head[:12])):
        return False, steps, None

    # 2. project item (单 Project + 当前状态)
    item_id, project_id, status, err = get_project_item(repo, issue_num)
    if not step("issue_project", item_id is not None, err or "project item 读取成功"):
        return False, steps, None
    if not step("issue_in_progress", status == "In Progress", "status=%s" % status):
        return False, steps, None

    # 3. 状态转换合法性 (worker 白名单)
    target = WORKER_ALLOWED_FROM.get(status)
    if not step("transition_allowed", target == "EV Review",
                "worker 只允许 In Progress→EV Review, 当前 %s" % status):
        return False, steps, None

    if check_only:
        step("write_skipped", True, "--check-only 不写状态")
        return True, steps, {"mode": "check-only", "would_set": "EV Review"}

    # 4. 写入 EV Review
    field_id, opts, err = get_status_field_ids(project_id)
    if not step("field_ids", field_id is not None, err or "Status 字段解析成功"):
        return False, steps, None
    ev_opt = opts.get("EV Review")
    if not step("ev_option_exists", ev_opt is not None, "EV Review option 存在"):
        return False, steps, None
    ok, err = set_status(project_id, item_id, field_id, ev_opt)
    if not step("write_ev_review", ok, err or "已写入 EV Review"):
        return False, steps, None

    # 5. 读回确认
    _, _, status2, err2 = get_project_item(repo, issue_num)
    if not step("readback", status2 == "EV Review",
                "读回 status=%s" % status2):
        return False, steps, None

    receipt = {
        "repo": repo, "issue": issue_num, "pr": pr_num,
        "head": head[:40], "status_before": status, "status_after": status2,
        "project_id": project_id, "item_id": item_id,
        "finalized_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   __import__("time").gmtime()),
    }
    return True, steps, receipt


def main():
    ap = argparse.ArgumentParser(description="worker delivery finalizer")
    ap.add_argument("--repo", required=True, help="repo name (algotradinglife/<repo>)")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--expected-head", default="")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ok, steps, receipt = finalize(args.repo, args.issue, args.pr,
                                  args.expected_head, args.check_only,
                                  verbose=not args.json)
    if args.json:
        out = {"status": "OK" if ok else "FAIL", "steps": steps,
               "receipt": receipt}
        print(json.dumps(out, ensure_ascii=False))
    else:
        for s in steps:
            tag = "✅" if s["ok"] else "❌"
            print("  %s %s: %s" % (tag, s["step"], s["detail"]))
        print("FINALIZE: %s" % ("PASS" if ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
