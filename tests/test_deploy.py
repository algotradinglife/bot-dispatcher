"""Tests for deploy.py — one-shot deployment tool.

These tests cover the deterministic, gh-free parts: workflow PROJECTS block
generation, dispatcher.yaml generation, tick script generation, and template
rendering. gh-dependent steps (auth, project creation) are skipped — they are
verified by live deployment runs.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("deploy", REPO_ROOT / "deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


FAKE_OPTS = {"Inbox": "i1", "Ready": "r1", "In Progress": "ip1",
             "Review": "v1", "Blocked": "b1", "Human": "h1", "Done": "d1"}


def test_gen_workflow_fills_real_ids(tmp_path):
    wf = deploy.gen_workflow(
        "PVT_PROJ", "PVTSSF_FIELD", FAKE_OPTS,
        "acme/algotrading", tmp_path, dry=False)
    text = wf.read_text()
    assert '"acme/algotrading"' in text
    assert '"PVT_PROJ"' in text
    assert '"PVTSSF_FIELD"' in text
    assert '"ready_for_review": "v1"' in text
    assert '"converted_to_draft": "r1"' in text
    assert '"closed": "d1"' in text
    # template's placeholder repo must be gone
    assert "algotradinglife" not in text


def test_gen_workflow_dry_run_writes_nothing(tmp_path):
    deploy.gen_workflow("P", "F", FAKE_OPTS, "a/b", tmp_path, dry=True)
    assert not (tmp_path / ".github").exists()


def test_gen_workflow_yaml_valid(tmp_path):
    wf = deploy.gen_workflow("P", "F", FAKE_OPTS, "a/b", tmp_path, dry=False)
    import yaml
    d = yaml.safe_load(wf.read_text())
    assert d["name"] == "Sync PR Status to Project"
    # PyYAML 1.1 parses `on:` as True; GitHub Actions uses YAML 1.2.
    # Assert the presence via the raw text instead.
    assert "pull_request:" in wf.read_text()


def test_gen_dispatcher_yaml_structure(tmp_path):
    cfg = deploy.gen_dispatcher_yaml(
        "acme", "acme/repo", "acme-board", "PVT_PROJ", 1,
        "PVTSSF_F", FAKE_OPTS, tmp_path, dry=False)
    text = cfg.read_text()
    assert "repo: acme/repo" in text
    assert "kanban_board: acme-board" in text
    assert 'node: "PVT_PROJ"' in text
    assert 'review_option: "v1"' in text
    assert "researcher: researcher    # Dr. Strange" in text
    assert "engineer: engineer        # Adam" in text
    assert "auditor: auditor          # Alan (EV)" in text
    assert "delivery_mode: kanban" in text


def test_gen_dispatcher_yaml_dry_run(tmp_path):
    deploy.gen_dispatcher_yaml(
        "k", "a/b", "b", "P", 1, "F", FAKE_OPTS, tmp_path, dry=True)
    assert not (tmp_path / "dispatcher.yaml").exists()


def test_gen_tick_contains_key_and_board(tmp_path):
    tk = deploy.gen_tick("acme", "acme/repo", "acme-board", tmp_path, dry=False)
    text = tk.read_text()
    assert "--repo acme" in text
    assert "acme/repo" in text
    # v0_3: tick 只跑 dispatcher — 无 sync_job / kanban / --archive
    assert "sync_job" not in text
    assert "--archive" not in text
    assert "notifications" in text
    assert tk.stat().st_mode & 0o111  # executable


def test_gen_tick_dry_run(tmp_path):
    deploy.gen_tick("k", "a/b", "b", tmp_path, dry=True)
    assert not (tmp_path / "k_tick.sh").exists()


def test_templates_exist():
    for tpl in ("AGENTS.md", "ROADMAP.md", "README.md"):
        assert (REPO_ROOT / "templates" / tpl).exists(), tpl
    # governance invariants baked into the template
    agents = (REPO_ROOT / "templates" / "AGENTS.md").read_text()
    assert "一 issue 一 worker" in agents
    assert "owner 不可变更" in agents
    assert "Engineering validation" in agents
    assert "路线图" in agents
    assert "PI 对接块" in agents


def test_states_definition_complete():
    names = [s[0] for s in deploy.STATES]
    assert names == ["Inbox", "Ready", "In Progress", "Review",
                     "Blocked", "Human", "Done"]
