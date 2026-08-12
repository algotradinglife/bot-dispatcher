"""claim / post_handoff / report_blocked 原子脚本单元测试 (mock gh).

Fixture 对齐真实 gh 语法:
- gh issue comment <N> --body ... → stdout = 评论 URL
- gh issue view <N> --json comments → {"comments": [{"id", "body"}]}
- claim 的 [CLAIM] 评论走 gh api POST repos/.../comments → JSON {"id"}
"""
import json
import sys
from unittest import mock

sys.path.insert(0, ".")
import finalize_delivery as fd  # noqa: E402  (get_project_item 等的 _gql 在 fd 全局解析)
import claim as cl  # noqa: E402
import post_handoff as ph  # noqa: E402
import report_blocked as rb  # noqa: E402


class FakeGQL:
    def __init__(self, responses):
        self.responses = responses  # list of (stdout, code)
        self.calls = []

    def __call__(self, query, timeout=25):
        self.calls.append(query)
        idx = len(self.calls) - 1
        if idx < len(self.responses):
            out, code = self.responses[idx]
            r = mock.Mock()
            r.stdout = out
            r.stderr = ""
            r.returncode = code
            return r
        r = mock.Mock()
        r.stdout = ""
        r.stderr = "unexpected call %d" % idx
        r.returncode = 1
        return r


def _item_json(status="Ready"):
    return json.dumps({"data": {"repository": {"issue": {"projectItems": {
        "nodes": [{"id": "PVTI_item1",
                   "project": {"id": "PVT_proj1"},
                   "fieldValueByName": {"name": status}}]}}}}})


def _multi_project_json():
    nodes = [
        {"id": "PVTI_a", "project": {"id": "P1"}, "fieldValueByName": {"name": "Ready"}},
        {"id": "PVTI_b", "project": {"id": "P2"}, "fieldValueByName": {"name": "Ready"}},
    ]
    return json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}})


def _fields_json():
    return json.dumps({"data": {"node": {"field": {
        "id": "PVTSSF_f1",
        "options": [{"id": "opt_rdy", "name": "Ready"},
                    {"id": "opt_ip", "name": "In Progress"},
                    {"id": "opt_blk", "name": "Blocked"},
                    {"id": "opt_ev", "name": "EV Review"}]}}}})


def _set_json():
    return json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}})


def _comments_json(comments):
    return json.dumps({"comments": comments})


def _api_comments_json(comments):
    return json.dumps(comments)


# ─── claim ────────────────────────────────────────────────────────────────

def _claim_api_gh(initial_comments, post_result):
    """gh api mock: GET 评论列表 / POST 创建评论 (状态可变)."""
    state = list(initial_comments)
    calls = []
    def fake_gh_api(method, path, body=None, timeout=25):
        calls.append((method, path))
        r = mock.Mock()
        r.stderr = ""
        r.returncode = 0
        if method == "GET":
            r.stdout = _api_comments_json(state)
        else:
            cid = post_result.get("id")
            state.append({"id": cid, "body": body or ""})
            r.stdout = json.dumps(post_result)
        return r
    return fake_gh_api, calls


def test_claim_writes_in_progress_with_claim_comment():
    fake = FakeGQL([(_item_json("Ready"), 0), (_fields_json(), 0),
                    (_set_json(), 0), (_item_json("In Progress"), 0)])
    api, _ = _claim_api_gh([], {"id": 777})
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(cl, "_gh_api", api):
        ok, steps, receipt = cl.claim("beijing-lot", 5, "sess-1")
    assert ok, steps
    assert receipt["status_after"] == "In Progress"
    assert receipt["session_id"] == "sess-1"
    set_calls = [c for c in fake.calls if "updateProjectV2ItemFieldValue" in c]
    assert len(set_calls) == 1
    assert 'singleSelectOptionId: "opt_ip"' in set_calls[0]


def test_claim_check_only_no_write_no_comment():
    fake = FakeGQL([(_item_json("Ready"), 0), (_fields_json(), 0)])
    api_calls = []
    def fake_gh_api(*a, **k):
        api_calls.append(a)
        r = mock.Mock(); r.stdout = "{}"; r.stderr = ""; r.returncode = 0
        return r
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(cl, "_gh_api", fake_gh_api):
        ok, steps, receipt = cl.claim("beijing-lot", 5, "sess-1", check_only=True)
    assert ok, steps
    assert receipt["mode"] == "check-only"
    set_calls = [c for c in fake.calls if "updateProjectV2ItemFieldValue" in c]
    assert len(set_calls) == 0
    assert len(api_calls) == 0


def test_claim_rejects_wrong_status():
    fake = FakeGQL([(_item_json("EV Review"), 0)])
    with mock.patch.object(fd, "_gql", fake):
        ok, steps, _ = cl.claim("beijing-lot", 5, "sess-1")
    assert not ok
    assert any(not s["ok"] for s in steps)


def test_claim_rejects_multi_project():
    fake = FakeGQL([(_multi_project_json(), 0)])
    with mock.patch.object(fd, "_gql", fake):
        ok, steps, _ = cl.claim("beijing-lot", 5, "sess-1")
    assert not ok
    assert any("2 个 Project" in s["detail"] for s in steps)


def test_claim_in_progress_own_session_resumes():
    # 已 In Progress 且 [CLAIM] 是本 session → resume, 不写状态
    existing = [{"id": 1, "body": "[CLAIM] session=sess-1\n认领事务: 并发互斥依据"}]
    api, _ = _claim_api_gh(existing, {"id": 2})
    with mock.patch.object(fd, "_gql", lambda *a, **k: mock.Mock(
            returncode=0, stdout=_item_json("In Progress"), stderr="")), \
         mock.patch.object(cl, "_gh_api", api):
        ok, steps, receipt = cl.claim("beijing-lot", 5, "sess-1")
    assert ok, steps
    assert receipt["mode"] == "resume"
    assert receipt["claim_owner"] == "sess-1"


def test_claim_in_progress_other_session_rejects():
    # 已 In Progress 但 [CLAIM] 是别的 session → fail-closed
    existing = [{"id": 1, "body": "[CLAIM] session=other-session\n认领事务"}]
    api, _ = _claim_api_gh(existing, {"id": 2})
    with mock.patch.object(fd, "_gql", lambda *a, **k: mock.Mock(
            returncode=0, stdout=_item_json("In Progress"), stderr="")), \
         mock.patch.object(cl, "_gh_api", api):
        ok, steps, _ = cl.claim("beijing-lot", 5, "sess-1")
    assert not ok
    assert any("不一致拒绝续跑" in s["detail"] for s in steps)


def test_claim_in_progress_no_claim_comment_rejects():
    # 已 In Progress 但无 [CLAIM] 评论 (旧数据/他人认领) → fail-closed
    api, _ = _claim_api_gh([], {"id": 2})
    with mock.patch.object(fd, "_gql", lambda *a, **k: mock.Mock(
            returncode=0, stdout=_item_json("In Progress"), stderr="")), \
         mock.patch.object(cl, "_gh_api", api):
        ok, steps, _ = cl.claim("beijing-lot", 5, "sess-1")
    assert not ok
    assert any("不一致拒绝续跑" in s["detail"] for s in steps)


def test_claim_readback_failure_fails():
    # 写入后读回仍是 Ready (写失败) → 拒绝
    fake = FakeGQL([(_item_json("Ready"), 0), (_fields_json(), 0),
                    (_set_json(), 0), (_item_json("Ready"), 0)])
    api, _ = _claim_api_gh([], {"id": 777})
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(cl, "_gh_api", api):
        ok, steps, _ = cl.claim("beijing-lot", 5, "sess-1")
    assert not ok
    assert any("readback_status" == s["step"] and not s["ok"] for s in steps)


def test_claim_empty_session_fails():
    with mock.patch.object(fd, "_gql", lambda *a, **k: mock.Mock(
            returncode=0, stdout="{}", stderr="")):
        ok, steps, _ = cl.claim("beijing-lot", 5, "")
    assert not ok


# ─── post_handoff ────────────────────────────────────────────────────────

HANDOFF_BODY = ("[SESSION] sess-1\n[HANDOFF]\n- IDEM: %s\n- HEAD: %s\n- 已完成: 统计板块\n"
                "- 关键决策: 用中位数\n- 已知问题: 无\n- 下一步: 特征\n"
                "- 返工: 按 REWORK_TYPE 分支 (method → fresh / minor → resume)")


def _handoff_body(key, head):
    return HANDOFF_BODY % (key, head)


def test_handoff_posts_and_reads_back():
    gh_calls = []
    key = ph.idempotency_key("sess-1", "a" * 40)
    state = []  # 初始无评论 → 走发布路径
    def fake_gh(args, timeout=20):
        gh_calls.append(args)
        r = mock.Mock()
        if args[:2] == ["issue", "comment"]:
            body = args[args.index("--body") + 1]
            state.append({"id": 123, "body": body})
            r.stdout = "https://github.com/algotradinglife/beijing-lot/issues/5#issuecomment-123"
            r.stderr = ""
            r.returncode = 0
        elif args[:2] == ["issue", "view"]:
            r.stdout = _comments_json(list(state))
            r.stderr = ""
            r.returncode = 0
        else:
            r.stdout = "{}"
            r.stderr = "unexpected"
            r.returncode = 1
        return r
    with mock.patch.object(ph, "_gh", fake_gh):
        ok, steps, receipt = ph.post_handoff(
            "beijing-lot", 5, "sess-1", "a" * 40,
            "统计板块", "用中位数", "无", "特征")
    assert ok, steps
    assert receipt["comment_id"] == 123
    # 真实命令形态: gh issue comment <N> --repo ... --body (无 create 子命令)
    create_call = [a for a in gh_calls if a[:2] == ["issue", "comment"]][0]
    assert create_call[2] == "5" and "--body" in create_call
    # 读回按 IDEM key 定位: 发出去的 body 含 key
    created_body = create_call[create_call.index("--body") + 1]
    assert "IDEM: %s" % key in created_body


def test_handoff_readback_missing_body_fails():
    def fake_gh(args, timeout=20):
        r = mock.Mock()
        if args[:2] == ["issue", "comment"]:
            r.stdout = "https://x/issuecomment-999"
            r.stderr = ""
            r.returncode = 0
        elif args[:2] == ["issue", "view"]:
            # 读回正文缺 [SESSION]/head → 失败
            r.stdout = _comments_json([{"id": 999, "body": "not a handoff"}])
            r.stderr = ""
            r.returncode = 0
        else:
            r.stdout = "{}"; r.stderr = ""; r.returncode = 1
        return r
    with mock.patch.object(ph, "_gh", fake_gh):
        ok, steps, _ = ph.post_handoff(
            "beijing-lot", 5, "sess-1", "a" * 40,
            "done", "dec", "ki", "next")
    assert not ok
    assert any("readback_body" == s["step"] and not s["ok"] for s in steps)


def test_handoff_comment_failure_fails():
    def fake_gh(args, timeout=20):
        r = mock.Mock()
        r.stdout = ""
        r.stderr = "some gh error"
        r.returncode = 1
        return r
    with mock.patch.object(ph, "_gh", fake_gh):
        ok, steps, _ = ph.post_handoff(
            "beijing-lot", 5, "sess-1", "a" * 40,
            "done", "dec", "ki", "next")
    assert not ok
    assert any(not s["ok"] for s in steps)


def test_handoff_check_only_no_post():
    gh_calls = []
    with mock.patch.object(ph, "_gh", lambda *a, **k: gh_calls.append(a) or mock.Mock(
            returncode=0, stdout="", stderr="")):
        ok, steps, receipt = ph.post_handoff(
            "beijing-lot", 5, "s1", "a" * 40, "d", "dec", "ki", "n",
            check_only=True)
    assert ok, steps
    assert receipt["mode"] == "check-only"
    assert len(gh_calls) == 0


def test_handoff_blank_input_fails():
    with mock.patch.object(ph, "_gh", lambda *a, **k: mock.Mock(
            returncode=0, stdout="", stderr="")):
        ok, steps, _ = ph.post_handoff(
            "beijing-lot", 5, "s1", "a" * 40, "  ", "dec", "ki", "n")
    assert not ok
    assert any("input_done" == s["step"] and not s["ok"] for s in steps)


def test_handoff_idempotent_reuse():
    """同 session+head 重试: 复用已有评论, 不重复发."""
    key = ph.idempotency_key("sess-1", "a" * 40)
    existing_body = "[SESSION] sess-1\n[HANDOFF]\n- IDEM: %s\n- HEAD: %s" % (key, "a" * 40)
    gh_calls = []
    def fake_gh(args, timeout=20):
        gh_calls.append(args)
        r = mock.Mock()
        if args[:2] == ["issue", "view"]:
            r.stdout = _comments_json([{"id": 555, "body": existing_body}])
            r.stderr = ""
            r.returncode = 0
        else:
            r.stdout = ""; r.stderr = ""; r.returncode = 1
        return r
    with mock.patch.object(ph, "_gh", fake_gh):
        ok, steps, receipt = ph.post_handoff(
            "beijing-lot", 5, "sess-1", "a" * 40,
            "done", "dec", "ki", "next")
    assert ok, steps
    assert receipt["comment_id"] == 555
    assert receipt["comment_url"] == "idempotent-reuse"
    # 只读不写 (无 comment 调用)
    assert not any(a[:2] == ["issue", "comment"] for a in gh_calls)


def test_handoff_old_comment_no_false_match():
    """旧评论含 [SESSION]/[HANDOFF] 但无本 IDEM key → 不误匹配, 发新评论."""
    old_body = "[SESSION] other-session\n[HANDOFF]\n- HEAD: %s" % ("b" * 40)
    key = ph.idempotency_key("sess-1", "a" * 40)
    gh_calls = []
    state = [{"id": 111, "body": old_body}]
    def fake_gh(args, timeout=20):
        gh_calls.append(args)
        r = mock.Mock()
        if args[:2] == ["issue", "comment"]:
            new_body = args[args.index("--body") + 1]
            state.append({"id": 222, "body": new_body})
            r.stdout = "https://x/issuecomment-222"
            r.stderr = ""
            r.returncode = 0
        elif args[:2] == ["issue", "view"]:
            r.stdout = _comments_json(list(state))
            r.stderr = ""
            r.returncode = 0
        else:
            r.stdout = "{}"; r.stderr = ""; r.returncode = 1
        return r
    with mock.patch.object(ph, "_gh", fake_gh):
        ok, steps, receipt = ph.post_handoff(
            "beijing-lot", 5, "sess-1", "a" * 40,
            "done", "dec", "ki", "next")
    assert ok, steps
    assert receipt["comment_id"] == 222
    # 确实发了新评论 (IDEM: key 在 body 中)
    created = [a for a in gh_calls if a[:2] == ["issue", "comment"]]
    assert len(created) == 1
    assert "IDEM: %s" % key in created[0][created[0].index("--body") + 1]


# ─── report_blocked ──────────────────────────────────────────────────────

def _blocked_gh(initial_comments):
    """gh mock: issue comment 发评论 / issue view 读评论 (状态可变)."""
    state = list(initial_comments)
    def fake_gh(args, timeout=20):
        r = mock.Mock()
        if args[:2] == ["issue", "comment"]:
            body = args[args.index("--body") + 1] if "--body" in args else ""
            state.append({"id": 42, "body": body})
            r.stdout = "https://x/issuecomment-42"
            r.stderr = ""
            r.returncode = 0
        elif args[:2] == ["issue", "view"]:
            r.stdout = _comments_json(state)
            r.stderr = ""
            r.returncode = 0
        else:
            r.stdout = "{}"; r.stderr = ""; r.returncode = 1
        return r
    return fake_gh


def test_blocked_writes_and_comments():
    fake = FakeGQL([(_item_json("In Progress"), 0), (_fields_json(), 0),
                    (_set_json(), 0), (_item_json("Blocked"), 0)])
    gh = _blocked_gh([])  # 无现有 [BLOCKED] → 发新评论
    with mock.patch.object(fd, "_gql", fake), mock.patch.object(rb, "_gh", gh):
        ok, steps, receipt = rb.report_blocked("beijing-lot", 5, "需要 PI 决策")
    assert ok, steps
    assert receipt["status_after"] == "Blocked"
    set_calls = [c for c in fake.calls if "updateProjectV2ItemFieldValue" in c]
    assert len(set_calls) == 1
    assert 'singleSelectOptionId: "opt_blk"' in set_calls[0]


def test_blocked_comment_before_status_write():
    """评论必须先于状态写 (评论失败 → 状态不动, 无半事务)."""
    fake = FakeGQL([(_item_json("In Progress"), 0), (_fields_json(), 0)])
    # 评论失败
    def fake_gh(args, timeout=20):
        r = mock.Mock()
        if args[:2] == ["issue", "view"]:
            r.stdout = _comments_json([]); r.stderr = ""; r.returncode = 0
        else:
            r.stdout = ""; r.stderr = "comment failed"; r.returncode = 1
        return r
    with mock.patch.object(fd, "_gql", fake), mock.patch.object(rb, "_gh", fake_gh):
        ok, steps, _ = rb.report_blocked("beijing-lot", 5, "需要 PI 决策")
    assert not ok
    set_calls = [c for c in fake.calls if "updateProjectV2ItemFieldValue" in c]
    assert len(set_calls) == 0  # 状态未被写


def test_blocked_rejects_wrong_status():
    fake = FakeGQL([(_item_json("EV Review"), 0)])
    with mock.patch.object(fd, "_gql", fake):
        ok, steps, _ = rb.report_blocked("beijing-lot", 5, "x")
    assert not ok
    assert any("In Progress→Blocked" in s["detail"] for s in steps)


def test_blocked_check_only_no_write():
    fake = FakeGQL([(_item_json("In Progress"), 0), (_fields_json(), 0)])
    gh_calls = []
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(rb, "_gh", lambda *a, **k: gh_calls.append(a) or mock.Mock(
             returncode=0, stdout="", stderr="")):
        ok, steps, receipt = rb.report_blocked("beijing-lot", 5, "x", check_only=True)
    assert ok, steps
    assert receipt["mode"] == "check-only"
    set_calls = [c for c in fake.calls if "updateProjectV2ItemFieldValue" in c]
    assert len(set_calls) == 0


def test_blocked_empty_reason_fails():
    with mock.patch.object(fd, "_gql", lambda *a, **k: mock.Mock(
            returncode=0, stdout="{}", stderr="")):
        ok, steps, _ = rb.report_blocked("beijing-lot", 5, "   ")
    assert not ok
    assert any("reason" == s["step"] and not s["ok"] for s in steps)


def test_blocked_readback_failure_fails():
    # 写入 Blocked 后读回失败 (仍是 In Progress) → 拒绝
    fake = FakeGQL([(_item_json("In Progress"), 0), (_fields_json(), 0),
                    (_set_json(), 0), (_item_json("In Progress"), 0)])
    gh = _blocked_gh([])
    with mock.patch.object(fd, "_gql", fake), mock.patch.object(rb, "_gh", gh):
        ok, steps, _ = rb.report_blocked("beijing-lot", 5, "需要 PI 决策")
    assert not ok
    assert any("readback_status" == s["step"] and not s["ok"] for s in steps)
