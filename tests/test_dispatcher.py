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
        ok, failed = MOD.flush_goals(output)
        assert ok is True and failed == {}
    assert run.call_count == 3
    payload = run.call_args_list[0].args[0][-1]
    assert "first" in payload and "second" in payload
    assert "\n\n" in payload  # compact separator, no heavy block
    assert run.call_args_list[2].args[0] == [
        "tmux", "send-keys", "-t", "agentdeck_sample", "Enter"
    ]
    assert output["actions"][-1]["event_count"] == 2
    assert output["actions"][-1]["result"] == "ok + Enter"


def test_duplicate_goal_messages_are_deduped_within_tick() -> None:
    """Identical goals for the same session are sent once, not twice."""
    output = {"actions": [], "_pending": {}}
    dup = "/goal [TO: Strategist] from @hh1985 on PR #143 — settle"
    MOD.queue_goal(output, "strategy", dup)
    MOD.queue_goal(output, "strategy", dup)  # same text, same session
    MOD.queue_goal(output, "strategy", "different")
    pending = output["_pending"]["strategy"]
    assert pending.count(dup) == 1
    assert len(pending) == 2


def test_goal_messages_set_a_persistent_goal() -> None:
    assert MOD.format_goal("Do work", "https://example.test/1") == (
        "/goal Do work\nhttps://example.test/1"
    )


def test_notice_messages_do_not_set_a_goal() -> None:
    assert MOD.format_notice("Work is done", "https://example.test/1") == (
        "Work is done\nhttps://example.test/1"
    )


def test_dry_run_keeps_digest_but_never_calls_agent_deck() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "sample-PI", "first")
    MOD.queue_goal(output, "sample-PI", "second")
    with patch.object(MOD.subprocess, "run") as run:
        ok, failed = MOD.flush_goals(output, dry_run=True)
        assert ok is True and failed == {}
    run.assert_not_called()
    assert output["actions"][-1]["event_count"] == 2
    assert output["actions"][-1]["state"] == "dry_run"
    assert output["actions"][-1]["result"] == "dry-run"


def test_delivery_failure_is_reported_for_retry() -> None:
    output = {"actions": [], "_pending": {}}
    MOD.queue_goal(output, "engineer", "work")
    failed = SimpleNamespace(returncode=1, stderr="session unavailable")
    with patch.object(MOD.subprocess, "run", return_value=failed):
        ok, failed = MOD.flush_goals(output)
        assert ok is False and len(failed) == 1
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
        ok, failed = MOD.flush_goals(output)
        assert ok is False and len(failed) == 1
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
        ok, failed = MOD.flush_goals(output, dry_run=False, baseline=True)
    assert ok is True and failed == {}
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


def test_merged_pr_notified_once_across_both_detection_paths() -> None:
    """A PR that merges between ticks is notified exactly once, even though
    both the recent-merges scan and the per-key state sweep can see it."""
    import sys
    import yaml
    from pathlib import Path

    repo_cfg = {
        "repo": "example-org/sample-research",
        "projects": [
            {"number": 1, "node": "PVT_EXAMPLE", "name": "Research", "owner": "researcher"},
        ],
        "session_map": {"pi": "sample-PI", "researcher": "sample-Researcher"},
        "assignee_map": {"hh1985": "pi", "worker-bot": "researcher"},
        "mention_map": {"pi": "pi", "PI": "pi", "researcher": "researcher"},
    }

    def fake_run(cmd, **kwargs):
        cmd_s = " ".join(cmd)
        ok = SimpleNamespace(returncode=0, stderr="", stdout="")
        # 1. GraphQL project scan (build_issue_proj_map)
        if cmd[:2] == ["gh", "api"]:
            ok.stdout = json.dumps({"data": {"node": {"items": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}})
            return ok
        # 2. Issue list (comment scan) / milestone scan
        if cmd[:3] == ["gh", "issue", "list"]:
            ok.stdout = ""
            return ok
        # 3. Open PR list — #146 already merged, so it is absent
        if cmd[:3] == ["gh", "pr", "list"] and "--state" in cmd:
            idx = cmd.index("--state")
            if cmd[idx + 1] == "open":
                ok.stdout = "[]"
            else:
                ok.stdout = json.dumps([{
                    "number": 146, "title": "research: freeze #133 Gate A",
                    "mergedAt": "2026-08-02T11:45:56Z",
                    "author": {"login": "hh1985"},
                    "mergedBy": {"login": "hh1985"},
                    "body": "Resolves #133",
                }])
            return ok
        # 4. Per-key pr view sweep (path B)
        if cmd[:3] == ["gh", "pr", "view"]:
            ok.stdout = json.dumps({
                "state": "MERGED", "mergedAt": "2026-08-02T11:45:56Z",
                "author": "hh1985", "title": "research: freeze #133 Gate A",
                "body": "Resolves #133",
            })
            return ok
        ok.stdout = "{}"
        return ok

    # Both paths scan #146; the merged notice must be queued exactly once.
    import io, contextlib
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td2:
        cfg_file = Path(td2) / "dispatcher.yaml"
        cfg_file.write_text(yaml.safe_dump({"repos": {"sample": repo_cfg}}))
        state_file = Path(td2) / "dispatcher_sample_state.json"
        # Previous tick recorded #146 as open; it merged since.
        state_file.write_text(json.dumps({"pr:146": "open"}))

        with contextlib.redirect_stdout(buf):
            with patch.object(MOD.subprocess, "run", side_effect=fake_run):
                with patch.object(sys, "argv", [
                    "dispatcher.py", "--repo", "sample",
                    "--config", str(cfg_file),
                    "--state-dir", td2, "--dry-run",
                ]):
                    MOD.main()
    payload = json.loads(buf.getvalue())
    merged_actions = [a for a in payload.get("actions", [])
                      if a.get("reason", "").startswith("pr_merged")]
    assert len(merged_actions) == 1, (
        "expected exactly one merged notification, got %d: %s"
        % (len(merged_actions), merged_actions)
    )


def test_unknown_to_alias_falls_back_to_project_owner() -> None:
    """[TO: Worker] (not in mention_map) routes by linked Issue Project owner,
    not by semantic alias guessing."""
    pr = {
        "number": 147,
        "title": "research: settle #133 Gate B",
        "author": {"login": "hh1985"},
        "body": "Resolves #133",
    }
    proj_map = {133: 2}
    projects = [{"number": 2, "owner": "strategist"}]
    sm = {"pi": "sample-PI", "strategist": "sample-Strategy"}
    assignee_map = {"hh1985": "pi"}
    target, session = MOD.parse_to_directive(
        "[TO: Worker]\nPI review correction required.", {"pi": "pi"}
    )
    assert target == "Worker"
    assert session is None  # unknown alias — no semantic match
    fallback = MOD.resolve_pr_session(pr, proj_map, projects, sm, assignee_map)
    assert fallback == "sample-Strategy"  # project owner decides


def test_known_to_alias_does_not_need_fallback() -> None:
    """[TO: Strategist] resolves via mention_map directly."""
    target, session = MOD.parse_to_directive(
        "[TO: Strategist]\nPlease settle.", {"strategist": "sample-Strategy"}
    )
    assert (target, session) == ("Strategist", "sample-Strategy")


def test_plain_comment_has_no_directive() -> None:
    """A comment without [TO:] yields no target, no session."""
    target, session = MOD.parse_to_directive(
        "Reproducible package confirmed.", {"pi": "sample-PI"}
    )
    assert (target, session) == (None, None)


def test_pr_to_worker_routes_by_project_owner_integration() -> None:
    """Full-tick: a PR comment [TO: Worker] with no mention_map alias still
    reaches the linked Issue's Project owner session."""
    import sys
    import io
    import contextlib
    import yaml
    from pathlib import Path

    repo_cfg = {
        "repo": "example-org/sample-research",
        "projects": [
            {"number": 2, "node": "PVT_EXAMPLE2", "name": "Prediction", "owner": "strategist"},
        ],
        "session_map": {"pi": "sample-PI", "strategist": "sample-Strategy"},
        "assignee_map": {"hh1985": "pi", "worker-bot": "strategist"},
        "mention_map": {"pi": "pi", "strategist": "strategist"},
    }

    def fake_run(cmd, **kwargs):
        cmd_s = " ".join(cmd)
        ok = SimpleNamespace(returncode=0, stderr="", stdout="")
        if cmd[:2] == ["gh", "api"]:
            ok.stdout = json.dumps({"data": {"node": {"items": {
                "nodes": [{
                    "id": "ITEM_133",
                    "content": {"__typename": "Issue", "number": 133, "title": "diversified allocator"},
                    "fieldValues": {"nodes": [{
                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                        "name": "Inbox",
                        "field": {"name": "Status"},
                    }]},
                }],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}})
            return ok
        if cmd[:3] == ["gh", "issue", "list"]:
            ok.stdout = ""
            return ok
        if cmd[:3] == ["gh", "pr", "list"] and "--state" in cmd:
            idx = cmd.index("--state")
            if cmd[idx + 1] == "open":
                ok.stdout = json.dumps([{
                    "number": 147, "title": "settle #133 Gate B",
                    "headRefName": "strategy/bj133-gate-b",
                    "author": {"login": "hh1985"},
                    "createdAt": "2026-08-02T12:00:00Z",
                    "mergeStateStatus": "CLEAN",
                    "body": "Resolves #133",
                    "isDraft": False,
                }])
            else:
                ok.stdout = "[]"
            return ok
        if cmd[:3] == ["gh", "pr", "view"]:
            view_jq = " ".join(cmd)
            if "reviewDecision" in view_jq:
                ok.stdout = ""
            elif "comments" in view_jq:
                ok.stdout = json.dumps([{
                    "id": "IC_147_C1",
                    "author": {"login": "hh1985"},
                    "body": "[TO: Worker][PI REVIEW — CORRECTION REQUIRED][PR #147]\nPackage reproducible but not aligned.",
                }])
            else:
                ok.stdout = json.dumps({
                    "state": "MERGED", "mergedAt": "2026-08-02T12:00:00Z",
                    "author": "hh1985", "title": "settle #133 Gate B",
                    "body": "Resolves #133",
                })
            return ok
        ok.stdout = "{}"
        return ok

    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "dispatcher.yaml"
        cfg_file.write_text(yaml.safe_dump({"repos": {"sample": repo_cfg}}))
        state_file = Path(td) / "dispatcher_sample_state.json"
        # PR already open from previous tick; the [TO: Worker] comment is new.
        state_file.write_text(json.dumps({"pr:147": "open", "prdraft:147": False}))

        with contextlib.redirect_stdout(buf):
            with patch.object(MOD.subprocess, "run", side_effect=fake_run):
                with patch.object(sys, "argv", [
                    "dispatcher.py", "--repo", "sample",
                    "--config", str(cfg_file),
                    "--state-dir", td, "--dry-run",
                ]):
                    MOD.main()
    payload = json.loads(buf.getvalue())
    forwarded = [a for a in payload.get("actions", [])
                 if a.get("reason") == "to_directive_pr"]
    assert len(forwarded) == 1, "expected one [TO:] forward, got %d: %s" % (
        len(forwarded), [a.get("reason") for a in payload.get("actions", [])])
    assert forwarded[0]["session"] == "sample-Strategy", forwarded[0]
    assert forwarded[0]["target"] == "Worker", forwarded[0]


def test_worker_session_no_linked_issue_fails_closed() -> None:
    """Unknown [TO:] on a PR with no linked Issue must NOT fall back to the
    PR author's assignee role — worker is Project ownership only."""
    pr = {"number": 1, "body": "No linked issue here",
          "author": {"login": "hh1985"}}
    proj_map = {}
    projects = []
    sm = {"pi": "sample-PI"}
    assert MOD.resolve_worker_session(pr, proj_map, projects, sm) is None


def test_worker_session_multi_owner_fails_closed() -> None:
    """A PR linking Issues owned by different Projects resolves to no unique
    worker — fail closed rather than guessing."""
    pr = {"number": 1, "body": "Fixes #1\nResolves #2"}
    proj_map = {1: 2, 2: 3}
    projects = [
        {"number": 2, "owner": "strategist"},
        {"number": 3, "owner": "engineer"},
    ]
    sm = {"strategist": "sample-Strategy", "engineer": "sample-Engineer"}
    assert MOD.resolve_worker_session(pr, proj_map, projects, sm) is None


def test_worker_session_single_owner_routes() -> None:
    """Single linked Issue with one Project owner routes to that owner."""
    pr = {"number": 1, "body": "Resolves #133"}
    proj_map = {133: 2}
    projects = [{"number": 2, "owner": "strategist"}]
    sm = {"strategist": "sample-Strategy"}
    assert MOD.resolve_worker_session(pr, proj_map, projects, sm) == "sample-Strategy"


def test_worker_session_mixed_mapped_unmapped_fails_closed() -> None:
    """A PR linking one Project-owned Issue and one unmapped Issue must not
    route — every linked Issue must establish the same owner."""
    pr = {"number": 1, "body": "Fixes #1\nResolves #2"}
    proj_map = {1: 2}  # #2 has no Project mapping
    projects = [{"number": 2, "owner": "strategist"}]
    sm = {"strategist": "sample-Strategy"}
    assert MOD.resolve_worker_session(pr, proj_map, projects, sm) is None


def test_plain_comment_not_forwarded_with_fallback() -> None:
    """A plain comment (no [TO:]) must not be routed via project-owner
    fallback — only explicit [TO:] directives trigger worker resolution."""
    pr = {
        "number": 147,
        "title": "settle #133 Gate B",
        "author": {"login": "hh1985"},
        "body": "Resolves #133",
    }
    proj_map = {133: 2}
    projects = [{"number": 2, "owner": "strategist"}]
    sm = {"pi": "sample-PI", "strategist": "sample-Strategy"}
    assignee_map = {"hh1985": "pi"}
    target, tsession = MOD.parse_to_directive(
        "Package reproducible, confirming.", {"pi": "pi"}
    )
    assert target is None and tsession is None
    # No [TO:] → no worker fallback, stays unresolved.
    if target and not tsession:
        tsession = MOD.resolve_worker_session(pr, proj_map, projects, sm)
    assert tsession is None


def test_issue_to_worker_routes_by_project_owner_integration() -> None:
    """Full-tick: an Issue comment [TO: Worker] with no mention_map alias
    still reaches the Issue's Project owner session."""
    import sys
    import io
    import contextlib
    import yaml
    from pathlib import Path

    repo_cfg = {
        "repo": "example-org/sample-research",
        "projects": [
            {"number": 2, "node": "PVT_EXAMPLE2", "name": "Prediction", "owner": "strategist"},
        ],
        "session_map": {"pi": "sample-PI", "strategist": "sample-Strategy"},
        "assignee_map": {"hh1985": "pi", "worker-bot": "strategist"},
        "mention_map": {"pi": "pi", "strategist": "strategist"},
    }

    def fake_run(cmd, **kwargs):
        ok = SimpleNamespace(returncode=0, stderr="", stdout="")
        if cmd[:2] == ["gh", "api"]:
            ok.stdout = json.dumps({"data": {"node": {"items": {
                "nodes": [{
                    "id": "ITEM_133",
                    "content": {"__typename": "Issue", "number": 133, "title": "diversified allocator"},
                    "fieldValues": {"nodes": [{
                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                        "name": "Inbox",
                        "field": {"name": "Status"},
                    }]},
                }],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}})
            return ok
        if cmd[:3] == ["gh", "issue", "list"]:
            if "--json" in cmd and "milestone" in " ".join(cmd):
                ok.stdout = ""
            else:
                ok.stdout = json.dumps([{"number": 133, "title": "diversified allocator"}])
            return ok
        if cmd[:3] == ["gh", "issue", "view"]:
            ok.stdout = json.dumps([{
                "id": "IC_133_C1",
                "author": {"login": "hh1985"},
                "body": "[TO: Worker][GATE B AUTHORIZED]\nProceed with Gate B.",
            }])
            return ok
        if cmd[:3] == ["gh", "pr", "list"]:
            ok.stdout = "[]"
            return ok
        if cmd[:3] == ["gh", "pr", "view"]:
            ok.stdout = "{}"
            return ok
        ok.stdout = "{}"
        return ok

    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "dispatcher.yaml"
        cfg_file.write_text(yaml.safe_dump({"repos": {"sample": repo_cfg}}))
        state_file = Path(td) / "dispatcher_sample_state.json"
        state_file.write_text(json.dumps({}))

        with contextlib.redirect_stdout(buf):
            with patch.object(MOD.subprocess, "run", side_effect=fake_run):
                with patch.object(sys, "argv", [
                    "dispatcher.py", "--repo", "sample",
                    "--config", str(cfg_file),
                    "--state-dir", td, "--dry-run",
                ]):
                    MOD.main()
    payload = json.loads(buf.getvalue())
    forwarded = [a for a in payload.get("actions", [])
                 if a.get("reason") == "to_directive"]
    assert len(forwarded) == 1, "expected one issue [TO:] forward: %s" % (
        [a.get("reason") for a in payload.get("actions", [])])
    assert forwarded[0]["session"] == "sample-Strategy", forwarded[0]
    assert forwarded[0]["target"] == "Worker", forwarded[0]


def test_author_default_pi_message_goes_to_owner() -> None:
    """A plain comment from the PI account (no [TO:]) defaults to the Issue
    Project owner session."""
    sm = {"pi": "sample-PI", "strategist": "sample-Strategy"}
    assignee_map = {"hh1985": "pi", "everything-bot-engineer": "engineer"}
    s = MOD.resolve_author_default_session("hh1985", assignee_map, sm, "sample-Strategy")
    assert s == "sample-Strategy"


def test_author_default_worker_message_goes_to_pi() -> None:
    """A plain comment from a worker account (no [TO:]) defaults to the PI
    session."""
    sm = {"pi": "sample-PI", "strategist": "sample-Strategy"}
    assignee_map = {"hh1985": "pi", "everything-bot-engineer": "engineer"}
    s = MOD.resolve_author_default_session("everything-bot-engineer", assignee_map, sm, "sample-Strategy")
    assert s == "sample-PI"


def test_author_default_unknown_author_fails_closed() -> None:
    """An author mapped to neither PI nor a worker role returns None so the
    caller can warn instead of silently dropping."""
    sm = {"pi": "sample-PI"}
    assignee_map = {"hh1985": "pi"}
    s = MOD.resolve_author_default_session("random-user", assignee_map, sm, "sample-Strategy")
    assert s is None


def test_plain_pi_comment_routes_to_owner_and_unknown_warns() -> None:
    """Full-tick: a plain PI comment (no [TO:]) on a Project-owned Issue is
    routed to the owner session; a comment from an unmapped author produces
    an unroutable warning instead of silent drop."""
    import sys
    import io
    import contextlib
    import yaml
    from pathlib import Path

    repo_cfg = {
        "repo": "example-org/sample-research",
        "projects": [
            {"number": 2, "node": "PVT_EXAMPLE2", "name": "Prediction", "owner": "strategist"},
        ],
        "session_map": {"pi": "sample-PI", "strategist": "sample-Strategy", "engineer": "sample-Engineer"},
        "assignee_map": {"hh1985": "pi", "everything-bot-engineer": "engineer"},
        "mention_map": {"pi": "pi", "strategist": "strategist"},
    }

    def fake_run(cmd, **kwargs):
        ok = SimpleNamespace(returncode=0, stderr="", stdout="")
        if cmd[:2] == ["gh", "api"]:
            ok.stdout = json.dumps({"data": {"node": {"items": {
                "nodes": [{
                    "id": "ITEM_133",
                    "content": {"__typename": "Issue", "number": 133, "title": "diversified allocator"},
                    "fieldValues": {"nodes": [{
                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                        "name": "Inbox",
                        "field": {"name": "Status"},
                    }]},
                }],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}})
            return ok
        if cmd[:3] == ["gh", "issue", "list"]:
            if "--json" in cmd and "milestone" in " ".join(cmd):
                ok.stdout = ""
            else:
                ok.stdout = json.dumps([{"number": 133, "title": "diversified allocator"}])
            return ok
        if cmd[:3] == ["gh", "issue", "view"]:
            ok.stdout = json.dumps([
                {"id": "IC_133_PI", "author": {"login": "hh1985"},
                 "body": "Proceeding with the settlement."},
                {"id": "IC_133_UNK", "author": {"login": "ghost-user"},
                 "body": "Can I help?"},
            ])
            return ok
        if cmd[:3] == ["gh", "pr", "list"]:
            ok.stdout = "[]"
            return ok
        if cmd[:3] == ["gh", "pr", "view"]:
            ok.stdout = "{}"
            return ok
        ok.stdout = "{}"
        return ok

    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "dispatcher.yaml"
        cfg_file.write_text(yaml.safe_dump({"repos": {"sample": repo_cfg}}))
        state_file = Path(td) / "dispatcher_sample_state.json"
        state_file.write_text(json.dumps({}))

        with contextlib.redirect_stdout(buf):
            with patch.object(MOD.subprocess, "run", side_effect=fake_run):
                with patch.object(sys, "argv", [
                    "dispatcher.py", "--repo", "sample",
                    "--config", str(cfg_file),
                    "--state-dir", td, "--dry-run",
                ]):
                    MOD.main()
    payload = json.loads(buf.getvalue())
    forwards = [a for a in payload.get("actions", []) if a.get("reason") == "to_directive"]
    assert len(forwards) == 1, "PI plain comment should forward once: %s" % forwards
    assert forwards[0]["session"] == "sample-Strategy", forwards[0]
    warns = payload.get("warnings", [])
    assert any("unroutable" in w for w in warns), "unknown author should warn: %s" % warns


def test_plain_worker_comment_on_pr_routes_to_pi() -> None:
    """Full-tick: a plain comment (no [TO:]) from the worker bot on an open PR
    defaults to the PI session via author-identity routing."""
    import sys
    import io
    import contextlib
    import yaml
    from pathlib import Path

    repo_cfg = {
        "repo": "example-org/sample-research",
        "projects": [
            {"number": 2, "node": "PVT_EXAMPLE2", "name": "Prediction", "owner": "strategist"},
        ],
        "session_map": {"pi": "sample-PI", "strategist": "sample-Strategy", "engineer": "sample-Engineer"},
        "assignee_map": {"hh1985": "pi", "everything-bot-engineer": "engineer"},
        "mention_map": {"pi": "pi", "strategist": "strategist"},
    }

    def fake_run(cmd, **kwargs):
        ok = SimpleNamespace(returncode=0, stderr="", stdout="")
        if cmd[:2] == ["gh", "api"]:
            ok.stdout = json.dumps({"data": {"node": {"items": {
                "nodes": [{
                    "id": "ITEM_133",
                    "content": {"__typename": "Issue", "number": 133, "title": "diversified allocator"},
                    "fieldValues": {"nodes": [{
                        "__typename": "ProjectV2ItemFieldSingleSelectValue",
                        "name": "Inbox",
                        "field": {"name": "Status"},
                    }]},
                }],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}})
            return ok
        if cmd[:3] == ["gh", "issue", "list"]:
            ok.stdout = ""
            return ok
        if cmd[:3] == ["gh", "pr", "list"] and "--state" in cmd:
            idx = cmd.index("--state")
            if cmd[idx + 1] == "open":
                ok.stdout = json.dumps([{
                    "number": 147, "title": "settle #133 Gate B",
                    "headRefName": "strategy/bj133-gate-b",
                    "author": {"login": "hh1985"},
                    "createdAt": "2026-08-02T12:00:00Z",
                    "mergeStateStatus": "CLEAN",
                    "body": "Resolves #133",
                    "isDraft": False,
                }])
            else:
                ok.stdout = "[]"
            return ok
        if cmd[:3] == ["gh", "pr", "view"]:
            view_jq = " ".join(cmd)
            if "reviewDecision" in view_jq:
                ok.stdout = ""
            elif "comments" in view_jq:
                ok.stdout = json.dumps([{
                    "id": "IC_147_W", "author": {"login": "everything-bot-engineer"},
                    "body": "Gate B package rebuilt with WR baseline.",
                }])
            else:
                ok.stdout = "{}"
            return ok
        ok.stdout = "{}"
        return ok

    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as td:
        cfg_file = Path(td) / "dispatcher.yaml"
        cfg_file.write_text(yaml.safe_dump({"repos": {"sample": repo_cfg}}))
        state_file = Path(td) / "dispatcher_sample_state.json"
        state_file.write_text(json.dumps({"pr:147": "open", "prdraft:147": False}))

        with contextlib.redirect_stdout(buf):
            with patch.object(MOD.subprocess, "run", side_effect=fake_run):
                with patch.object(sys, "argv", [
                    "dispatcher.py", "--repo", "sample",
                    "--config", str(cfg_file),
                    "--state-dir", td, "--dry-run",
                ]):
                    MOD.main()
    payload = json.loads(buf.getvalue())
    forwards = [a for a in payload.get("actions", [])
                if a.get("reason") == "to_directive_pr"]
    assert len(forwards) == 1, "worker plain comment should forward once: %s" % forwards
    assert forwards[0]["session"] == "sample-PI", forwards[0]
    assert not forwards[0].get("target"), forwards[0]  # no [TO:] — notice form


def test_to_directive_with_label_prefix_resolves() -> None:
    """[PI DEPENDENCY CLOSURE][TO: STRATEGY] — [TO:] preceded by a label on
    the same line must still resolve (the bug that stalled paired-trading)."""
    target, session = MOD.parse_to_directive(
        "[PI DEPENDENCY CLOSURE][TO: STRATEGY]\nDependency #80 closed.",
        {"strategist": "sample-Strategy", "strategy": "sample-Strategy"},
    )
    assert (target, session) == ("STRATEGY", "sample-Strategy")


def test_to_directive_second_line_still_ignored() -> None:
    """Directives on later lines are still not scanned (first content line
    only), preserving the existing single-line contract."""
    target, session = MOD.parse_to_directive(
        "Ordinary sentence.\n[TO: PI] ignored directive",
        {"pi": "sample-PI"},
    )
    assert (target, session) == (None, None)


def test_inline_prose_to_reference_not_misread() -> None:
    """A sentence that merely references [TO: ...] mid-line (not as a leading
    directive) must not be treated as a routing instruction."""
    target, session = MOD.parse_to_directive(
        "See the [TO: PI] example in the docs.",
        {"pi": "sample-PI"},
    )
    assert (target, session) == (None, None)


def test_to_directive_slash_annotation_resolves() -> None:
    """[TO: PI / FRESH INDEPENDENT EVIDENCE REVIEW] — annotation after the
    target (slash form) must resolve to the first token."""
    target, session = MOD.parse_to_directive(
        "[TO: PI / FRESH INDEPENDENT EVIDENCE REVIEW]\nReview needed.",
        {"pi": "sample-PI"},
    )
    assert (target, session) == ("PI", "sample-PI")


def test_to_directive_goal_prefix_resolves() -> None:
    """/goal [TO: Worker][GATE B AUTHORIZED] — /goal command prefix before
    [TO:] must not defeat parsing (the format PI actually uses)."""
    target, session = MOD.parse_to_directive(
        "/goal [TO: Worker][GATE B AUTHORIZED]\nProceed.",
        {"worker": "sample-Worker"},
    )
    assert (target, session) == ("Worker", "sample-Worker")


def test_to_directive_multi_target_takes_first() -> None:
    """[TO: PI / STRATEGY] resolves to the first target (PI), not the
    slash-separated remainder."""
    target, session = MOD.parse_to_directive(
        "[TO: PI / STRATEGY]\nBoth notified.",
        {"pi": "sample-PI", "strategy": "sample-Strategy"},
    )
    assert (target, session) == ("PI", "sample-PI")


def test_extract_report_url_from_merged_pr_body() -> None:
    """A completed Issue with a merged PR whose body names results/... gets
    the report URL appended."""
    with patch.object(MOD.subprocess, "run") as mocked:
        mocked.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps([
                {"number": 162, "state": "MERGED",
                 "body": "Resolves #145. Package: results/bj145_gate_b_summary.md"},
            ]), stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # no comments
        ]
        url = MOD.extract_report_url("example-org/sample", 145)
    assert url == "https://github.com/example-org/sample/blob/main/results/bj145_gate_b_summary.md"


def test_extract_report_url_none_when_no_linked_pr() -> None:
    """No linked PR carrying the issue number -> no report URL."""
    with patch.object(MOD.subprocess, "run") as mocked:
        mocked.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps([
                {"number": 200, "state": "MERGED", "body": "Unrelated work."},
            ]), stderr=""),
        ]
        url = MOD.extract_report_url("example-org/sample", 999)
    assert url is None


def test_extract_report_url_wiki_link() -> None:
    """Wiki links in PR text are returned as-is."""
    with patch.object(MOD.subprocess, "run") as mocked:
        mocked.side_effect = [
            SimpleNamespace(returncode=0, stdout=json.dumps([
                {"number": 162, "state": "MERGED",
                 "body": "Fixes #145. Wiki: https://github.com/example-org/sample/wiki/BJ145"},
            ]), stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
        url = MOD.extract_report_url("example-org/sample", 145)
    assert url == "https://github.com/example-org/sample/wiki/BJ145"


def test_flush_goals_returns_failed_sessions() -> None:
    """Failed delivery returns (False, {session: digest}) so the caller can
    persist goals for retry without rolling back GitHub event state."""
    output = {"_pending": {"sample-Engineer": "goal one"}, "actions": []}
    fail = SimpleNamespace(returncode=1, stderr="session not running", stdout="")
    with patch.object(MOD.subprocess, "run", side_effect=[fail]):
        ok, failed = MOD.flush_goals(output)
    assert ok is False
    assert failed == {"sample-Engineer": "goal one"}
    action = output["actions"][0]
    assert action["state"] == "pending_retry"
    assert "session not running" in action["result"]


def test_flush_goals_mixed_success_and_failure() -> None:
    """One dead session does not mark healthy sessions as failed; only the
    failed session is returned for retry."""
    output = {
        "_pending": {
            "sample-Strategy": "goal a",
            "sample-Engineer": "goal b",
        },
        "actions": [],
    }
    ok_send = SimpleNamespace(returncode=0, stderr="", stdout="")
    ok_show = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
        {"tmux_session": "agentdeck_x"}))
    ok_enter = SimpleNamespace(returncode=0, stderr="", stdout="")
    fail_send = SimpleNamespace(returncode=1, stderr="session not running", stdout="")
    with patch.object(MOD.subprocess, "run", side_effect=[
        ok_send, ok_show, ok_enter,   # Strategy: send + show + enter
        fail_send,                    # Engineer: send fails
    ]):
        ok, failed = MOD.flush_goals(output)
    assert ok is False
    assert list(failed.keys()) == ["sample-Engineer"]
    states = {a["session"]: a["state"] for a in output["actions"]}
    assert states["sample-Strategy"] == "sent"
    assert states["sample-Engineer"] == "pending_retry"
