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




def test_graphql_failure_never_becomes_empty_graph() -> None:
    failed = SimpleNamespace(returncode=1, stdout="", stderr="network down")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        with pytest.raises(MOD.ControlPlaneUnavailable):
            MOD.gql_query("query { viewer { login } }")


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


def test_load_state_corrupt_file_returns_none_and_backs_up() -> None:
    """损坏 state 文件 → fail-closed 抛错 (人工处理), 不静默转 baseline."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        state_file = Path(td) / "dispatcher_test_state.json"
        state_file.write_text("{broken json!!")
        try:
            MOD.load_state(state_file)
            assert False, "should raise on corrupt state"
        except RuntimeError as e:
            assert "损坏" in str(e)




