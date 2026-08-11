#!/usr/bin/env python3
"""Kanban bridge v2 — map GitHub eight-state transitions to kanban cards.

Called by the tick post-processor after the dispatcher emits actions.
Design (2026-08-11 retrospective, codex-reviewed):
- GitHub = decision layer / SSOT (unchanged); kanban = execution layer.
- 1 issue = 1 EXECUTION card (owner) + 1 EV card (auditor), BOTH FIXED.
- Rework = new run on the SAME cards (generation++), NOT new cards.
  This eliminates: concurrent EV audits, branch/worktree conflicts,
  card pileup, and manual status flips.
- Worker writes GitHub status itself (SOUL contract); kanban manages the
  execution lifecycle (heartbeat / retry / reclaim via gateway dispatcher).
- Context continuity: worker records session_id at delivery; rework run
  resumes via `hermes --resume <session_id>`; falls back to handoff doc.

Card lifecycle (status moves, no done until final):
  exec card:  ready → running (worker claim) → blocked「waiting EV」
              → ready (EV REJECT rework) → ... → done (final PASS + PI done)
  ev card:    ready → running (auditor claim) → blocked「waiting rework」
              → ready (rework done) → ... → done (PASS)
"""
import json
import os
import re
import subprocess
import sys
import time

BOARD = os.environ.get("KANBAN_BOARD", "beijing-lot")
EXEC_TAG = "EXEC_CARD"
EV_TAG = "EV_CARD"
GEN_RE = re.compile(r"GENERATION=(\d+)")


def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r


def list_tasks():
    r = run(["hermes", "kanban", "list", "--json"])
    if r.returncode != 0:
        return []
    try:
        d = json.loads(r.stdout)
    except Exception:
        return []
    return d if isinstance(d, list) else d.get("tasks", d.get("items", []))


def find_cards(issue_num, tag=None):
    """Find cards bound to a GitHub issue. tag filters EXEC_CARD / EV_CARD."""
    out = []
    for t in list_tasks():
        body = (t.get("body") or "") + " " + (t.get("title") or "")
        if "#%d" % issue_num not in body and "issue %d" % issue_num not in body.lower():
            continue
        if tag and tag not in body:
            continue
        out.append(t)
    return out


def generation_of(card):
    body = (card.get("body") or "") + " " + (card.get("title") or "")
    m = GEN_RE.search(body)
    return int(m.group(1)) if m else 0


def get_or_create_card(issue_num, title, url, kind, owner):
    """Return the fixed card for (issue, kind); create if absent.

    kind: "exec" (owner worker) or "ev" (auditor). Idempotent — one card
    per issue per kind, reused across rework rounds (generation bumps).
    """
    tag = EXEC_TAG if kind == "exec" else EV_TAG
    cards = find_cards(issue_num, tag)
    if cards:
        return cards[0]

    assignee = owner if kind == "exec" else "auditor"
    workspace = "worktree" if kind == "exec" else "scratch"
    branch = ("%s/bj%d-issue" % (owner, issue_num)) if kind == "exec" else None

    cmd = ["hermes", "kanban", "create",
           "Issue #%d: %s [%s GENERATION=1]" % (issue_num, title, tag),
           "--body", "GitHub issue #%d (%s) — %s\n%s GENERATION=1"
                      % (issue_num, title, url, tag),
           "--assignee", assignee,
           "--workspace", workspace,
           "--idempotency-key", "gh-issue-%d-%s" % (issue_num, tag.lower())]
    if branch:
        cmd += ["--branch", branch]
    r = run(cmd + ["--json"])
    if r.returncode != 0:
        print("⚠️ kanban create %s card failed: %s" % (kind, r.stderr[:150]),
              file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def bump_generation(card, reason):
    """Increment GENERATION in the card title/body (rework round marker)."""
    card_id = card.get("id")
    cur = generation_of(card)
    new = cur + 1
    tag = EXEC_TAG if EXEC_TAG in (card.get("body") or "") else EV_TAG
    title = re.sub(r"GENERATION=\d+", "GENERATION=%d" % new,
                   card.get("title") or "")
    body = re.sub(r"GENERATION=\d+", "GENERATION=%d" % new,
                  card.get("body") or "")
    r = run(["hermes", "kanban", "edit", card_id,
             "--result", "%s (generation %d->%d)" % (reason, cur, new),
             "--metadata", json.dumps({"generation": new})])
    if r.returncode != 0:
        print("⚠️ kanban bump generation %s failed: %s"
              % (card_id, r.stderr[:120]), file=sys.stderr)


def set_status(card_id, action, reason=""):
    """Move a card's status: unblock (→ready) / block (→blocked)."""
    if action == "unblock":
        r = run(["hermes", "kanban", "unblock", card_id, "--reason", reason])
    elif action == "block":
        # block 的 reason 是位置参数 (usage: block task_id [reason ...])
        args = ["hermes", "kanban", "block", card_id]
        if reason:
            args.append(reason)
        r = run(args)
    else:
        return
    if r.returncode != 0:
        print("⚠️ kanban %s %s failed: %s"
              % (action, card_id, r.stderr[:120]), file=sys.stderr)


def complete_card(card_id):
    r = run(["hermes", "kanban", "complete", card_id])
    if r.returncode != 0:
        print("⚠️ kanban complete %s failed: %s" % (card_id, r.stderr[:120]),
              file=sys.stderr)


def handle(action):
    reason = action.get("reason", "")
    node = action.get("node", "")
    state = action.get("state", "")
    role = action.get("role") or "analyst"
    sent = action.get("sent", "")

    parts = node.split(":")
    if len(parts) < 3 or parts[0] != "project":
        return
    try:
        issue_num = int(parts[2])
    except ValueError:
        return

    url = "https://github.com/algotradinglife/beijing-lot/issues/%d" % issue_num
    title = state

    # ── Ready: ensure exec card exists, EV card exists (both fixed) ──
    if reason == "issue_ready":
        get_or_create_card(issue_num, title, url, "exec", role)
        get_or_create_card(issue_num, title, url, "ev", "auditor")

    # ── EV Review: worker delivered → exec card blocked「waiting EV」,
    #    EV card unblocked (auditor re-audits if rework round). ──
    elif reason in ("ev_review_ready", "issue_ev_review"):
        for c in find_cards(issue_num, EXEC_TAG):
            set_status(c.get("id"), "block", "worker delivered, waiting EV")
        for c in find_cards(issue_num, EV_TAG):
            set_status(c.get("id"), "unblock", "EV audit round")

    # ── PI Review: EV PASS → EV card blocked「PASS, waiting PI」 ──
    elif reason == "issue_pi_review":
        for c in find_cards(issue_num, EV_TAG):
            set_status(c.get("id"), "block", "EV PASS, waiting PI terminal review")

    # ── Done: final — complete both cards ──
    elif reason == "issue_done":
        for c in find_cards(issue_num):
            complete_card(c.get("id"))

    # ── Blocked: worker asks PI → exec card blocked ──
    elif reason == "issue_blocked":
        for c in find_cards(issue_num, EXEC_TAG):
            set_status(c.get("id"), "block", "worker blocked on GitHub (issue #%d)" % issue_num)

    # ── Human: needs real person → both cards blocked ──
    elif reason == "issue_human_escalate":
        for c in find_cards(issue_num):
            set_status(c.get("id"), "block", "human intervention needed (issue #%d)" % issue_num)

    # ── In Progress: worker claimed (GitHub In Progress) → ensure exec
    #    card is unblocked so dispatcher can spawn/continue it. ──
    elif reason == "issue_in_progress":
        for c in find_cards(issue_num, EXEC_TAG):
            if c.get("status") == "blocked":
                set_status(c.get("id"), "unblock", "worker resumed (GitHub In Progress)")


def main():
    raw = sys.stdin.read().strip()
    if not raw.startswith("{"):
        return
    try:
        d = json.loads(raw)
    except Exception:
        return
    for a in d.get("actions", []):
        try:
            handle(a)
        except Exception as exc:
            print("⚠️ kanban bridge: %s" % exc, file=sys.stderr)


if __name__ == "__main__":
    main()
