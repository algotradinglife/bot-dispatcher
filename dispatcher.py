#!/usr/bin/env python3
"""
Config-driven GitHub dispatcher for no-agent polling jobs.
Reads repository routing from a local YAML file.
Usage: dispatcher.py --repo <repo_key> [--config <path>]

Routes based on GitHub Issue Graph (blockedBy/blocking/parent/subIssues)
and GitHub Project Status. Sends one /goal per session per tick.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG_HOME = Path(os.environ.get(
    "XDG_CONFIG_HOME", Path.home() / ".config"
)).expanduser()
DEFAULT_STATE_HOME = Path(os.environ.get(
    "XDG_STATE_HOME", Path.home() / ".local" / "state"
)).expanduser()
DEFAULT_CONFIG_FILE = Path(os.environ.get(
    "BOT_DISPATCHER_CONFIG",
    DEFAULT_CONFIG_HOME / "bot-dispatcher" / "dispatcher.yaml",
)).expanduser()
DEFAULT_STATE_DIR = Path(os.environ.get(
    "BOT_DISPATCHER_STATE_DIR",
    DEFAULT_STATE_HOME / "bot-dispatcher",
)).expanduser()
GRAPH_PAGE_SIZE = 100


class ControlPlaneUnavailable(RuntimeError):
    """Raised when GitHub cannot provide authoritative routing state."""


def validate_repo_config(repo_name, cfg):
    if not isinstance(cfg, dict):
        raise ValueError("repo '%s' config must be a mapping" % repo_name)
    repo = cfg.get("repo")
    if not isinstance(repo, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise ValueError("repo '%s' must set repo: owner/name" % repo_name)

    session_map = cfg.get("session_map")
    if not isinstance(session_map, dict) or not session_map.get("pi"):
        raise ValueError("repo '%s' must map the pi session" % repo_name)
    if any(not isinstance(role, str) or not isinstance(session, str) or not session
           for role, session in session_map.items()):
        raise ValueError("repo '%s' session_map entries must be non-empty strings" % repo_name)

    workflow_role = cfg.get("workflow_role", "pi")
    if not isinstance(workflow_role, str) or workflow_role not in session_map:
        raise ValueError(
            "repo '%s' workflow_role is not in session_map" % repo_name
        )

    projects = cfg.get("projects", [])
    if not isinstance(projects, list):
        raise ValueError("repo '%s' projects must be a list" % repo_name)
    project_numbers = set()
    for project in projects:
        if not isinstance(project, dict):
            raise ValueError("repo '%s' project entries must be mappings" % repo_name)
        number = project.get("number")
        node = project.get("node")
        owner = project.get("owner")
        if (not isinstance(number, int) or isinstance(number, bool)
                or number < 1 or number in project_numbers):
            raise ValueError("repo '%s' project numbers must be unique positive integers" % repo_name)
        if not isinstance(node, str) or not node:
            raise ValueError("repo '%s' project %s must set a node ID" % (repo_name, number))
        if owner not in session_map:
            raise ValueError("repo '%s' project %s owner is not in session_map" % (repo_name, number))
        project_numbers.add(number)

    for map_name in ("assignee_map", "mention_map"):
        role_map = cfg.get(map_name, {})
        if not isinstance(role_map, dict):
            raise ValueError("repo '%s' %s must be a mapping" % (repo_name, map_name))
        unknown_roles = sorted({role for role in role_map.values() if role not in session_map})
        if unknown_roles:
            raise ValueError("repo '%s' %s references unknown roles: %s" % (
                repo_name, map_name, ", ".join(unknown_roles)))


def load_config(repo_name, config_file=DEFAULT_CONFIG_FILE):
    config_file = Path(config_file).expanduser()
    if not config_file.exists():
        raise ValueError("config not found at %s" % config_file)
    raw = yaml.safe_load(config_file.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    repos = raw.get("repos", {})
    if not isinstance(repos, dict):
        raise ValueError("config 'repos' must be a mapping")
    cfg = repos.get(repo_name)
    if not cfg:
        raise ValueError("repo '%s' not in config. Available: %s" % (
            repo_name, ", ".join(sorted(repos.keys()))))
    validate_repo_config(repo_name, cfg)
    return cfg


def project_state_key(project_number, item_number, field="status"):
    return "project:%d:%d:%s" % (project_number, item_number, field)


def build_session_map(cfg):
    sm = cfg.get("session_map", {})
    return sm, {v: k for k, v in sm.items()}


def resolve_workflow_session(cfg, session_map):
    """Resolve the operational coordinator while keeping PI as the default."""
    return session_map.get(cfg.get("workflow_role", "pi"))


def resolve_owner(proj_num, projects, sm):
    for p in projects:
        if p["number"] == proj_num:
            role = p.get("owner")
            return sm.get(role) if role else None
    return None


def resolve_assignee_session(assignee_login, assignee_map, sm):
    role = assignee_map.get(assignee_login)
    return sm.get(role) if role else None


def resolve_author_default_session(author_login, assignee_map, sm, owner_session):
    """Author-identity fallback routing when a comment cannot be routed by an
    explicit [TO:] directive or Project ownership:
    - a message from the PI account (hh1985) defaults to the owner session
      (the worker who owns the Issue/PR's Project);
    - a message from a worker account (e.g. everything-bot-engineer) defaults
      to the PI session.
    Returns None when the author maps to neither role, so the caller can
    fail loudly (warning) instead of silently dropping the message."""
    role = assignee_map.get(author_login)
    if role == "pi":
        return owner_session
    if role and role != "pi":
        return sm.get("pi")
    return None


def build_mention_map(cfg, sm):
    mm = {}
    raw = cfg.get("mention_map", {})
    for keyword, role in raw.items():
        session = sm.get(role)
        if session:
            mm[keyword] = session
    return mm


def resolve_target_to_session(target, mention_map):
    tl = target.lower()
    for kw, s in mention_map.items():
        if kw.lower() == tl:
            return s
    return None


def parse_to_directive(body, mention_map):
    for line in body.split('\n'):
        s = line.strip()
        if s.startswith('>') or not s:
            continue
        # Recognized shapes on the first content line:
        #   [TO: PI] | [PI DEPENDENCY CLOSURE][TO: STRATEGY]
        #   /goal [TO: Worker][...] | [TO: PI / FRESH REVIEW]
        # /goal and leading [label] groups are optional; the first token
        # after [TO: is the target. Anchored so inline prose is never read
        # as a directive.
        m = re.match(
            r'(?:/goal\s+)?(?:\[(?!TO:)[^\]]*\]\s*)*'
            r'\[TO:\s*([A-Za-z0-9_-]+)(?:\s*/[^\]]*)?\]',
            s, re.IGNORECASE)
        if m:
            t = m.group(1)
            return t, resolve_target_to_session(t, mention_map)
        break
    return None, None


def format_goal(title, url):
    """One-line task reminder: action + link, no prose. Sets the session goal."""
    return "/goal %s\n%s" % (title, url)


def format_notice(title, url):
    """One-line informational notice: action + link, no goal semantics."""
    return "%s\n%s" % (title, url)


def queue_goal(output, session, message):
    """Retain every distinct event; one session digest is emitted per tick.

    Dedupes identical messages within the same tick: two comments on the same
    PR/issue with the same [TO:] target produce the same goal text, and
    sending both would duplicate work in the session digest.
    """
    if not session:
        return
    pending = output.setdefault("_pending", {}).setdefault(session, [])
    if message not in pending:
        pending.append(message)


def queue_workflow_issue_transition(
    cfg,
    session_map,
    state_key,
    issue_num,
    title,
    previous_status,
    current_status,
    url,
    primary_session,
    output,
):
    """Give an explicitly configured PM visibility into Issue lifecycle work."""
    if "workflow_role" not in cfg:
        return
    workflow_session = resolve_workflow_session(cfg, session_map)
    if not workflow_session or workflow_session == primary_session:
        return

    message = format_goal(
        "PM coordination: Issue #%d -> %s — %s"
        % (issue_num, current_status, title),
        url,
    )
    queue_goal(output, workflow_session, message)
    output["actions"].append({
        "node": "workflow:%s" % state_key,
        "state": current_status,
        "session": workflow_session,
        "reason": "issue_status_coordinator",
        "prev_status": previous_status,
        "sent": message[:80],
        "result": "queued",
    })


def load_state(state_file):
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            return {}
    return {}


def save_state(state_file, state):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))


def gql_query(query, retries=4, base_delay=1.0):
    """Run a GraphQL query with retry on transient failures (rate limit, network)."""
    last_err = None
    for attempt in range(retries):
        try:
            r = subprocess.run(["gh", "api", "graphql", "-f", "query=%s" % query],
                               capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        else:
            if r.returncode != 0:
                last_err = r.stderr.strip()[:160]
            else:
                try:
                    payload = json.loads(r.stdout)
                except Exception as exc:
                    last_err = "invalid JSON: %s" % exc
                else:
                    if payload.get("errors"):
                        # Retry only on transient error kinds; fail fast otherwise
                        err_str = str(payload["errors"])
                        transient = any(k in err_str for k in (
                            "rate limit", "rate_limit", "abuse", "internal", "timeout",
                            "connection", "Network error", "ETIMEDOUT", "EOF",
                            "connection reset", "refused", "TLS", "503", "502",
                        ))
                        if not transient:
                            raise ControlPlaneUnavailable(
                                "GitHub GraphQL errors: %s" % err_str[:160])
                        last_err = err_str[:160]
                    else:
                        return payload
        if attempt < retries - 1:
            time.sleep(base_delay * (2 ** attempt))
    raise ControlPlaneUnavailable("GitHub GraphQL failed: %s" % (last_err or "unknown"))


def _issue_relation(repo, issue_num, relation):
    owner, name = repo.split("/", 1)
    numbers = set()
    cursor = None
    while True:
        after = "null" if cursor is None else json.dumps(cursor)
        query = (
            'query { repository(owner:%s, name:%s) { issue(number:%d) { '
            '%s(first:%d, after:%s) { nodes { number } '
            'pageInfo { hasNextPage endCursor } } } } }'
        ) % (json.dumps(owner), json.dumps(name), issue_num, relation,
             GRAPH_PAGE_SIZE, after)
        data = gql_query(query)
        issue = data.get("data", {}).get("repository", {}).get("issue")
        if issue is None:
            raise ControlPlaneUnavailable("Issue #%d unavailable from GitHub Graph" % issue_num)
        connection = issue.get(relation)
        if not isinstance(connection, dict):
            raise ControlPlaneUnavailable("Issue #%d relation %s unavailable" % (issue_num, relation))
        numbers.update(node["number"] for node in connection.get("nodes", []) if node)
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return numbers
        cursor = page.get("endCursor")
        if not cursor:
            raise ControlPlaneUnavailable("Issue Graph pagination cursor missing")


def get_issue_graph(repo, issue_num):
    owner, name = repo.split("/", 1)
    query = (
        'query { repository(owner:%s, name:%s) { issue(number:%d) '
        '{ number parent { number } } } }'
    ) % (json.dumps(owner), json.dumps(name), issue_num)
    data = gql_query(query)
    issue = data.get("data", {}).get("repository", {}).get("issue")
    if issue is None:
        raise ControlPlaneUnavailable("Issue #%d unavailable from GitHub Graph" % issue_num)
    return {
        "parent": issue.get("parent", {}).get("number") if issue.get("parent") else None,
        "blocked_by": _issue_relation(repo, issue_num, "blockedBy"),
        "blocking": _issue_relation(repo, issue_num, "blocking"),
        "sub_issues": _issue_relation(repo, issue_num, "subIssues"),
    }


def build_issue_proj_map(cfg):
    items = get_project_items(cfg)
    mm = {}
    for item in items:
        if item.get("is_pr"):
            continue
        number = item["number"]
        project_num = item["project_num"]
        if number in mm and mm[number] != project_num:
            raise ControlPlaneUnavailable(
                "Issue #%d has conflicting configured Project memberships" % number
            )
        mm[number] = project_num
    return mm, items


def find_owner_for_issue(issue_num, proj_map, projects, sm):
    pn = proj_map.get(issue_num)
    if pn is None:
        return None
    for proj in projects:
        if proj["number"] == pn:
            role = proj.get("owner")
            return sm.get(role) if role else None
    return None


def notify_graph_stakeholders(repo, issue_num, title, cur_s, url, graph,
                              proj_map, projects, sm, output):
    related = set()
    if graph["parent"]:
        related.add(graph["parent"])
    related.update(graph["blocked_by"])
    related.update(graph["blocking"])
    related.update(graph["sub_issues"])

    for other_num in sorted(related):
        if other_num == issue_num:
            continue
        session = find_owner_for_issue(other_num, proj_map, projects, sm)
        if not session:
            continue
        rels = []
        if graph["parent"] and other_num == graph["parent"]:
            rels.append("parent")
        if other_num in graph["blocked_by"]:
            rels.append("blocker")
        if other_num in graph["blocking"]:
            rels.append("dependent")
        if other_num in graph["sub_issues"]:
            rels.append("sub-issue")
        rel_str = ", ".join(rels) if rels else "related"
        other_url = "https://github.com/%s/issues/%d" % (repo, other_num)
        msg = format_goal("Issue #%d → %s — affects #%d (%s)" % (issue_num, cur_s, other_num, rel_str),
                          other_url)
        queue_goal(output, session, msg)


def get_project_items(cfg):
    """Fetch all items from configured Projects with cursor pagination."""
    result = []
    for proj in cfg.get("projects", []):
        pn = proj["number"]
        pid = proj["node"]
        cursor = None
        while True:
            after = "null" if cursor is None else json.dumps(cursor)
            query = (
                '{node(id:%s){... on ProjectV2{items(first:%d,after:%s){'
                'nodes{id content{__typename ... on Issue{number title} '
                '... on PullRequest{number title}} fieldValues(first:20){nodes{'
                '__typename ... on ProjectV2ItemFieldSingleSelectValue{name '
                'field{... on ProjectV2SingleSelectField{name}}}}}} '
                'pageInfo{hasNextPage endCursor}}}}}'
            ) % (json.dumps(pid), GRAPH_PAGE_SIZE, after)
            data = gql_query(query)
            project = data.get("data", {}).get("node")
            if not isinstance(project, dict):
                raise ControlPlaneUnavailable("Project #%d unavailable" % pn)
            connection = project.get("items")
            if not isinstance(connection, dict):
                raise ControlPlaneUnavailable("Project #%d items unavailable" % pn)
            for node in connection.get("nodes", []):
                content = node.get("content")
                if not content or not content.get("number"):
                    continue
                status = "Inbox"
                for value in node.get("fieldValues", {}).get("nodes", []):
                    if not isinstance(value, dict):
                        continue
                    field = value.get("field")
                    if (value.get("__typename") == "ProjectV2ItemFieldSingleSelectValue"
                            and isinstance(field, dict) and field.get("name") == "Status"):
                        status = value.get("name", "Inbox")
                        break
                result.append({
                    "number": content["number"],
                    "status": status,
                    "project_num": pn,
                    "project_name": proj.get("name", str(pn)),
                    "_item_id": node["id"],
                    "is_pr": content.get("__typename") == "PullRequest",
                })
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                raise ControlPlaneUnavailable("Project #%d pagination cursor missing" % pn)
    return result


def linked_issue_numbers(body):
    return {
        int(value)
        for value in re.findall(
            r"(?i)(?:close[sd]?|fix(?:es)?|resolve[sd]?)\s+#(\d+)",
            body or "",
        )
    }


def resolve_pr_session(pr, proj_map, projects, sm, assignee_map):
    """Prefer linked Issue Project ownership over ambiguous shared bot authorship."""
    linked = linked_issue_numbers(pr.get("body", ""))
    sessions = {
        find_owner_for_issue(number, proj_map, projects, sm)
        for number in linked
    }
    sessions.discard(None)
    if len(sessions) == 1:
        return next(iter(sessions))
    if linked:
        return None
    author = pr.get("author", {})
    login = author.get("login", "") if isinstance(author, dict) else str(author)
    return resolve_assignee_session(login, assignee_map, sm)


def resolve_worker_session(pr, proj_map, projects, sm):
    """Resolve the assigned worker for a PR strictly by linked-Issue Project
    ownership. Unrecognized [TO:] aliases (e.g. \"Worker\") are routed by the
    Project that owns the linked Issue — never by author/assignee semantics.
    Returns None unless every linked Issue resolves to the same single
    Project owner (fail closed on any unmapped or conflicting issue)."""
    linked = linked_issue_numbers(pr.get("body", ""))
    if not linked:
        return None
    sessions = {
        find_owner_for_issue(number, proj_map, projects, sm)
        for number in linked
    }
    if len(sessions) == 1 and None not in sessions:
        return next(iter(sessions))
    return None


def resolve_issue_worker_session(issue_num, proj_map, projects, sm):
    """Resolve the assigned worker for an Issue strictly by its Project
    ownership. Unrecognized [TO:] aliases on an Issue comment route to the
    Project that owns the Issue; None if unmapped."""
    return find_owner_for_issue(issue_num, proj_map, projects, sm)


def check_linked_pr(repo, issue_num, assignee_map, sm, proj_map, projects):
    r = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                        "--json", "number,title,author,reviewDecision,body",
                        "--jq", "."],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise ControlPlaneUnavailable("Unable to list linked PRs")
    for pr in json.loads(r.stdout):
        if issue_num not in linked_issue_numbers(pr.get("body", "")):
            continue
        if pr.get("reviewDecision") == "CHANGES_REQUESTED":
            session = resolve_pr_session(pr, proj_map, projects, sm, assignee_map)
            if session:
                url = "https://github.com/%s/pull/%d" % (repo, pr["number"])
                message = format_goal(
                    "PR #%d needs changes — Issue #%d" % (pr["number"], issue_num),
                    url,
                )
                return session, message, pr["number"]
    return None


def extract_report_url(repo, issue_num):
    """Find the analysis-report link for a completed Issue: inspect linked PR
    bodies and comments for report paths (results/... or doc/repro/...) or
    wiki links, preferring the most recently merged/open PR. Returns a
    GitHub URL (report path or wiki link) or None."""
    report_patterns = (
        re.compile(r'(?:results|doc/repro)/(?!\.\.)[A-Za-z0-9_./-]+(?<![./])', re.IGNORECASE),
        re.compile(r'https://github\.com/%s/wiki/[A-Za-z0-9_./#-]+' % re.escape(repo), re.IGNORECASE),
    )
    try:
        r = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "all",
                            "--limit", "100",
                            "--json", "number,title,state,body,mergedAt,updatedAt",
                            "--jq", ".[] | select(.body != null)"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return None
        candidates = []
        for pr in json.loads(r.stdout):
            if issue_num not in linked_issue_numbers(pr.get("body", "")):
                continue
            candidates.append(pr)
        # Most recently updated/merged first.
        candidates.sort(key=lambda p: p.get("mergedAt") or p.get("updatedAt") or "", reverse=True)
        texts = []
        for pr in candidates:
            texts.append(pr.get("body", ""))
            try:
                cr = subprocess.run(["gh", "pr", "view", str(pr["number"]), "--repo", repo,
                                     "--json", "comments", "--jq", ".comments[].body"],
                                    capture_output=True, text=True, timeout=15)
                if cr.returncode == 0 and cr.stdout.strip():
                    texts.extend(json.loads(cr.stdout))
            except Exception:
                pass
        for text in texts:
            for pat in report_patterns:
                m = pat.search(text or "")
                if m:
                    path = m.group(0)
                    if path.startswith("http"):
                        return path
                    return "https://github.com/%s/blob/main/%s" % (repo, path)
    except Exception:
        pass
    return None


def submit_session_enter(session):
    """Submit a goal that agent-deck may have only pasted into the TUI.

    Deliberate fallback: agent-deck `session send --no-wait` normally submits
    the message itself, but on some sessions it only pastes into the input
    buffer. The extra Enter guarantees delivery; without it, goals can sit
    unsubmitted at the prompt.
    """
    shown = subprocess.run(
        ["agent-deck", "session", "show", session, "--json"],
        capture_output=True, text=True, timeout=10,
    )
    if shown.returncode != 0:
        return False, "session lookup failed: %s" % shown.stderr.strip()[:120]
    try:
        tmux_session = json.loads(shown.stdout).get("tmux_session")
    except (json.JSONDecodeError, AttributeError):
        return False, "session lookup returned invalid JSON"
    if not tmux_session:
        return False, "session lookup returned no tmux_session"
    submitted = subprocess.run(
        ["tmux", "send-keys", "-t", tmux_session, "Enter"],
        capture_output=True, text=True, timeout=10,
    )
    if submitted.returncode != 0:
        return False, "Enter submission failed: %s" % submitted.stderr.strip()[:120]
    return True, "ok"


def flush_goals(output, dry_run=False, baseline=False):
    """Send one digest per session while retaining every queued event.

    baseline=True (first run): the digest is reported but never delivered —
    a fresh repo joins from the moment of its first tick, historical events
    are recorded as seen without replay.
    """
    pending = output.pop("_pending", {})
    all_success = True
    for session, messages in pending.items():
        if isinstance(messages, str):
            messages = [messages]
        digest = "\n\n".join(messages)
        if baseline:
            success = True
            outcome = "baseline-skipped"
        elif dry_run:
            success = True
            outcome = "dry-run"
        else:
            result = subprocess.run(
                ["agent-deck", "session", "send", session, "--no-wait", digest],
                capture_output=True, text=True, timeout=30,
            )
            success = result.returncode == 0
            outcome = "ok" if success else "FAILED: %s" % result.stderr.strip()[:160]
            if success:
                success, enter_outcome = submit_session_enter(session)
                outcome = "ok + Enter" if success else "FAILED: %s" % enter_outcome
        all_success = all_success and success
        output["actions"].append({
            "node": "goal:%s" % session,
            "state": "dry_run" if dry_run else ("baseline" if baseline else ("sent" if success else "pending_retry")),
            "session": session,
            "reason": "goal_digest",
            "event_count": len(messages),
            "result": outcome,
        })
    return all_success


def main():
    parser = argparse.ArgumentParser(description="Generic repo dispatcher")
    parser.add_argument("--repo", required=True, help="Repository key from the config file")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_FILE,
                        help="YAML config path (default: %(default)s)")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="State directory (default: %(default)s)")
    parser.add_argument("--validate-config", action="store_true",
                        help="Validate the selected repository config and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan once without sending messages or updating state")
    args = parser.parse_args()

    try:
        cfg = load_config(args.repo, args.config)
    except ValueError as exc:
        parser.error(str(exc))
    if args.validate_config:
        print(json.dumps({"repo_key": args.repo, "repo": cfg["repo"], "valid": True}))
        return

    repo = cfg["repo"]
    projects = cfg.get("projects", [])
    sm, _ = build_session_map(cfg)
    coordinator_session = resolve_workflow_session(cfg, sm)
    assignee_map = cfg.get("assignee_map", {})
    mention_map = build_mention_map(cfg, sm)
    safe_repo_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.repo)
    state_file = args.state_dir.expanduser() / ("dispatcher_%s_state.json" % safe_repo_key)
    prefix = "[%s]" % args.repo

    output = {"ts": time.time(), "actions": [], "warnings": [], "_pending": {}}
    prev_state = load_state(state_file)
    new_state = dict(prev_state)
    # First run (no state file): baseline only — never replay historical events.
    # New repos join the dispatcher from the moment of first tick; backlog is dropped.
    first_run = not state_file.exists()
    proj_map = {}
    control_plane_ok = False

    # ── 1. Project Status changes ──
    try:
        proj_map, items = build_issue_proj_map(cfg)
        # Fetch issue graphs in parallel (one GraphQL call per issue; large repos
        # benefit from concurrency instead of serial subprocess latency)
        changed_issues = [
            item["number"]
            for item in items
            if not item.get("is_pr")
            and prev_state.get(
                project_state_key(item["project_num"], item["number"]),
                "Inbox",
            ) != item.get("status", "Inbox")
        ]
        issue_graphs = {}
        if changed_issues:
            try:
                from concurrent.futures import ThreadPoolExecutor

                def _fetch_graph(num):
                    return num, get_issue_graph(repo, num)

                with ThreadPoolExecutor(max_workers=min(8, len(changed_issues))) as ex:
                    for num, graph in ex.map(_fetch_graph, changed_issues):
                        issue_graphs[num] = graph
            except Exception:
                # Fall back to serial if concurrency fails
                for num in changed_issues:
                    issue_graphs[num] = get_issue_graph(repo, num)
        for item in items:
            issue_num = item["number"]
            pn = item["project_num"]
            sk = project_state_key(pn, issue_num)
            prev_s = prev_state.get(sk, "Inbox")
            cur_s = item.get("status", "Inbox")

            if prev_s == cur_s:
                if sk not in prev_state:
                    new_state[sk] = cur_s
                continue

            # Fetch title (pr view for PRs, issue view for issues)
            if item.get("is_pr"):
                ir = subprocess.run(["gh", "pr", "view", str(issue_num), "--repo", repo,
                                     "--json", "title", "--jq", ".title"],
                                    capture_output=True, text=True, timeout=10)
            else:
                ir = subprocess.run(["gh", "issue", "view", str(issue_num), "--repo", repo,
                                     "--json", "title", "--jq", ".title"],
                                    capture_output=True, text=True, timeout=10)
            if ir.returncode != 0:
                raise ControlPlaneUnavailable(
                    "Unable to read title for %s #%d" %
                    ("PR" if item.get("is_pr") else "Issue", issue_num)
                )
            title = ir.stdout.strip()
            url = ("https://github.com/%s/pull/%d" if item.get("is_pr") else "https://github.com/%s/issues/%d") % (repo, issue_num)
            owner = resolve_owner(pn, projects, sm)

            session = None
            msg = None
            reason = None

            # PRs on project board — only Review status matters
            if item.get("is_pr"):
                if cur_s == "Review":
                    session = coordinator_session
                    if session:
                        msg = format_goal("PR #%d is in REVIEW — %s" % (issue_num, title),
                                          url)
                        reason = "pr_review_ready"
                        queue_goal(output, session, msg)
                        output["actions"].append({"node": sk, "state": cur_s, "session": session, "reason": reason,
                                                  "prev_status": prev_s, "sent": msg[:80], "result": "queued"})
                new_state[sk] = cur_s
                continue

            if cur_s == "Ready":
                session = owner
                if session:
                    msg = format_goal("Issue #%d is READY — %s" % (issue_num, title),
                                      url)
                    reason = "issue_ready"

            elif cur_s == "Blocked":
                lp = check_linked_pr(repo, issue_num, assignee_map, sm, proj_map, projects)
                if lp:
                    session, msg, _ = lp
                    reason = "pr_changes_needed"
                else:
                    output["warnings"].append("%s Issue #%d is BLOCKED — no linked PR" % (prefix, issue_num))
                    new_state[sk] = cur_s
                    continue

            elif cur_s == "Review":
                try:
                    r = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                                        "--json", "number,title,author,reviewDecision,body",
                                        "--jq", "."],
                                       capture_output=True, text=True, timeout=10)
                    if r.returncode != 0:
                        raise ControlPlaneUnavailable(
                            "Unable to resolve linked PR for Issue #%d" % issue_num
                        )
                    linked_pr = None
                    for pr in json.loads(r.stdout):
                        if issue_num in linked_issue_numbers(pr.get("body", "")):
                            linked_pr = pr
                            break
                    if linked_pr:
                        pu = "https://github.com/%s/pull/%d" % (repo, linked_pr["number"])
                        if linked_pr.get("reviewDecision") == "CHANGES_REQUESTED":
                            linked_session = resolve_pr_session(
                                linked_pr, proj_map, projects, sm, assignee_map
                            )
                            if linked_session:
                                session = linked_session
                                msg = format_goal(
                                    "PR #%d needs changes — Issue #%d in Review"
                                    % (linked_pr["number"], issue_num),
                                    pu,
                                )
                                reason = "review_pr_changes"
                        else:
                            session = coordinator_session
                            msg = format_goal(
                                "Issue #%d in Review — PR #%d awaiting coordination"
                                % (issue_num, linked_pr["number"]),
                                pu,
                            )
                            reason = "review_pr_ready"
                except ControlPlaneUnavailable:
                    raise
                except Exception as exc:
                    raise ControlPlaneUnavailable(
                        "Invalid linked PR data for Issue #%d" % issue_num
                    ) from exc

                if not session:
                    output["warnings"].append("%s Issue #%d in REVIEW — no linked PR" % (prefix, issue_num))
                    new_state[sk] = cur_s
                    continue

            elif cur_s == "Done":
                session = owner
                if session:
                    msg = format_notice("Issue #%d is DONE — %s" % (issue_num, title),
                                        url)
                    report = extract_report_url(repo, issue_num)
                    if report:
                        msg = format_notice(
                            "Issue #%d is DONE — %s (report: %s)" % (issue_num, title, report),
                            url)
                    reason = "issue_done"

            if session and msg:
                queue_goal(output, session, msg)
                output["actions"].append({"node": sk, "state": cur_s, "session": session, "reason": reason,
                                          "prev_status": prev_s, "sent": msg[:80], "result": "queued"})

            queue_workflow_issue_transition(
                cfg,
                sm,
                sk,
                issue_num,
                title,
                prev_s,
                cur_s,
                url,
                session,
                output,
            )

            # Notify graph stakeholders
            notify_graph_stakeholders(
                repo, issue_num, title, cur_s, url, issue_graphs[issue_num],
                proj_map, projects, sm, output)

            new_state[sk] = cur_s

        control_plane_ok = True
    except Exception as e:
        output["_pending"] = {}
        new_state = dict(prev_state)
        output["warnings"].append("%s control plane unavailable: %s" % (prefix, str(e)[:160]))

    # ── 2. Issue comments [TO: ...] fallback ──
    warned_issue_comments = set()  # (issue, author) already warned this tick
    try:
        r = subprocess.run(["gh", "issue", "list", "--repo", repo, "--state", "open",
                            "--json", "number,title", "--jq", "."],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            for iss in json.loads(r.stdout):
                num = iss["number"]
                title = iss["title"]
                try:
                    cr = subprocess.run(["gh", "issue", "view", str(num), "--repo", repo,
                                         "--json", "comments", "--jq", ".comments"],
                                        capture_output=True, text=True, timeout=15)
                    if cr.returncode != 0:
                        continue
                    for c in json.loads(cr.stdout):
                        try:
                            cid = c.get("id", "")
                            if not cid:
                                continue
                            body = c.get("body", "")
                            author = c.get("author", {}).get("login", "unknown")
                            target, tsession = parse_to_directive(body, mention_map)
                            ock = "comment:%s" % cid
                            ck = "comment:%d:%s:%s" % (num, cid, target.lower()) if target else ock
                            if prev_state.get(ock) or prev_state.get(ck):
                                continue
                            if first_run:
                                # Baseline mode (first run): remember the comment,
                                # never replay historical directives.
                                new_state[ck] = "seen"
                                continue
                            if target and not tsession:
                                # [TO: <alias>] not in mention_map (e.g. "Worker")
                                # — the assigned worker is the Project that owns
                                # this Issue.
                                tsession = resolve_issue_worker_session(
                                    num, proj_map, projects, sm
                                )
                            if not tsession:
                                # No explicit [TO:] target (or unresolvable) —
                                # fall back to author identity: PI messages
                                # default to the Issue's Project owner; worker
                                # messages default to the PI session. Never
                                # silently drop a routed author's message.
                                owner = resolve_issue_worker_session(
                                    num, proj_map, projects, sm
                                )
                                tsession = resolve_author_default_session(
                                    author, assignee_map, sm, owner
                                )
                            if not tsession:
                                # Truly unroutable (unknown author, no owner) —
                                # record a warning instead of disappearing it.
                                new_state[ck] = "seen"
                                wkey = (num, author)
                                if wkey not in warned_issue_comments:
                                    warned_issue_comments.add(wkey)
                                    why = ("[TO: %s] unresolvable" % target
                                           if target else "no [TO:] directive")
                                    output["warnings"].append(
                                        "%s Issue #%d comment by @%s unroutable "
                                        "(%s, no Project owner, author unmapped)"
                                        % (prefix, num, author, why)
                                    )
                                continue
                            url = "https://github.com/%s/issues/%d" % (repo, num)
                            if target:
                                msg = format_goal("[TO: %s] from @%s on Issue #%d — %s" % (target, author, num, title), url)
                            else:
                                msg = format_notice("Comment by @%s on Issue #%d — %s" % (author, num, title), url)
                            queue_goal(output, tsession, msg)
                            new_state[ck] = "forwarded"
                            output["actions"].append({"node": ck, "state": "forwarded", "session": tsession, "reason": "to_directive",
                                                      "target": target, "sent": msg[:80], "result": "queued"})
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception as e:
        output["warnings"].append("%s comment scan: %s" % (prefix, str(e)[:80]))

    # ── 3. PR monitoring ──
    prs = []
    try:
        pr_r = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                               "--json", "number,title,headRefName,author,createdAt,mergeStateStatus,body,isDraft",
                               "--jq", "."],
                              capture_output=True, text=True, timeout=15)
        if pr_r.returncode == 0:
            prs = json.loads(pr_r.stdout)
            for pr in prs:
                pn = pr["number"]
                pk = "pr:%d" % pn
                pu = "https://github.com/%s/pull/%d" % (repo, pn)
                author = pr["author"]["login"]
                is_draft = pr.get("isDraft", False)

                # 0. Draft->Ready transition -> workflow coordinator
                dk = "prdraft:%d" % pn
                was_draft = prev_state.get(dk)
                if was_draft is True and not is_draft:
                    msg = format_goal("PR #%d is now READY FOR REVIEW — %s" % (pn, pr["title"]),
                                      pu)
                    queue_goal(output, coordinator_session, msg)
                    output["actions"].append({"node": dk, "state": "ready", "session": coordinator_session,
                                              "reason": "pr_draft_ready", "sent": msg[:80], "result": "queued"})
                new_state[dk] = is_draft

                # 3a. New PR -> workflow coordinator
                if prev_state.get(pk) != "open":
                    msg = format_notice("New PR #%d: %s by @%s" % (pn, pr["title"], author),
                                      pu)
                    queue_goal(output, coordinator_session, msg)
                    output["actions"].append({"node": pk, "state": "open", "session": coordinator_session, "reason": "new_pr",
                                              "sent": msg[:80], "result": "queued"})
                    new_state[pk] = "open"

                # 3b. Review state changes
                try:
                    if not control_plane_ok:
                        raise ControlPlaneUnavailable(
                            "Project/Graph unavailable for PR owner routing"
                        )
                    rv = subprocess.run(["gh", "pr", "view", str(pn), "--repo", repo,
                                         "--json", "reviewDecision", "--jq", ".reviewDecision"],
                                        capture_output=True, text=True, timeout=10)
                    if rv.returncode == 0:
                        decision = rv.stdout.strip()
                        rk = "prreview:%d" % pn
                        pd = prev_state.get(rk)
                        if decision and (not pd or pd != decision):
                            asession = resolve_pr_session(pr, proj_map, projects, sm, assignee_map)
                            if asession:
                                if decision == "APPROVED":
                                    reason = "pr_approved"
                                    t = "PR #%d has been **APPROVED** — %s" % (pn, pr["title"])
                                elif decision == "CHANGES_REQUESTED":
                                    reason = "pr_changes_requested"
                                    t = "PR #%d has **CHANGES REQUESTED** — %s" % (pn, pr["title"])
                                else:
                                    reason = "pr_review_changed"
                                    t = "PR #%d review updated — %s" % (pn, pr["title"])
                                if decision == "CHANGES_REQUESTED":
                                    msg = format_goal(t, pu)  # action required
                                else:
                                    msg = format_notice(t, pu)  # informational
                                queue_goal(output, asession, msg)
                                output["actions"].append({"node": rk, "state": decision, "session": asession, "reason": reason,
                                                          "sent": msg[:80], "result": "queued"})
                            else:
                                msg = format_goal("PR #%d review: %s — unresolved owner" % (pn, decision),
                                                  pu)
                                queue_goal(output, coordinator_session, msg)
                                output["actions"].append({"node": rk, "state": decision, "session": coordinator_session,
                                                          "reason": "pr_unclear_owner", "sent": msg[:80], "result": "queued"})

                        if decision:
                            new_state[rk] = decision
                except Exception:
                    pass

            # 3c. Track closed/merged + recent merges
            if not control_plane_ok:
                raise ControlPlaneUnavailable(
                    "Project/Graph unavailable for merged PR owner routing"
                )
            open_keys = {"pr:%d" % p["number"] for p in prs}
            merged_keys = {k for k, v in prev_state.items()
                           if k.startswith("pr:") and not k.startswith("prreview:")
                           and v in ("merged", "closed")}

            try:
                mr = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "merged",
                                     "--json", "number,title,mergedAt,author,mergedBy,body",
                                     "--jq", ".[:10]"],
                                    capture_output=True, text=True, timeout=10)
                if mr.returncode == 0:
                    for mp in json.loads(mr.stdout):
                        pn = mp["number"]
                        mk = "pr:%d" % pn
                        if mk not in merged_keys and prev_state.get(mk) != "merged":
                            new_state[mk] = "merged"
                            author = mp.get("author", {}).get("login", "unknown")
                            asession = resolve_pr_session(
                                mp, proj_map, projects, sm, assignee_map
                            )
                            pu = "https://github.com/%s/pull/%d" % (repo, pn)
                            if asession:
                                msg = format_notice("PR #%d has been **MERGED**! — %s" % (pn, mp.get("title", "")),
                                                  pu)
                                queue_goal(output, asession, msg)
                                output["actions"].append({"node": mk, "state": "merged", "session": asession,
                                                          "reason": "pr_merged_recent", "sent": msg[:80], "result": "queued"})
                            else:
                                msg = format_notice("PR #%d was merged — unclear who to notify" % pn,
                                                  pu)
                                queue_goal(output, coordinator_session, msg)
                                output["actions"].append({"node": mk, "state": "merged", "session": coordinator_session,
                                                          "reason": "pr_merged_unmapped_recent", "sent": msg[:80], "result": "queued"})
            except Exception:
                pass

            for key in list(prev_state):
                if key.startswith("pr:") and not key.startswith("prreview:"):
                    if key in open_keys:
                        continue
                    pn = int(key.split(":")[1])
                    try:
                        cl = subprocess.run(["gh", "pr", "view", str(pn), "--repo", repo,
                                             "--json", "state,mergedAt,author,title,body",
                                             "--jq", "{state, mergedAt, author: .author.login, title, body}"],
                                            capture_output=True, text=True, timeout=10)
                        if cl.returncode == 0:
                            info = json.loads(cl.stdout)
                            if info.get("state") == "MERGED":
                                if new_state.get(key) == "merged":
                                    continue  # already handled by recent-merges path
                                new_state[key] = "merged"
                                author = info.get("author", "")
                                asession = resolve_pr_session(
                                    {"author": {"login": author}, "body": info.get("body", "")},
                                    proj_map, projects, sm, assignee_map,
                                )
                                if asession and prev_state.get(key) != "merged":
                                    pu = "https://github.com/%s/pull/%d" % (repo, pn)
                                    msg = format_notice("PR #%d has been **MERGED**! — %s" % (pn, info.get("title", "")),
                                                      pu)
                                    queue_goal(output, asession, msg)
                                    output["actions"].append({"node": key, "state": "merged", "session": asession,
                                                              "reason": "pr_merged", "sent": msg[:80], "result": "queued"})
                                elif not asession and prev_state.get(key) != "merged":
                                    pu = "https://github.com/%s/pull/%d" % (repo, pn)
                                    msg = format_notice("PR #%d was merged — unclear who to notify" % pn,
                                                      pu)
                                    queue_goal(output, coordinator_session, msg)
                                    output["actions"].append({"node": key, "state": "merged", "session": coordinator_session,
                                                              "reason": "pr_merged_unmapped", "sent": msg[:80], "result": "queued"})
                    except Exception:
                        pass
    except Exception as e:
        output["warnings"].append("%s PR scan: %s" % (prefix, str(e)[:80]))

    # ── 3d. PR comments [TO: ...] fallback ──
    warned_pr_comments = set()  # (pr, author) already warned this tick
    try:
        for pr in prs:
            pn = pr["number"]
            cr = subprocess.run(["gh", "pr", "view", str(pn), "--repo", repo,
                                 "--json", "comments",
                                 "--jq", ".comments"],
                                capture_output=True, text=True, timeout=15)
            if cr.returncode != 0:
                continue
            for c in json.loads(cr.stdout):
                cid = c.get("id", "")
                if not cid:
                    continue
                body = c.get("body", "")
                author = c.get("author", {}).get("login", "unknown")
                target, tsession = parse_to_directive(body, mention_map)
                ock = "prcomment:%s" % cid
                ck = "prcomment:%d:%s:%s" % (pn, cid, target.lower()) if target else ock
                if prev_state.get(ock) or prev_state.get(ck):
                    continue
                if first_run:
                    # Baseline mode (first run): remember, never replay.
                    new_state[ck] = "seen"
                    continue
                if target and not tsession:
                    # [TO: <alias>] that no mention_map alias resolves (e.g.
                    # "Worker") — the assigned worker is decided by Project
                    # ownership of the linked Issue, never by semantic
                    # guessing or author/assignee fallback.
                    tsession = resolve_worker_session(
                        pr, proj_map, projects, sm
                    )
                if not tsession:
                    # No explicit [TO:] target (or unresolvable) — fall back
                    # to author identity: PI messages default to the linked
                    # Issue's Project owner; worker messages default to the
                    # PI session. Never silently drop a routed author's
                    # message.
                    owner = resolve_worker_session(pr, proj_map, projects, sm)
                    tsession = resolve_author_default_session(
                        author, assignee_map, sm, owner
                    )
                if not tsession:
                    # Truly unroutable (unknown author, no linked owner) —
                    # record a warning instead of disappearing it.
                    new_state[ck] = "seen"
                    wkey = (pn, author)
                    if wkey not in warned_pr_comments:
                        warned_pr_comments.add(wkey)
                        why = ("[TO: %s] unresolvable" % target
                               if target else "no [TO:] directive")
                        output["warnings"].append(
                            "%s PR #%d comment by @%s unroutable "
                            "(%s, no linked owner, author unmapped)"
                            % (prefix, pn, author, why)
                        )
                    continue
                url = "https://github.com/%s/pull/%d" % (repo, pn)
                if target:
                    msg = format_goal("[TO: %s] from @%s on PR #%d — %s" % (target, author, pn, pr["title"]), url)
                else:
                    msg = format_notice("Comment by @%s on PR #%d — %s" % (author, pn, pr["title"]), url)
                queue_goal(output, tsession, msg)
                new_state[ck] = "forwarded"
                output["actions"].append({"node": ck, "state": "forwarded", "session": tsession, "reason": "to_directive_pr",
                                          "target": target, "sent": msg[:80], "result": "queued"})
    except Exception as e:
        output["warnings"].append("%s PR comment scan: %s" % (prefix, str(e)[:80]))

    # ── 4. Milestone monitoring ──
    try:
        mr = subprocess.run(["gh", "issue", "list", "--repo", repo, "--state", "open",
                             "--json", "number,title,milestone,projectItems",
                             "--jq", ".[] | {n: .number, title: .title[0:50], ms: (.milestone.title // null), ms_due: (.milestone.dueOn // null), status: ([.projectItems[] | .status.name] | first // \"Inbox\"), nproj: ([.projectItems[] | .title] | length)}"],
                            capture_output=True, text=True, timeout=15)
        if mr.returncode == 0:
            # Coverage check: every open Issue must have a Project and a Milestone.
            # Missing either means PI has not finished routing — flag it.
            uncovered = []
            milestones = {}
            now = time.time()
            for line in mr.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                n = row.get("n")
                has_ms = bool(row.get("ms"))
                has_proj = (row.get("nproj") or 0) > 0
                if n is not None and (not has_ms or not has_proj):
                    missing = []
                    if not has_proj:
                        missing.append("Project")
                    if not has_ms:
                        missing.append("Milestone")
                    uncovered.append("#%d (missing %s)" % (n, " + ".join(missing)))
                ms = row.get("ms", "")
                if not ms:
                    continue
                if ms not in milestones:
                    milestones[ms] = {"total": 0, "done": 0, "open": 0, "due": row.get("ms_due", ""), "issues": []}
                milestones[ms]["total"] += 1
                status = row.get("status", "Inbox")
                if status in ("Done", "Cancelled"):
                    milestones[ms]["done"] += 1
                else:
                    milestones[ms]["open"] += 1
                    milestones[ms]["issues"].append("#%d [%s]" % (row["n"], status))

            for ms_name, ms_data in milestones.items():
                mk = "milestone:%s" % ms_name
                prev = prev_state.get(mk, {})
                if not isinstance(prev, dict):
                    prev = {}
                progress = "%d/%d" % (ms_data["done"], ms_data["total"])
                overdue = ""
                if ms_data.get("due"):
                    due_ts = datetime.fromisoformat(ms_data["due"].replace("Z", "+00:00")).timestamp()
                    days_left = (due_ts - now) / 86400
                    if days_left < 0:
                        overdue = "OVERDUE by %d days" % abs(int(days_left))
                    elif days_left < 7:
                        overdue = "%d days left" % int(days_left)

                if prev.get("progress") != progress or (overdue and prev.get("overdue") != overdue):
                    if coordinator_session:
                        msg = format_notice("Milestone %s — %s%s" % (ms_name, progress, " (%s)" % overdue if overdue else ""),
                                          "https://github.com/%s/milestones" % repo)
                        queue_goal(output, coordinator_session, msg)
                        output["actions"].append({"node": mk, "state": progress, "session": coordinator_session,
                                                  "reason": "milestone_update", "sent": msg[:80], "result": "queued"})
                new_state[mk] = {"progress": progress, "overdue": overdue}
            if uncovered:
                output["warnings"].append(
                    "%s un-routed issues (missing Project and/or Milestone): %s" % (
                        prefix, ", ".join(uncovered[:8])))
                if len(uncovered) > 8:
                    output["warnings"].append(
                        "%s ... and %d more un-routed issues" % (prefix, len(uncovered) - 8))
    except Exception as e:
        output["warnings"].append("%s milestone scan: %s" % (prefix, str(e)[:80]))

    delivery_ok = flush_goals(output, dry_run=args.dry_run, baseline=first_run)
    if first_run:
        output["warnings"].append(
            "%s first run: baseline recorded, historical events not replayed" % prefix)
    if not args.dry_run:
        if delivery_ok:
            save_state(state_file, new_state)
        else:
            output["warnings"].append(
                "%s delivery failed; prior state retained for retry" % prefix)
            save_state(state_file, prev_state)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
