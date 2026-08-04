#!/usr/bin/env python3
"""
Kanban → GitHub sync job (reporting channel of the three-layer system).

Reads done kanban cards and mirrors their evidence summary back to the
control plane: posts a GitHub issue comment and moves the linked issue to
Review on its Project board.

Direction is strictly one-way: kanban (execution state) → GitHub (decision
state). This script never reads GitHub status to drive kanban, never mutates
kanban, and never touches Issue Graph.

Usage:
  sync_job.py --repo <owner/name> --config <dispatcher.yaml> [--board <slug>]
              [--dry-run] [--state-dir <dir>]

Idempotency: a state file records card ids already synced; a card is only
reported once even if the job runs repeatedly.

Eventual consistency: cards are read with `hermes kanban list --status done`
(scoped to the configured board via --board when provided).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_STATE_HOME = Path(__file__).resolve().parent.parent / ".sync_state"
ISSUE_REF_RE = re.compile(r"#(\d+)")


# ── pure helpers (unit-testable) ──────────────────────────────────────

def extract_issue_number(card: dict) -> int | None:
    """Find the source issue number from a done card.

    Priority: body `issue: <num>`/`#<num>` mention → title `[Issue #N]` →
    body URL `issues/<N>`.
    """
    body = card.get("body") or ""
    title = card.get("title") or ""
    m = re.search(r"(?:^|\s)issue[:\s#]*(\d+)", body, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\[Issue #(\d+)\]", title)
    if m:
        return int(m.group(1))
    m = re.search(r"github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", body)
    if m:
        return int(m.group(1))
    return None


def build_review_comment(card: dict) -> str:
    """Compose the GitHub issue comment that mirrors the card's evidence.

    The comment is the *evidence handoff*: what was done, the result summary,
    and structured metadata (if any). It is deliberately conservative — it
    reports execution output, it does not claim acceptance.
    """
    title = card.get("title") or "(untitled)"
    result = card.get("result") or ""
    completed = card.get("completed_at")
    lines = [
        "## 🤖 执行完成（kanban 自动同步）",
        "",
        "**任务**: %s" % title,
    ]
    if completed:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(completed))
        lines.append("**完成时间**: %s" % ts)
    if result:
        lines += ["", "**结果摘要**:", "", result]
    summary = card.get("summary")
    if summary and summary != result:
        lines += ["", "**交接摘要**:", "", summary]
    metadata = card.get("metadata")
    if metadata:
        try:
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if isinstance(metadata, dict) and metadata:
                lines += ["", "**结构化元数据**:", ""]
                for k, v in metadata.items():
                    lines.append("- %s: %s" % (k, v))
        except (json.JSONDecodeError, TypeError):
            lines += ["", "**元数据**: %s" % metadata]
    lines += [
        "",
        "> 状态由 kanban done 单向映射至 Review，等待 PI 评审。",
        "> 本评论由 sync job 自动生成；证据完整性由报告 watchdog 校验。",
    ]
    return "\n".join(lines)


# ── sync orchestration ────────────────────────────────────────────────

def list_done_cards(board: str | None = None) -> list[dict]:
    argv = ["hermes", "kanban", "list", "--status", "done", "--json"]
    if board:
        argv += ["--tenant", board]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("kanban list failed: %s" % r.stderr.strip()[:200])
    return json.loads(r.stdout or "[]")


def load_synced(state_file: Path) -> set[str]:
    if state_file.exists():
        try:
            return set(json.loads(state_file.read_text()).get("synced", []))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_synced(state_file: Path, synced: set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"synced": sorted(synced)}, indent=2))


def resolve_item_id(repo: str, issue_num: int, project_node: str) -> str:
    """gh api graphql: find the ProjectV2 item id for a linked issue."""
    query = (
        'query($p: ID!) { node(id: $p) { ... on ProjectV2 { items(first: 100) {'
        ' nodes { id content { ... on Issue { number } } } } } } }'
    )
    r = subprocess.run(
        ["gh", "api", "graphql", "-f", "query=%s" % query, "-F", "p=%s" % project_node],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError("item lookup failed: %s" % r.stderr.strip()[:200])
    data = json.loads(r.stdout)
    for node in data["data"]["node"]["items"]["nodes"]:
        if node.get("content") and node["content"].get("number") == issue_num:
            return node["id"]
    return ""


def sync_one_card(card: dict, repo: str, project: dict | None,
                  dry_run: bool = False) -> dict:
    """Post the evidence comment and (optionally) move the issue to Review."""
    issue_num = extract_issue_number(card)
    if issue_num is None:
        return {"card": card["id"], "status": "skipped", "reason": "no issue ref"}
    actions = []

    # 1. Evidence comment
    comment = build_review_comment(card)
    if dry_run:
        actions.append({"action": "comment", "dry_run": True, "body": comment[:80]})
    else:
        r = subprocess.run(
            ["gh", "issue", "comment", str(issue_num), "--repo", repo, "--body", comment],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {"card": card["id"], "status": "failed",
                    "reason": "comment: %s" % r.stderr.strip()[:160]}
        actions.append({"action": "comment", "ok": True})

    # 2. Move to Review on the project board
    if project and issue_num:
        try:
            item_id = resolve_item_id(repo, issue_num, project["node"])
            if not item_id:
                actions.append({"action": "review", "status": "skipped",
                                "reason": "issue not in project"})
            elif dry_run:
                actions.append({"action": "review", "dry_run": True})
            else:
                r = subprocess.run(
                    ["gh", "api", "graphql",
                     "-f", "query=mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) {"
                           " updateProjectV2ItemFieldValue(input: {"
                           " projectId: $p, itemId: $i, fieldId: $f,"
                           " value: {singleSelectOptionId: $o}}) { projectV2Item { id } } }",
                     "-F", "p=%s" % project["node"],
                     "-F", "i=%s" % item_id,
                     "-F", "f=%s" % project["review_field"],
                     "-F", "o=%s" % project["review_option"]],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    return {"card": card["id"], "status": "failed",
                            "reason": "review: %s" % r.stderr.strip()[:160]}
                actions.append({"action": "review", "ok": True})
        except Exception as exc:  # review move is best-effort
            actions.append({"action": "review", "status": "skipped",
                            "reason": str(exc)[:120]})

    return {"card": card["id"], "status": "synced", "issue": issue_num,
            "actions": actions}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--config", type=Path, default=None,
                        help="dispatcher.yaml (for project review mapping)")
    parser.add_argument("--board", default=None, help="kanban board/tenant slug")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_HOME)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project = None
    if args.config:
        raw = yaml.safe_load(Path(args.config).read_text()) or {}
        repo_cfg = (raw.get("repos") or {}).get(args.repo)
        if repo_cfg:
            projects = repo_cfg.get("projects") or []
            for p in projects:
                if p.get("review_field") and p.get("review_option"):
                    project = p
                    break

    cards = list_done_cards(args.board)
    state_file = args.state_dir / ("synced_%s.json" % args.repo.replace("/", "_"))
    synced = load_synced(state_file)
    results = []
    for card in cards:
        if card["id"] in synced:
            continue
        outcome = sync_one_card(card, args.repo, project, dry_run=args.dry_run)
        results.append(outcome)
        if outcome["status"] == "synced" and not args.dry_run:
            synced.add(card["id"])
    if not args.dry_run:
        save_synced(state_file, synced)
    print(json.dumps({"cards": len(cards), "new": len(results),
                      "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
