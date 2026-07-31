from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bot_dispatcher", ROOT / "dispatcher.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


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
        with unittest.TestCase().assertRaises(MOD.ControlPlaneUnavailable):
            MOD.build_issue_proj_map({})


def test_shared_bot_pr_routes_by_linked_issue_project() -> None:
    pr = {
        "author": {"login": "everything-bot-engineer"},
        "body": "Fixes #7",
    }
    session = MOD.resolve_pr_session(
        pr,
        proj_map={7: 2},
        projects=[{"number": 2, "owner": "strategist"}],
        sm={"engineer": "eng", "strategist": "strategy"},
        assignee_map={"everything-bot-engineer": "engineer"},
    )
    assert session == "strategy"


def test_every_event_is_kept_in_one_session_digest() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "strategy", "first")
    MOD.queue_goal(output, "strategy", "second")
    completed = SimpleNamespace(returncode=0, stderr="")
    with patch.object(MOD.subprocess, "run", return_value=completed) as run:
        assert MOD.flush_goals(output) is True
    assert run.call_count == 1
    payload = run.call_args.args[0][-1]
    assert "first" in payload and "second" in payload
    assert output["actions"][-1]["event_count"] == 2


def test_delivery_failure_is_reported_for_retry() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "engineer", "work")
    failed = SimpleNamespace(returncode=1, stderr="session unavailable")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        assert MOD.flush_goals(output) is False
    assert output["actions"][-1]["state"] == "pending_retry"


def test_graphql_failure_never_becomes_empty_graph() -> None:
    failed = SimpleNamespace(returncode=1, stdout="", stderr="network down")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        with unittest.TestCase().assertRaises(MOD.ControlPlaneUnavailable):
            MOD.gql_query("query { viewer { login } }")
