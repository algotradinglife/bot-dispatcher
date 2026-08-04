"""Kanban-delivery dual-track tests (L0 layer of test-plan-v0_1).

Covers:
  - delivery_mode config parsing & validation (agent-deck | kanban | default)
  - build_kanban_command argv construction (title/body/assignee/idempotency)
  - kanban_idempotency_key determinism
  - flush_goals kanban branch: per-event `hermes kanban create`, failure keeps
    pending state (at-least-once), success commits
  - agent-deck legacy path preserved (regression guard)
  - issue_num anchor propagates from queue_goal to the kanban card
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bot_dispatcher", ROOT / "dispatcher.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def valid_config() -> dict:
    return {
        "repo": "example-org/sample-research",
        "projects": [
            {
                "number": 1,
                "node": "PVT_EXAMPLE",
                "name": "Research",
                "owner": "researcher",
            }
        ],
        "session_map": {
            "pi": "sample-PI",
            "researcher": "sample-Research",
        },
        "assignee_map": {"example-user": "researcher"},
        "mention_map": {"research-team": "researcher"},
    }


# ── L0-01/02/07: config parsing & validation ──────────────────────────

def test_delivery_mode_defaults_to_agent_deck() -> None:
    cfg = valid_config()
    assert cfg.get("delivery_mode", "agent-deck") == "agent-deck"
    # validation accepts the default
    MOD.validate_repo_config("sample-research", cfg)


def test_delivery_mode_kanban_accepted() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    cfg["kanban_bin"] = "hermes"
    cfg["kanban_board"] = "disease_survey"
    MOD.validate_repo_config("sample-research", cfg)


def test_delivery_mode_unknown_rejected() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "carrier-pigeon"
    with pytest.raises(ValueError, match="delivery_mode"):
        MOD.validate_repo_config("sample-research", cfg)


def test_kanban_bin_must_be_nonempty() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    cfg["kanban_bin"] = ""
    with pytest.raises(ValueError, match="kanban_bin"):
        MOD.validate_repo_config("sample-research", cfg)


def test_kanban_board_must_be_string() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    cfg["kanban_board"] = 42
    with pytest.raises(ValueError, match="kanban_board"):
        MOD.validate_repo_config("sample-research", cfg)


# ── L0-04: idempotency key ────────────────────────────────────────────

def test_idempotency_key_is_deterministic() -> None:
    k1 = MOD.kanban_idempotency_key("org/repo", 42)
    k2 = MOD.kanban_idempotency_key("org/repo", 42)
    assert k1 == k2 == "issue-org-repo-42"


def test_idempotency_key_differs_across_issues_and_repos() -> None:
    assert MOD.kanban_idempotency_key("org/repo", 42) != MOD.kanban_idempotency_key(
        "org/repo", 43
    )
    assert MOD.kanban_idempotency_key("org/repo", 42) != MOD.kanban_idempotency_key(
        "other/repo", 42
    )


# ── L0-02/03: kanban command construction ─────────────────────────────

def test_build_kanban_command_shape() -> None:
    argv = MOD.build_kanban_command("org/repo", 42, "Do the thing", "researcher",
                                    extra_body="/goal Do the thing\nhttps://x/42",
                                    kanban_bin="hermes")
    assert argv[0] == "hermes"
    assert argv[1:3] == ["kanban", "create"]
    assert "--idempotency-key" in argv
    assert "issue-org-repo-42" in argv
    assert "--assignee" in argv and "researcher" in argv
    assert argv[-1] == "[Issue #42] Do the thing"
    body = argv[argv.index("--body") + 1]
    assert "https://x/42" in body


def test_build_kanban_command_minimal() -> None:
    argv = MOD.build_kanban_command("org/repo", 7, "Task", "pi")
    assert argv[-1] == "[Issue #7] Task"
    assert "--project" not in argv


# ── L0-06/L1-01: flush_goals kanban branch ────────────────────────────

def test_flush_goals_kanban_creates_one_card_per_event() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "researcher", "/goal Do X\nhttps://x/1", issue_num=1)
    MOD.queue_goal(output, "researcher", "/goal Do Y\nhttps://x/2", issue_num=2)

    completed = SimpleNamespace(returncode=0, stderr="", stdout="t_123")
    with patch.object(MOD.subprocess, "run", return_value=completed) as run:
        assert MOD.flush_goals(output, cfg=cfg) is True

    assert run.call_count == 2
    keys = []
    for call in run.call_args_list:
        argv = call.args[0]
        keys.append(argv[argv.index("--idempotency-key") + 1])
    assert keys == ["issue-example-org-sample-research-1",
                    "issue-example-org-sample-research-2"]
    assert output["actions"][-1]["event_count"] == 2
    assert output["actions"][-1]["result"] == "ok; ok"


def test_flush_goals_kanban_failure_keeps_pending() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "researcher", "/goal Do X\nhttps://x/1", issue_num=1)

    failed = SimpleNamespace(returncode=1, stderr="boom", stdout="")
    with patch.object(MOD.subprocess, "run", return_value=failed) as run:
        assert MOD.flush_goals(output, cfg=cfg) is False
    run.assert_called_once()
    assert output["actions"][-1]["state"] == "pending_retry"
    assert "FAILED" in output["actions"][-1]["result"]


def test_flush_goals_kanban_partial_failure_reports_all_outcomes() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "researcher", "/goal A\nhttps://x/1", issue_num=1)
    MOD.queue_goal(output, "researcher", "/goal B\nhttps://x/2", issue_num=2)

    ok = SimpleNamespace(returncode=0, stderr="", stdout="")
    bad = SimpleNamespace(returncode=1, stderr="boom", stdout="")
    with patch.object(MOD.subprocess, "run", side_effect=[ok, bad]):
        assert MOD.flush_goals(output, cfg=cfg) is False
    assert output["actions"][-1]["result"] == "ok; FAILED: boom"


def test_flush_goals_kanban_respects_dry_run() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "researcher", "/goal Do X\nhttps://x/1", issue_num=1)
    with patch.object(MOD.subprocess, "run") as run:
        assert MOD.flush_goals(output, dry_run=True, cfg=cfg) is True
    run.assert_not_called()
    assert output["actions"][-1]["state"] == "dry_run"


def test_flush_goals_kanban_respects_baseline() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "researcher", "/goal Do X\nhttps://x/1", issue_num=1)
    with patch.object(MOD.subprocess, "run") as run:
        assert MOD.flush_goals(output, baseline=True, cfg=cfg) is True
    run.assert_not_called()
    assert output["actions"][-1]["state"] == "baseline"


def test_flush_goals_kanban_no_issue_num_uses_zero_key() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    output = {"actions": [], "_pending": {}}
    # queue_goal without issue_num (e.g. workflow-coordination copy)
    MOD.queue_goal(output, "researcher", "/goal Orphan task\nhttps://x/9")
    completed = SimpleNamespace(returncode=0, stderr="", stdout="")
    with patch.object(MOD.subprocess, "run", return_value=completed) as run:
        assert MOD.flush_goals(output, cfg=cfg) is True
    argv = run.call_args_list[0].args[0]
    assert "issue-example-org-sample-research-0" in argv


# ── L0-06: legacy agent-deck path preserved ───────────────────────────

def test_flush_goals_agent_deck_unchanged_with_cfg() -> None:
    """Passing cfg with default delivery_mode keeps the legacy path."""
    cfg = valid_config()  # no delivery_mode -> agent-deck
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "researcher", "/goal Do X\nhttps://x/1", issue_num=1)
    completed = SimpleNamespace(returncode=0, stderr="", stdout="")
    shown = SimpleNamespace(returncode=0, stderr="",
                            stdout='{"tmux_session":"agentdeck_sample"}')
    with patch.object(
        MOD.subprocess, "run", side_effect=[completed, shown, completed]
    ) as run:
        assert MOD.flush_goals(output, cfg=cfg) is True
    assert run.call_args_list[0].args[0][0] == "agent-deck"
    assert run.call_args_list[2].args[0][0] == "tmux"
    assert output["actions"][-1]["result"] == "ok + Enter"


# ── L0-08 (pure-function part): card title strips /goal marker ────────

def test_card_title_strips_goal_marker() -> None:
    cfg = valid_config()
    cfg["delivery_mode"] = "kanban"
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "researcher", "/goal Investigate X\nhttps://x/1", issue_num=1)
    completed = SimpleNamespace(returncode=0, stderr="", stdout="")
    with patch.object(MOD.subprocess, "run", return_value=completed) as run:
        MOD.flush_goals(output, cfg=cfg)
    argv = run.call_args_list[0].args[0]
    assert argv[-1] == "[Issue #1] Investigate X"
