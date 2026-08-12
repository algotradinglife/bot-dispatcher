"""kanban_bridge D 项测试: RESUME_SESSION 注入尊重 REWORK_TYPE.

回归场景 (2026-08-12 规则):
- method REJECT → 不注入 RESUME_SESSION (fresh), prompt 带 FRESH 指令
- minor REJECT → 注入 RESUME_SESSION (resume 省时)
- 首次 (无裁决) → 无 SESSION 可注入
- PASS 之后 → 不是返工, 不注入
- REJECT 无 REWORK_TYPE 标记 → 保守 method (fresh)
- 卡已存在 (返工轮) → 卡评论传递 REWORK_TYPE 决策
- GitHub 读取失败 → ControlPlaneUnavailable (fail-closed)
- 多个 [SESSION] → 取最新
"""
import json
import sys
from unittest import mock

sys.path.insert(0, ".")
import kanban_bridge as kb


def _gh_comments(comments):
    r = mock.Mock()
    r.returncode = 0
    r.stdout = json.dumps({"comments": comments})
    r.stderr = ""
    return r


def _gh_fail():
    r = mock.Mock()
    r.returncode = 1
    r.stdout = ""
    r.stderr = "rate limit"
    return r


def _comment(body, author="everything-bot-engineer", created_at="2026-08-13T00:00:00Z"):
    return {"id": 1, "body": body,
            "author": {"login": author}, "createdAt": created_at}


def _ev_block(verdict, rw=None, ts="2026-08-13T00:00:00Z"):
    extra = "rework_type=%s\n" % rw if rw else ""
    return ("[EV-VERDICT]\nauditor=everything-bot-engineer\n"
            "sha=%s\nverdict=%s\ntimestamp=%s\n%s[/EV-VERDICT]"
            % ("a" * 40, verdict, ts, extra))


# ─── get_rework_type ─────────────────────────────────────────────────────

def test_rework_type_method():
    c = [_comment(_ev_block("REJECT", "method"))]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        rw, sha = kb.get_rework_type(5)
        assert rw == "method" and len(sha) == 40


def test_rework_type_minor():
    c = [_comment(_ev_block("REJECT", "minor"))]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        rw, sha = kb.get_rework_type(5)
        assert rw == "minor" and len(sha) == 40


def test_rework_type_reject_no_marker_conservative_method():
    c = [_comment(_ev_block("REJECT"))]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        rw, _ = kb.get_rework_type(5)
        assert rw == "method"  # 保守


def test_rework_type_pass_returns_none():
    c = [_comment(_ev_block("PASS"))]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.get_rework_type(5) is None


def test_rework_type_no_comments_none():
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments([])):
        assert kb.get_rework_type(5) is None


def test_rework_type_latest_verdict_wins():
    # 旧 REJECT (method) → 新 PASS → 取新 (不是返工)
    c = [_comment(_ev_block("REJECT", "method", ts="2026-08-13T00:00:00Z"),
                  created_at="2026-08-13T00:00:00Z"),
         _comment(_ev_block("PASS", ts="2026-08-13T02:00:00Z"),
                  created_at="2026-08-13T02:00:00Z")]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.get_rework_type(5) is None


def test_rework_type_latest_reject_wins():
    # 旧 PASS → 新 REJECT (minor) → 取新
    c = [_comment(_ev_block("PASS", ts="2026-08-13T00:00:00Z"),
                  created_at="2026-08-13T00:00:00Z"),
         _comment(_ev_block("REJECT", "minor", ts="2026-08-13T02:00:00Z"),
                  created_at="2026-08-13T02:00:00Z")]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        rw, _ = kb.get_rework_type(5)
        assert rw == "minor"


def test_rework_type_worker_comment_ignored():
    """worker 普通评论含 REJECT/PASS 字样 → 不误判 (非 auditor + 无 EV-VERDICT 块)."""
    c = [_comment("fixed all REJECT findings, unit tests PASS",
                  author="everything-bot-engineer")]
    # 同一 bot 账号既是 auditor 又是 worker — 但无 [EV-VERDICT] 块 → parse 不到
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.get_rework_type(5) is None


def test_rework_type_non_auditor_comment_ignored():
    """非可信 auditor 的评论即使有 EV-VERDICT 块也忽略 (PI 不是 auditor)."""
    c = [_comment(_ev_block("REJECT", "minor"), author="hh1985")]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.get_rework_type(5) is None


def test_rework_type_read_failure_raises():
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_fail()):
        try:
            kb.get_rework_type(5)
            assert False, "should raise"
        except kb.ControlPlaneUnavailable:
            pass


# ─── get_session_id ──────────────────────────────────────────────────────

def test_session_id_newest_wins():
    c = [_comment("[SESSION] old-session\n[HANDOFF] 第一轮"),
         _comment("[SESSION] newest-session\n[HANDOFF] 第二轮")]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.get_session_id(5) == "newest-session"


def test_session_id_none_when_no_session():
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments([])):
        assert kb.get_session_id(5) is None


def test_session_id_untrusted_author_ignored():
    """非可信作者 (PI hh1985) 的 [SESSION] 评论被忽略."""
    c = [_comment("[SESSION] pi-fake-session\n[HANDOFF] PI 不该发这个", author="hh1985")]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.get_session_id(5) is None


def test_session_id_trusted_wins_over_untrusted():
    """可信 worker 的旧 [SESSION] 不被 PI 的新评论覆盖 (取可信最新)."""
    c = [_comment("[SESSION] worker-session\n[HANDOFF] 真实交付", author="everything-bot-engineer"),
         _comment("[SESSION] pi-fake-session\n[HANDOFF] PI 干扰", author="hh1985")]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.get_session_id(5) == "worker-session"


def test_session_id_read_failure_raises():
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_fail()):
        try:
            kb.get_session_id(5)
            assert False, "should raise"
        except kb.ControlPlaneUnavailable:
            pass


# ─── exec_card_prompt ────────────────────────────────────────────────────

def _gh_view(comments):
    def fake_run(cmd, timeout=30):
        if cmd[:3] == ["gh", "issue", "view"]:
            return _gh_comments(comments)
        r = mock.Mock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r
    return fake_run


def test_prompt_first_round_no_session():
    """首次 Ready: 无 EV 裁决 → 无 RESUME_SESSION 注入."""
    with mock.patch.object(kb, "run", _gh_view([])):
        p = kb.exec_card_prompt(5, "研究 X", "https://x", "analyst")
    assert "RESUME_SESSION=" not in p
    assert "REWORK_TYPE=method" not in p


def test_prompt_method_reject_fresh_no_resume():
    """method REJECT: 不注入 RESUME_SESSION, 带 FRESH 指令 (即使有 [SESSION])."""
    c = [_comment(_ev_block("REJECT", "method")),
         _comment("[SESSION] sess-abc\n[HANDOFF] 完成统计板块")]
    with mock.patch.object(kb, "run", _gh_view(c)):
        p = kb.exec_card_prompt(5, "研究 X", "https://x", "analyst")
    assert "RESUME_SESSION=" not in p
    assert "REWORK_TYPE=method" in p
    assert "FRESH" in p


def test_prompt_minor_reject_resumes():
    """minor REJECT: 注入 RESUME_SESSION (resume 省时)."""
    c = [_comment(_ev_block("REJECT", "minor")),
         _comment("[SESSION] sess-abc\n[HANDOFF] 完成统计板块")]
    with mock.patch.object(kb, "run", _gh_view(c)):
        p = kb.exec_card_prompt(5, "研究 X", "https://x", "analyst")
    assert "RESUME_SESSION=sess-abc" in p
    assert "REWORK_TYPE=method" not in p


def test_prompt_pass_after_rework_no_resume():
    """PASS 之后 (非返工): 不注入."""
    c = [_comment(_ev_block("PASS")),
         _comment("[SESSION] sess-abc\n[HANDOFF] 完成")]
    with mock.patch.object(kb, "run", _gh_view(c)):
        p = kb.exec_card_prompt(5, "研究 X", "https://x", "analyst")
    assert "RESUME_SESSION=" not in p


def test_prompt_reject_no_marker_fresh():
    """REJECT 无 REWORK_TYPE 标记 → 保守 method (fresh)."""
    c = [_comment(_ev_block("REJECT")),
         _comment("[SESSION] sess-abc")]
    with mock.patch.object(kb, "run", _gh_view(c)):
        p = kb.exec_card_prompt(5, "研究 X", "https://x", "analyst")
    assert "RESUME_SESSION=" not in p
    assert "REWORK_TYPE=method" in p


def test_prompt_read_failure_fresh():
    """GitHub 读取失败 → fail-closed: 不注入 (宁可 fresh 不可错 resume)."""
    def fake_run(cmd, timeout=30):
        if cmd[:3] == ["gh", "issue", "view"]:
            return _gh_fail()
        r = mock.Mock()
        r.returncode = 0
        r.stdout = ""
        r.stderr = ""
        return r
    with mock.patch.object(kb, "run", fake_run):
        p = kb.exec_card_prompt(5, "研究 X", "https://x", "analyst")
    assert "RESUME_SESSION=" not in p


# ─── rework_directive / handle 返工评论 ──────────────────────────────────

def test_rework_directive_method():
    c = [_comment(_ev_block("REJECT", "method"))]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        d = kb.rework_directive(5)
    assert d and "REWORK_TYPE=method" in d and "FRESH" in d


def test_rework_directive_minor():
    c = [_comment(_ev_block("REJECT", "minor")),
         _comment("[SESSION] sess-abc\n[HANDOFF] 完成统计板块")]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        d = kb.rework_directive(5)
    assert d and "REWORK_TYPE=minor" in d and "RESUME_SESSION=sess-abc" in d


def test_rework_directive_minor_no_session_falls_back_fresh():
    """minor 返工但读不到 [SESSION] → fail-closed 降级 FRESH."""
    c = [_comment(_ev_block("REJECT", "minor"))]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        d = kb.rework_directive(5)
    assert d and "REWORK_TYPE=method" in d and "FRESH" in d


def test_rework_directive_none_when_pass():
    c = [_comment(_ev_block("PASS"))]
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_comments(c)):
        assert kb.rework_directive(5) is None


def test_rework_directive_read_failure_raises():
    with mock.patch.object(kb, "run", lambda *a, **k: _gh_fail()):
        try:
            kb.rework_directive(5)
            assert False, "should raise"
        except kb.ControlPlaneUnavailable:
            pass


def test_handle_rework_comments_existing_card():
    """返工轮 (卡已存在 + blocked + method REJECT) → unblock + 追加 REWORK_TYPE 评论."""
    action = {"reason": "issue_ready", "node": "project:1:5", "state": "Ready",
              "role": "analyst"}
    calls = []
    def fake_list():
        return [{"id": "exec-1", "title": "Issue #5 [EXEC_CARD GENERATION=1]",
                 "body": "EXEC_CARD GENERATION=1", "status": "blocked"}]
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _gh_comments([_comment(_ev_block("REJECT", "method"))])
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "list":
            return mock.Mock(returncode=0, stdout=json.dumps(fake_list()), stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "show":
            return mock.Mock(returncode=0, stdout=json.dumps({"comments": []}), stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "comment":
            return mock.Mock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "unblock":
            return mock.Mock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "create":
            return mock.Mock(returncode=0, stdout=json.dumps({"id": "new-1"}), stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(kb, "run", fake_run), \
         mock.patch.object(kb, "_issue_status_allows_rework", return_value=True):
        kb.handle(action)
    comments = [c for c in calls if c[:3] == ["hermes", "kanban", "comment"]]
    unblocks = [c for c in calls if c[:3] == ["hermes", "kanban", "unblock"]]
    assert len(comments) == 1
    assert comments[0][3] == "exec-1"          # card id
    assert "REWORK_TYPE=method" in comments[0][4]  # directive text
    assert len(unblocks) == 1                   # blocked 卡被 unblock (worker 可 claim)
    assert unblocks[0][3] == "exec-1"


def test_handle_rework_comment_idempotent():
    """返工评论幂等: 卡已有同 REWORK_ID 评论 → 不重复; 新 sha 轮次 → 追加."""
    action = {"reason": "issue_ready", "node": "project:1:5", "state": "Ready",
              "role": "analyst"}
    calls = []
    def fake_list():
        return [{"id": "exec-1", "title": "Issue #5 [EXEC_CARD GENERATION=1]",
                 "body": "EXEC_CARD GENERATION=1", "status": "blocked"}]
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _gh_comments([_comment(_ev_block("REJECT", "method"))])
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "list":
            return mock.Mock(returncode=0, stdout=json.dumps(fake_list()), stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "show":
            # 已有同 sha 的 REWORK_ID 评论 → 幂等跳过 (不重复追加)
            sha = "a" * 40
            return mock.Mock(returncode=0,
                             stdout=json.dumps({"comments": [{"body": "REWORK_ID=%s\nREWORK_TYPE=method ..." % sha}]}),
                             stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "unblock":
            return mock.Mock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "create":
            return mock.Mock(returncode=0, stdout=json.dumps({"id": "new-1"}), stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(kb, "run", fake_run), \
         mock.patch.object(kb, "_issue_status_allows_rework", return_value=True):
        kb.handle(action)
    comments = [c for c in calls if c[:3] == ["hermes", "kanban", "comment"]]
    assert len(comments) == 0  # 同 REWORK_ID 已存在 → 不重复
    # 但 unblock 仍应执行 (指令已就位, 只是幂等跳过追加)
    unblocks = [c for c in calls if c[:3] == ["hermes", "kanban", "unblock"]]
    assert len(unblocks) == 1


def test_comment_card_new_sha_appends():
    """第二轮返工 (新 sha): 旧 REWORK_ID 评论不抑制新指令."""
    card_id = "exec-1"
    old_sha, new_sha = "b" * 40, "c" * 40
    calls = []
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["hermes", "kanban", "show"]:
            return mock.Mock(returncode=0,
                             stdout=json.dumps({"comments": [{"body": "REWORK_ID=%s\nREWORK_TYPE=method" % old_sha}]}),
                             stderr="")
        if cmd[:3] == ["hermes", "kanban", "comment"]:
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(kb, "run", fake_run):
        ok = kb.comment_card(card_id, "REWORK_ID=%s\nREWORK_TYPE=minor" % new_sha, new_sha)
    assert ok
    comments = [c for c in calls if c[:3] == ["hermes", "kanban", "comment"]]
    assert len(comments) == 1  # 新 sha → 追加


def test_comment_card_41char_id_prefix_no_false_match():
    """41 位十六进制 REWORK_ID 不得前缀误命中 40 位 key (需右边界)."""
    card_id = "exec-1"
    key = "c" * 40
    calls = []
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["hermes", "kanban", "show"]:
            # 评论里是 41 位 (key + 额外一个 hex 字符) → 不是 key 本身
            return mock.Mock(returncode=0,
                             stdout=json.dumps({"comments": [{"body": "REWORK_ID=%s\nREWORK_TYPE=method" % (key + "f")}]}),
                             stderr="")
        if cmd[:3] == ["hermes", "kanban", "comment"]:
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(kb, "run", fake_run):
        ok = kb.comment_card(card_id, "REWORK_ID=%s\nREWORK_TYPE=minor" % key, key)
    assert ok
    comments = [c for c in calls if c[:3] == ["hermes", "kanban", "comment"]]
    assert len(comments) == 1  # 41 位不匹配 → 追加 (不误判幂等)


def test_handle_rework_comment_fail_no_unblock():
    """评论失败 → 不 unblock (worker 不会在无指令时启动)."""
    action = {"reason": "issue_ready", "node": "project:1:5", "state": "Ready",
              "role": "analyst"}
    calls = []
    def fake_list():
        return [{"id": "exec-1", "title": "Issue #5 [EXEC_CARD GENERATION=1]",
                 "body": "EXEC_CARD GENERATION=1", "status": "blocked"}]
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _gh_comments([_comment(_ev_block("REJECT", "method"))])
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "list":
            return mock.Mock(returncode=0, stdout=json.dumps(fake_list()), stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "show":
            return mock.Mock(returncode=0, stdout=json.dumps({"comments": []}), stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "comment":
            return mock.Mock(returncode=1, stdout="", stderr="comment failed")  # 失败
        return mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(kb, "run", fake_run):
        kb.handle(action)
    unblocks = [c for c in calls if c[:3] == ["hermes", "kanban", "unblock"]]
    assert len(unblocks) == 0  # 评论失败 → 保持 blocked


def test_handle_rework_unblock_checks_issue_status():
    """unblock 前 CAS 确认: GitHub 已 EV Review → 不 unblock (跨 tick 竞态防)."""
    action = {"reason": "issue_ready", "node": "project:1:5", "state": "Ready",
              "role": "analyst"}
    calls = []
    gh_views = {"n": 0}
    def fake_list():
        return [{"id": "exec-1", "title": "Issue #5 [EXEC_CARD GENERATION=1]",
                 "body": "EXEC_CARD GENERATION=1", "status": "blocked"}]
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            gh_views["n"] += 1
            # 第一次 gh issue view = 返工裁决读取; 之后 = CAS 重读
            if gh_views["n"] == 1:
                return _gh_comments([_comment(_ev_block("REJECT", "method"))])
            return _gh_comments([])  # CAS 重读 (实际走 get_project_item, 被 mock 覆盖)
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "list":
            return mock.Mock(returncode=0, stdout=json.dumps(fake_list()), stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "show":
            return mock.Mock(returncode=0, stdout=json.dumps({"comments": []}), stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "comment":
            return mock.Mock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "unblock":
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    # _issue_status_allows_rework 内部调 get_project_item (GraphQL) → mock
    with mock.patch.object(kb, "run", fake_run), \
         mock.patch.object(kb, "_issue_status_allows_rework", return_value=False):
        kb.handle(action)
    unblocks = [c for c in calls if c[:3] == ["hermes", "kanban", "unblock"]]
    assert len(unblocks) == 0  # CAS 确认失败 → 不 unblock


def test_prompt_second_read_failure_fresh():
    """第一次读取成功 (minor) + 第二次 session 读取失败 → 降级 FRESH prompt."""
    calls = []
    gh_views = {"n": 0}
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            gh_views["n"] += 1
            if gh_views["n"] == 1:
                return _gh_comments([_comment(_ev_block("REJECT", "minor"))])
            return _gh_fail()  # 第二次 (get_session_id) 失败
        return mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(kb, "run", fake_run):
        p = kb.exec_card_prompt(5, "研究 X", "https://x", "analyst")
    assert "RESUME_SESSION=" not in p
    assert "REWORK_TYPE=method" in p and "FRESH" in p


def test_handle_first_round_no_comment():
    """首次 Ready (无裁决): 不追加 REWORK_TYPE 评论 (exec_card_prompt 已处理)."""
    action = {"reason": "issue_ready", "node": "project:1:5", "state": "Ready",
              "role": "analyst"}
    calls = []
    def fake_run(cmd, timeout=30):
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _gh_comments([])
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "list":
            return mock.Mock(returncode=0, stdout="[]", stderr="")
        if cmd[:2] == ["hermes", "kanban"] and cmd[2] == "create":
            return mock.Mock(returncode=0, stdout=json.dumps({"id": "new-1"}), stderr="")
        return mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(kb, "run", fake_run):
        kb.handle(action)
    comments = [c for c in calls if c[:3] == ["hermes", "kanban", "comment"]]
    assert len(comments) == 0
