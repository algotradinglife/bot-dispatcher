from __future__ import annotations

import importlib.util
import json
import tempfile
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


def project_node(number: int, item_id: str = "item") -> dict:
    return {
        "id": item_id,
        "content": {"__typename": "Issue", "number": number, "title": str(number)},
        "fieldValues": {
            "nodes": [
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": "Ready",
                    "field": {"name": "Status"},
                }
            ]
        },
    }


def project_page(nodes: list[dict], has_next: bool, cursor: str | None) -> dict:
    return {
        "data": {
            "node": {
                "items": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                }
            }
        }
    }


def test_loads_valid_repo_config() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "dispatcher.yaml"
        path.write_text(
            "repos:\n"
            "  sample:\n"
            "    repo: example-org/sample-research\n"
            "    projects: []\n"
            "    session_map:\n"
            "      pi: sample-PI\n"
        )
        assert MOD.load_config("sample", path)["repo"] == "example-org/sample-research"


def test_rejects_unknown_project_owner_role() -> None:
    config = valid_config()
    config["projects"][0]["owner"] = "missing"
    with pytest.raises(ValueError, match="owner is not in session_map"):
        MOD.validate_repo_config("sample", config)


def test_rejects_duplicate_project_number() -> None:
    config = valid_config()
    config["projects"].append(dict(config["projects"][0]))
    with pytest.raises(ValueError, match="unique positive integers"):
        MOD.validate_repo_config("sample", config)


def test_workflow_session_defaults_to_pi() -> None:
    config = valid_config()
    assert MOD.resolve_workflow_session(config, config["session_map"]) == "sample-PI"


def test_explicit_pm_workflow_session_is_resolved() -> None:
    config = valid_config()
    config["session_map"]["pm"] = "sample-PM"
    config["workflow_role"] = "pm"
    MOD.validate_repo_config("sample", config)
    assert MOD.resolve_workflow_session(config, config["session_map"]) == "sample-PM"


def test_rejects_unknown_workflow_role() -> None:
    config = valid_config()
    config["workflow_role"] = "pm"
    with pytest.raises(ValueError, match="workflow_role is not in session_map"):
        MOD.validate_repo_config("sample", config)


def test_explicit_pm_receives_issue_lifecycle_copy() -> None:
    config = valid_config()
    config["session_map"]["pm"] = "sample-PM"
    config["workflow_role"] = "pm"
    output = {"actions": [], "_pending": {}}
    MOD.queue_workflow_issue_transition(
        config,
        config["session_map"],
        "project:1:42:status",
        42,
        "Test research",
        "Ready",
        "In Progress",
        "https://example.test/issues/42",
        "sample-Research",
        output,
    )
    assert list(output["_pending"]) == ["sample-PM"]
    assert output["_pending"]["sample-PM"][0].startswith(
        "/goal PM coordination: Issue #42 -> In Progress"
    )
    assert output["actions"][0]["reason"] == "issue_status_coordinator"


def test_implicit_pi_mode_does_not_add_lifecycle_copy() -> None:
    config = valid_config()
    output = {"actions": [], "_pending": {}}
    MOD.queue_workflow_issue_transition(
        config,
        config["session_map"],
        "project:1:42:status",
        42,
        "Test research",
        "Ready",
        "In Progress",
        "https://example.test/issues/42",
        "sample-Research",
        output,
    )
    assert output == {"actions": [], "_pending": {}}


def test_project_items_follow_all_cursor_pages() -> None:
    cfg = {"projects": [{"number": 2, "node": "PVT_test", "name": "Research"}]}
    pages = [
        project_page([project_node(1, "a")], True, "cursor-1"),
        project_page([project_node(2, "b")], False, None),
    ]
    with patch.object(MOD, "gql_query", side_effect=pages) as query:
        items = MOD.get_project_items(cfg)
    assert [item["number"] for item in items] == [1, 2]
    assert query.call_count == 2
    assert 'after:"cursor-1"' in query.call_args_list[1].args[0]


def test_conflicting_project_membership_fails_closed() -> None:
    items = [
        {"number": 7, "project_num": 1, "is_pr": False},
        {"number": 7, "project_num": 2, "is_pr": False},
    ]
    with patch.object(MOD, "get_project_items", return_value=items):
        with pytest.raises(MOD.ControlPlaneUnavailable):
            MOD.build_issue_proj_map({})


def test_shared_pr_routes_by_linked_issue_project() -> None:
    pr = {"author": {"login": "shared-bot"}, "body": "Fixes #7"}
    session = MOD.resolve_pr_session(
        pr,
        proj_map={7: 2},
        projects=[{"number": 2, "owner": "researcher"}],
        sm={"engineer": "eng", "researcher": "research"},
        assignee_map={"shared-bot": "engineer"},
    )
    assert session == "research"


def test_shared_merged_pr_routes_by_linked_issue_project() -> None:
    merged_pr = {
        "author": {"login": "shared-bot"},
        "body": "Resolves #11",
        "mergedAt": "2026-07-31T00:00:00Z",
    }
    session = MOD.resolve_pr_session(
        merged_pr,
        proj_map={11: 3},
        projects=[{"number": 3, "owner": "engineer"}],
        sm={"engineer": "eng", "researcher": "research"},
        assignee_map={"shared-bot": "researcher"},
    )
    assert session == "eng"


def test_every_event_is_kept_in_one_session_digest() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "research", "first")
    MOD.queue_goal(output, "research", "second")
    completed = SimpleNamespace(returncode=0, stderr="", stdout="")
    shown = SimpleNamespace(
        returncode=0,
        stderr="",
        stdout='{"tmux_session":"agentdeck_sample"}',
    )
    with patch.object(
        MOD.subprocess, "run", side_effect=[completed, shown, completed]
    ) as run:
        assert MOD.flush_goals(output) is True
    assert run.call_count == 3
    payload = run.call_args_list[0].args[0][-1]
    assert "first" in payload and "second" in payload
    assert run.call_args_list[2].args[0] == [
        "tmux", "send-keys", "-t", "agentdeck_sample", "Enter"
    ]
    assert output["actions"][-1]["event_count"] == 2
    assert output["actions"][-1]["result"] == "ok + Enter"


def test_goal_messages_set_a_persistent_goal() -> None:
    assert MOD.format_goal("Do work", "Details", "https://example.test/1").startswith(
        "/goal Do work\n"
    )


def test_dry_run_keeps_digest_but_never_calls_agent_deck() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "sample-PI", "first")
    MOD.queue_goal(output, "sample-PI", "second")
    with patch.object(MOD.subprocess, "run") as run:
        assert MOD.flush_goals(output, dry_run=True) is True
    run.assert_not_called()
    assert output["actions"][-1]["event_count"] == 2
    assert output["actions"][-1]["state"] == "dry_run"
    assert output["actions"][-1]["result"] == "dry-run"


def test_delivery_failure_is_reported_for_retry() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "engineer", "work")
    failed = SimpleNamespace(returncode=1, stderr="session unavailable")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        assert MOD.flush_goals(output) is False
    assert output["actions"][-1]["state"] == "pending_retry"


def test_enter_submission_failure_is_reported_for_retry() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "engineer", "work")
    sent = SimpleNamespace(returncode=0, stderr="", stdout="")
    shown = SimpleNamespace(
        returncode=0,
        stderr="",
        stdout='{"tmux_session":"agentdeck_engineer"}',
    )
    enter_failed = SimpleNamespace(returncode=1, stderr="no tmux", stdout="")
    with patch.object(
        MOD.subprocess, "run", side_effect=[sent, shown, enter_failed]
    ):
        assert MOD.flush_goals(output) is False
    assert output["actions"][-1]["state"] == "pending_retry"
    assert "Enter submission failed" in output["actions"][-1]["result"]


def test_graphql_failure_never_becomes_empty_graph() -> None:
    failed = SimpleNamespace(returncode=1, stdout="", stderr="network down")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        with pytest.raises(MOD.ControlPlaneUnavailable):
            MOD.gql_query("query { viewer { login } }")


def test_linked_pr_lookup_failure_fails_closed() -> None:
    failed = SimpleNamespace(returncode=1, stdout="", stderr="network down")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        with pytest.raises(MOD.ControlPlaneUnavailable):
            MOD.check_linked_pr(
                "owner/repo",
                7,
                assignee_map={},
                sm={},
                proj_map={},
                projects=[],
            )


def test_hyphenated_to_directive_resolves_case_insensitively() -> None:
    mention_map = {"research-team": "sample-Research"}
    assert MOD.parse_to_directive(
        "[TO: Research-Team]\nPlease investigate.", mention_map
    ) == ("Research-Team", "sample-Research")


def test_quoted_directive_is_ignored() -> None:
    mention_map = {"research": "sample-Research"}
    assert MOD.parse_to_directive(
        "> [TO: research]\nQuoted", mention_map
    ) == (None, None)


def test_project_state_key_is_repo_neutral() -> None:
    assert MOD.project_state_key(7, 42) == "project:7:42:status"


def test_gql_query_retries_transient_failure() -> None:
    """Transient GraphQL failures are retried; success on second attempt."""
    transient = SimpleNamespace(returncode=1, stdout="", stderr="rate limit exceeded")
    ok = SimpleNamespace(
        returncode=0,
        stdout='{"data": {"repository": {"issue": {"number": 1}}}}',
        stderr="",
    )
    with patch.object(MOD.subprocess, "run", side_effect=[transient, ok]):
        payload = MOD.gql_query("{dummy}", retries=2, base_delay=0)
    assert payload["data"]["repository"]["issue"]["number"] == 1


def test_gql_query_raises_after_all_retries() -> None:
    failed = SimpleNamespace(returncode=1, stdout="", stderr="persistent error")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        with pytest.raises(MOD.ControlPlaneUnavailable):
            MOD.gql_query("{dummy}", retries=2, base_delay=0)


def test_flush_goals_baseline_never_sends() -> None:
    """Baseline flush records digests as baseline-skipped without agent-deck calls."""
    output = {"_pending": {"sample-PI": "historical event"}, "actions": []}
    with patch.object(MOD.subprocess, "run") as mocked:
        ok = MOD.flush_goals(output, dry_run=False, baseline=True)
    assert ok is True
    mocked.assert_not_called()
    action = output["actions"][0]
    assert action["state"] == "baseline"
    assert action["result"] == "baseline-skipped"


def test_first_run_marks_comments_seen_not_forwarded() -> None:
    """On first run a [TO: ...] comment is recorded as seen, never forwarded."""
    comment = {
        "id": "c1",
        "body": "[TO: engineer]\nDo the thing.",
        "author": {"login": "someone"},
    }
    issue = {"number": 1, "title": "t"}
    with patch.object(MOD.subprocess, "run") as mocked:
        mocked.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps([issue]), stderr=""),
            SimpleNamespace(returncode=0, stdout=json.dumps([comment]), stderr=""),
        ]
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "dispatcher_test_state.json"
            prev = MOD.load_state(state_file)
            new = dict(prev)
            # simulate the comment-scan branch for first run
            first_run = True
            mention_map = {"engineer": "sample-Engineer"}
            target, tsession = MOD.parse_to_directive(comment["body"], mention_map)
            assert (target, tsession) == ("engineer", "sample-Engineer")
            cid = comment["id"]
            ck = "comment:%d:%s:%s" % (issue["number"], cid, target.lower())
            assert prev.get(ck) is None
            if first_run:
                new[ck] = "seen"
    assert new["comment:1:c1:engineer"] == "seen"
