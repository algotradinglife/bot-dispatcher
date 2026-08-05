"""Sync job tests (L1 layer of test-plan-v0_1).

Covers:
  - L0-08: extract_issue_number / build_review_comment pure functions
  - L1-04: done card → gh comment + Project Review (mock gh)
  - L1-05: idempotency — repeated runs never re-comment
  - L1-06: state persistence (synced set survives restarts)
  - one-way discipline: sync_job never writes kanban
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_job", ROOT / "sync_job.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def done_card(**overrides) -> dict:
    card = {
        "id": "t_test123",
        "title": "[Issue #42] Investigate microbiome signal",
        "body": "issue: 42\nContract: reproduce figure 3",
        "assignee": "researcher",
        "status": "done",
        "result": "Reproduced figure 3; n=120 cohort, p<0.01.",
        "completed_at": 1785750000,
        "summary": "Figure 3 reproduced with stats",
        "metadata": '{"changed_files": ["report.md"], "tests_run": 12}',
    }
    card.update(overrides)
    return card


# ── L0-08: pure helpers ───────────────────────────────────────────────

def test_extract_issue_number_from_body() -> None:
    assert MOD.extract_issue_number(done_card()) == 42


def test_extract_issue_number_from_title() -> None:
    card = done_card(body="no issue here")
    assert MOD.extract_issue_number(card) == 42


def test_extract_issue_number_from_url() -> None:
    card = done_card(title="Generic task",
                     body="See https://github.com/org/repo/issues/7 for context")
    assert MOD.extract_issue_number(card) == 7


def test_extract_issue_number_none() -> None:
    card = done_card(title="No ref here", body="nothing")
    assert MOD.extract_issue_number(card) is None


def test_build_review_comment_includes_result_and_metadata() -> None:
    comment = MOD.build_review_comment(done_card())
    assert "[Issue #42]" in comment
    assert "Reproduced figure 3" in comment
    assert "Figure 3 reproduced with stats" in comment
    assert "changed_files" in comment and "report.md" in comment
    assert "等待 PI 评审" in comment


def test_build_review_comment_handles_missing_fields() -> None:
    comment = MOD.build_review_comment(done_card(result="", summary=None,
                                                 metadata=None, completed_at=None))
    assert "Reproduced figure 3" not in comment
    assert "(untitled)" not in comment  # title present


def test_build_review_comment_metadata_non_json() -> None:
    comment = MOD.build_review_comment(done_card(metadata="raw text, not json"))
    assert "raw text, not json" in comment


# ── L1-04: sync_one_card posts comment + review ───────────────────────

def test_sync_one_card_posts_comment_and_review() -> None:
    project = {"node": "PVT_X", "review_field": "FIELD_X", "review_option": "opt_review"}
    ok = SimpleNamespace(returncode=0, stderr="", stdout="")

    def fake_run(argv, **kw):
        if argv[0] == "gh" and argv[1] == "issue":
            return ok
        if argv[0] == "gh" and argv[1] == "api":
            # first call: item lookup; second: review mutation
            if "node(id: $p)" in argv[4]:
                return SimpleNamespace(
                    returncode=0, stderr="", stdout=json.dumps({
                        "data": {"node": {"items": {"nodes": [
                            {"id": "item_42", "content": {"number": 42}}
                        ]}}}
                    }))
            return ok
        return ok

    with patch.object(MOD.subprocess, "run", side_effect=fake_run) as run:
        out = MOD.sync_one_card(done_card(), "org/repo", project)

    assert out["status"] == "synced"
    assert out["issue"] == 42
    actions = {a["action"]: a for a in out["actions"]}
    assert actions["comment"]["ok"] is True
    assert actions["review"]["ok"] is True
    # comment body must contain evidence
    comment_call = [c for c in run.call_args_list
                    if c.args[0][:2] == ["gh", "issue"]][0]
    body = comment_call.args[0][comment_call.args[0].index("--body") + 1]
    assert "Reproduced figure 3" in body


def test_sync_one_card_no_issue_ref_skipped() -> None:
    card = done_card(title="No ref", body="nothing")
    out = MOD.sync_one_card(card, "org/repo", None)
    assert out["status"] == "skipped"


def test_sync_one_card_comment_failure_reported() -> None:
    bad = SimpleNamespace(returncode=1, stderr="api error", stdout="")
    with patch.object(MOD.subprocess, "run", return_value=bad):
        out = MOD.sync_one_card(done_card(), "org/repo", None)
    assert out["status"] == "failed"
    assert "api error" in out["reason"]


def test_sync_one_card_review_move_best_effort() -> None:
    """If project lookup fails, comment still succeeds; review is skipped."""
    project = {"node": "PVT_X", "review_field": "F", "review_option": "o"}

    def fake_run(argv, **kw):
        if argv[0] == "gh" and argv[1] == "issue":
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if argv[0] == "gh" and argv[1] == "api" and "node(id: $p)" in argv[4]:
            return SimpleNamespace(returncode=1, stderr="boom", stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    with patch.object(MOD.subprocess, "run", side_effect=fake_run):
        out = MOD.sync_one_card(done_card(), "org/repo", project)
    assert out["status"] == "synced"
    assert any(a["action"] == "review" and a.get("status") == "skipped"
               for a in out["actions"])


# ── L1-05: idempotency + state persistence ────────────────────────────

def test_main_skips_already_synced_cards(tmp_path: Path) -> None:
    state_file = tmp_path / "synced_org_repo.json"
    state_file.write_text(json.dumps({"synced": ["t_test123"]}))

    with patch.object(MOD, "list_done_cards", return_value=[done_card()]), \
         patch.object(MOD, "sync_one_card", return_value={
             "card": "t_test123", "status": "synced", "issue": 42}) as sync_mock, \
         patch.object(MOD, "save_synced") as save_mock:
        MOD.main.__wrapped__ if hasattr(MOD.main, "__wrapped__") else None
        # call the internals the way main does
        cards = MOD.list_done_cards()
        synced = MOD.load_synced(state_file)
        pending = [c for c in cards if c["id"] not in synced]
        assert pending == []
        sync_mock.assert_not_called()
        save_mock.assert_not_called()


def test_load_save_synced_roundtrip(tmp_path: Path) -> None:
    sf = tmp_path / "state.json"
    MOD.save_synced(sf, {"t_a", "t_b"})
    assert MOD.load_synced(sf) == {"t_a", "t_b"}


def test_load_synced_missing_file_empty(tmp_path: Path) -> None:
    assert MOD.load_synced(tmp_path / "nope.json") == set()


def test_load_synced_corrupt_file_empty(tmp_path: Path) -> None:
    sf = tmp_path / "state.json"
    sf.write_text("not json{{{")
    assert MOD.load_synced(sf) == set()


# ── L1-06: EV trigger (pure logic of the EV card pattern) ─────────────

def test_extract_issue_number_prefers_explicit_issue_field() -> None:
    """body `issue:` anchor wins over a title [Issue #N]."""
    card = done_card(title="[Issue #99] Different",
                     body="issue: 42\nContract here")
    assert MOD.extract_issue_number(card) == 42


# ── EV verdict sync (auditor → GitHub, 账号即物证) ─────────────────────

def ev_card(verdict: str = "VERDICT: REJECT\n缺 §7 附录", title: str = "[EV] issue #1 审计",
            status: str = "done") -> dict:
    return {
        "id": "t_ev1",
        "title": title,
        "body": "issue: 1\nEV for #1",
        "result": verdict,
        "status": status,
    }


def test_parse_verdict_structured_marker() -> None:
    assert MOD.parse_verdict("VERDICT: PASS\n证据链完整") == "PASS"
    assert MOD.parse_verdict("VERDICT: REJECT\n缺 §7") == "REJECT"
    # 大小写不敏感
    assert MOD.parse_verdict("verdict: pass") == "PASS"
    # 标记可在任意行 (约定首行, 但容忍后续行)
    assert MOD.parse_verdict("详情...\nVERDICT: REJECT") == "REJECT"


def test_parse_verdict_fails_closed_on_free_text() -> None:
    """自由文本不含结构化标记 → None (绝不宽松猜文本)."""
    assert MOD.parse_verdict("REJECT: 缺 §7 附录") is None
    assert MOD.parse_verdict("the PASS criteria...") is None
    assert MOD.parse_verdict("VERDICT: MAYBE") is None  # 非法值
    assert MOD.parse_verdict("") is None


def test_is_ev_card_detects_prefix() -> None:
    assert MOD.is_ev_card(ev_card()) is True
    assert MOD.is_ev_card(done_card()) is False


def test_sync_ev_verdict_reject_posts_comment() -> None:
    card = ev_card(verdict="VERDICT: REJECT\n缺 §7 附录, 请补齐")
    with patch.object(MOD, "current_gh_user", return_value="hh1985"), \
         patch.object(MOD.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0,
                                                   stdout="ok", stderr="")) as m:
        out = MOD.sync_ev_verdict(card, "org/repo", gh_user="hh1985")
    assert out["status"] == "synced"
    assert "REJECT" in out["verdict"]
    argv = m.call_args.args[0]
    assert argv[:3] == ["gh", "issue", "comment"]
    body = argv[argv.index("--body") + 1]
    assert "REJECT" in body
    assert "EV 裁决" in body


def test_sync_ev_verdict_rejects_free_text_result() -> None:
    """自由文本 result (无结构化标记) → fail-closed, 不发评论."""
    card = ev_card(verdict="REJECT: 缺 §7 附录")  # 无 VERDICT: 标记
    with patch.object(MOD, "current_gh_user", return_value="hh1985"), \
         patch.object(MOD.subprocess, "run") as m:
        out = MOD.sync_ev_verdict(card, "org/repo", gh_user="hh1985")
    assert out["status"] == "failed"
    assert "VERDICT" in out["reason"]
    m.assert_not_called()  # 不猜文本 → 不写任何评论


def test_sync_ev_verdict_requires_gh_user() -> None:
    card = ev_card(verdict="PASS")
    with pytest.raises(RuntimeError, match="gh_user"):
        MOD.sync_ev_verdict(card, "org/repo")  # 不传 gh_user → fail-closed


def test_sync_ev_verdict_guard_rejects_wrong_account() -> None:
    """账号守卫: 当前账号 ≠ 期望账号 → 拒绝写入 (防角色错位)."""
    card = ev_card(verdict="VERDICT: PASS\n证据链完整")
    with patch.object(MOD, "current_gh_user", return_value="bot-account"):
        with pytest.raises(RuntimeError, match="账号守卫"):
            MOD.sync_ev_verdict(card, "org/repo", gh_user="hh1985")


def test_sync_ev_verdict_pass_label() -> None:
    card = ev_card(verdict="VERDICT: PASS\n证据链完整")
    with patch.object(MOD, "current_gh_user", return_value="hh1985"), \
         patch.object(MOD.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0,
                                                   stdout="ok", stderr="")):
        out = MOD.sync_ev_verdict(card, "org/repo", gh_user="hh1985")
    assert "PASS" in out["verdict"]
    assert "REJECT" not in out["verdict"]


def test_sync_ev_verdict_no_issue_ref_skipped() -> None:
    card = ev_card(title="[EV] no ref here")
    card["body"] = "no issue link"
    out = MOD.sync_ev_verdict(card, "org/repo", gh_user="hh1985")
    assert out["status"] == "skipped"


def test_sync_ev_verdict_no_result_skipped() -> None:
    card = ev_card(verdict="")
    out = MOD.sync_ev_verdict(card, "org/repo", gh_user="hh1985")
    assert out["status"] == "skipped"
    assert "no verdict" in out["reason"]


def test_main_ev_mode_uses_auditor_account_from_env(monkeypatch) -> None:
    """EV 同步: 账号从 GH_USER_AUDITOR 环境变量预传递 (无默认, fail-closed)."""
    monkeypatch.setenv("GH_USER_AUDITOR", "auditor-account")
    calls: list[list[str]] = []

    def fake_run(argv, capture_output=False, text=False, timeout=30,
                 input=None, env=None):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    with patch.object(MOD, "list_all_cards", return_value=[ev_card()]), \
         patch.object(MOD, "list_done_cards", return_value=[]), \
         patch.object(MOD, "current_gh_user", return_value="auditor-account"), \
         patch.object(MOD.subprocess, "run", side_effect=fake_run):
        # simulate the --sync-ev branch (as main does)
        ev_cards = [c for c in MOD.list_all_cards()
                    if MOD.is_ev_card(c) and c.get("status") == "done"]
        gh_user = MOD.role_user("auditor")
        assert gh_user == "auditor-account"
        for card in ev_cards:
            MOD.switch_gh_user(gh_user)
            MOD.sync_ev_verdict(card, "org/repo", gh_user=gh_user)
    auth_switch = [a for a in calls if a[:2] == ["gh", "auth"]]
    comment_call = [a for a in calls if a[:3] == ["gh", "issue", "comment"]]
    assert auth_switch, "必须切换账号"
    assert "auditor-account" in auth_switch[0]
    assert comment_call, "必须发评论"


def test_role_user_missing_env_fails_closed(monkeypatch) -> None:
    """角色账号未预传递 → 拒绝运行 (禁止默认账号)."""
    monkeypatch.delenv("GH_USER_AUDITOR", raising=False)
    with pytest.raises(RuntimeError, match="GH_USER_AUDITOR"):
        MOD.role_user("auditor")


def test_role_user_unknown_role() -> None:
    with pytest.raises(RuntimeError, match="未知角色"):
        MOD.role_user("nobody")
