#!/usr/bin/env python3
"""
Generic Research OS Dispatcher — no_agent, read-only.
Reads repo-local dispatcher.yaml unless BOT_DISPATCHER_CONFIG is set.
Usage: dispatcher.py --repo <repo_name>

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

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("BOT_DISPATCHER_CONFIG", REPO_ROOT / "dispatcher.yaml"))
STATE_DIR = Path(os.environ.get(
    "BOT_DISPATCHER_STATE_DIR",
    Path.home() / ".local" / "state" / "bot-dispatcher",
))
GRAPH_PAGE_SIZE = 100


class ControlPlaneUnavailable(RuntimeError):
    """Raised when GitHub cannot provide authoritative routing state."""



def load_config(repo_name):
    if not CONFIG_FILE.exists():
        print("ERROR: config not found at %s" % CONFIG_FILE, file=sys.stderr)
        sys.exit(1)
    raw = yaml.safe_load(CONFIG_FILE.read_text())
    repos = raw.get("repos", {})
    cfg = repos.get(repo_name)
    if not cfg:
        print("ERROR: repo '%s' not in config. Available: %s" % (
            repo_name, ", ".join(repos.keys())), file=sys.stderr)
        sys.exit(1)
    return cfg


def build_session_map(cfg):
    sm = cfg.get("session_map", {})
    return sm, {v: k for k, v in sm.items()}


def resolve_owner(proj_num, projects, sm):
    for p in projects:
        if p["number"] == proj_num:
            role = p.get("owner")
            return sm.get(role) if role else None
    return None


def resolve_assignee_session(assignee_login, assignee_map, sm):
    role = assignee_map.get(assignee_login)
    return sm.get(role) if role else None


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
        m = re.match(r'(?:@\S+\s+)?\[TO:\s*(\w+)\]', s, re.IGNORECASE)
        if m:
            t = m.group(1)
            return t, resolve_target_to_session(t, mention_map)
        break
    return None, None


def format_goal(title, body, url):
    return "%s\n\n%s\n\n---\n%s" % (title, body, url)


def queue_goal(output, session, message):
    """Retain every event; one session digest is emitted per tick."""
    if not session:
        return
    output.setdefault("_pending", {}).setdefault(session, []).append(message)


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


def gql_query(query):
    r = subprocess.run(["gh", "api", "graphql", "-f", "query=%s" % query],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise ControlPlaneUnavailable("GitHub GraphQL failed: %s" % r.stderr.strip()[:160])
    try:
        payload = json.loads(r.stdout)
    except Exception as exc:
        raise ControlPlaneUnavailable("GitHub GraphQL returned invalid JSON") from exc
    if payload.get("errors"):
        raise ControlPlaneUnavailable("GitHub GraphQL errors: %s" % str(payload["errors"])[:160])
    return payload


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


def notify_graph_stakeholders(repo, issue_num, title, cur_s, url, proj_map, projects, sm, output, prefix):
    graph = get_issue_graph(repo, issue_num)
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
                          "Status change of #%d (%s) to **%s** affects this issue (%s).\n\nPlease review accordingly."
                          % (issue_num, title, cur_s, rel_str), other_url)
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
                    title="PR #%d needs changes — Issue #%d" % (pr["number"], issue_num),
                    body="PR #%d (%s) has CHANGES_REQUESTED by PI. Please address the findings."
                         % (pr["number"], pr["title"]),
                    url=url,
                )
                return session, message, pr["number"]
    return None


def flush_goals(output):
    """Send one digest per session while retaining every queued event."""
    pending = output.pop("_pending", {})
    all_success = True
    for session, messages in pending.items():
        if isinstance(messages, str):
            messages = [messages]
        digest = "\n\n==============================\n\n".join(messages)
        result = subprocess.run(
            ["agent-deck", "session", "send", session, "--no-wait", digest],
            capture_output=True, text=True, timeout=30,
        )
        success = result.returncode == 0
        all_success = all_success and success
        outcome = "ok" if success else "FAILED: %s" % result.stderr.strip()[:160]
        output["actions"].append({
            "node": "goal:%s" % session,
            "state": "sent" if success else "pending_retry",
            "session": session,
            "reason": "goal_digest",
            "event_count": len(messages),
            "result": outcome,
        })
    return all_success


def main():
    parser = argparse.ArgumentParser(description="Generic repo dispatcher")
    parser.add_argument("--repo", required=True, help="Repo name from dispatcher.yaml")
    args = parser.parse_args()

    cfg = load_config(args.repo)
    repo = cfg["repo"]
    org = cfg.get("org", "")
    projects = cfg.get("projects", [])
    sm, rev_sm = build_session_map(cfg)
    assignee_map = cfg.get("assignee_map", {})
    mention_map = build_mention_map(cfg, sm)

    state_file = STATE_DIR / ("dispatcher_%s_state.json" % args.repo)
    prefix = "[%s]" % args.repo

    output = {"ts": time.time(), "actions": [], "warnings": [], "_pending": {}}
    prev_state = load_state(state_file)
    new_state = dict(prev_state)

    # ── 1. Project Status changes ──
    try:
        proj_map, items = build_issue_proj_map(cfg)
        for item in items:
            issue_num = item["number"]
            pn = item["project_num"]
            sk = "bjproject:%d:%d:status" % (pn, issue_num)
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
                    session = sm.get("pi")
                    if session:
                        msg = format_goal("PR #%d is in REVIEW — %s" % (issue_num, title),
                                          "Project: %s\nPlease review and merge." % item.get("project_name", str(pn)), url)
                        reason = "pr_review_ready"
                        queue_goal(output, session, msg)
                        output["actions"].append({"node": sk, "state": cur_s, "session": session, "reason": reason,
                                                  "prev_status": prev_s, "sent": msg[:80], "result": "queued"})
                new_state[sk] = cur_s
                continue

            if cur_s == "Ready":
                session = owner
                if session:
                    pname = item.get("project_name", str(pn))
                    msg = format_goal("Issue #%d is READY — %s" % (issue_num, title),
                                      "Project: %s" % pname, url)
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
                    if r.returncode == 0:
                        linked_pr = None
                        for pr in json.loads(r.stdout):
                            if re.search(r"(?i)(?:close[sd]?|fix(?:es)?|resolve[sd]?)\s*#%d\b" % issue_num, pr.get("body", "")):
                                linked_pr = pr
                                break
                        if linked_pr:
                            pu = "https://github.com/%s/pull/%d" % (repo, linked_pr["number"])
                            if linked_pr.get("reviewDecision") == "CHANGES_REQUESTED":
                                s = resolve_pr_session(linked_pr, proj_map, projects, sm, assignee_map)
                                if s:
                                    session = s
                                    msg = format_goal("PR #%d needs changes — Issue #%d in Review" % (linked_pr["number"], issue_num),
                                                      "PR #%d (%s) has CHANGES_REQUESTED. Please address findings." % (linked_pr["number"], linked_pr["title"]), pu)
                                    reason = "review_pr_changes"
                            else:
                                session = sm.get("pi")
                                msg = format_goal("Issue #%d in Review — PR #%d awaiting PI" % (issue_num, linked_pr["number"]),
                                                  "PR #%d (%s) by @%s is ready.\nReview: %s" % (linked_pr["number"], linked_pr["title"], linked_pr["author"]["login"], linked_pr.get("reviewDecision", "none")), pu)
                                reason = "review_pr_ready"
                except Exception:
                    pass

                if not session:
                    output["warnings"].append("%s Issue #%d in REVIEW — no linked PR" % (prefix, issue_num))
                    new_state[sk] = cur_s
                    continue

            elif cur_s == "Done":
                session = owner
                if session:
                    msg = format_goal("Issue #%d is DONE — %s" % (issue_num, title),
                                      "Your issue has been completed.", url)
                    reason = "issue_done"

            if session and msg:
                queue_goal(output, session, msg)
                output["actions"].append({"node": sk, "state": cur_s, "session": session, "reason": reason,
                                          "prev_status": prev_s, "sent": msg[:80], "result": "queued"})

            # Notify graph stakeholders
            notify_graph_stakeholders(repo, issue_num, title, cur_s, url, proj_map, projects, sm, output, prefix)

            new_state[sk] = cur_s

    except Exception as e:
        output["_pending"] = {}
        new_state = dict(prev_state)
        output["warnings"].append("%s control plane unavailable: %s" % (prefix, str(e)[:160]))

    # ── 2. Issue comments [TO: ...] fallback ──
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
                            if not tsession:
                                new_state[ck] = "seen"
                                continue
                            bp = body.strip()[:200]
                            if len(body) > 200:
                                bp += "…"
                            url = "https://github.com/%s/issues/%d" % (repo, num)
                            msg = format_goal("[TO: %s] from @%s on Issue #%d — %s" % (target, author, num, title), bp, url)
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

                # 0. Draft->Ready transition -> PI
                dk = "prdraft:%d" % pn
                was_draft = prev_state.get(dk)
                if was_draft is True and not is_draft:
                    msg = format_goal("PR #%d is now READY FOR REVIEW — %s" % (pn, pr["title"]),
                                      "Author: @%s\nThe PR was moved from Draft to Ready.\nBranch: %s" % (author, pr.get("headRefName", "")), pu)
                    pi_session = sm.get("pi")
                    queue_goal(output, pi_session, msg)
                    output["actions"].append({"node": dk, "state": "ready", "session": sm.get("pi"),
                                              "reason": "pr_draft_ready", "sent": msg[:80], "result": "queued"})
                new_state[dk] = is_draft

                # 3a. New PR -> PI
                if prev_state.get(pk) != "open":
                    msg = format_goal("New PR #%d: %s by @%s" % (pn, pr["title"], author),
                                      "Status: %s\nBranch: %s" % (pr.get("mergeStateStatus", "unknown"), pr["headRefName"]), pu)
                    pi_session = sm.get("pi")
                    queue_goal(output, pi_session, msg)
                    output["actions"].append({"node": pk, "state": "open", "session": sm.get("pi"), "reason": "new_pr",
                                              "sent": msg[:80], "result": "queued"})
                    new_state[pk] = "open"

                # 3b. Review state changes
                try:
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
                                    b = "Your PR has passed review. Proceed."
                                elif decision == "CHANGES_REQUESTED":
                                    reason = "pr_changes_requested"
                                    t = "PR #%d has **CHANGES REQUESTED** — %s" % (pn, pr["title"])
                                    b = "PI has requested changes. Please address findings."
                                else:
                                    reason = "pr_review_changed"
                                    t = "PR #%d review updated — %s" % (pn, pr["title"])
                                    b = "Review state: %s" % decision
                                msg = format_goal(t, b, pu)
                                queue_goal(output, asession, msg)
                                output["actions"].append({"node": rk, "state": decision, "session": asession, "reason": reason,
                                                          "sent": msg[:80], "result": "queued"})

                            elif author != "hh1985":
                                msg = format_goal("PR #%d review: %s — unclear who should act" % (pn, decision),
                                                  "PR by @%s has review '%s' but @%s is not mapped." % (author, decision, author), pu)
                                pi_session = sm.get("pi")
                                queue_goal(output, pi_session, msg)
                                output["actions"].append({"node": rk, "state": decision, "session": sm.get("pi"),
                                                          "reason": "pr_unclear_owner", "sent": msg[:80], "result": "queued"})

                        if decision:
                            new_state[rk] = decision
                except Exception:
                    pass

            # 3c. Track closed/merged + recent merges
            open_keys = {"pr:%d" % p["number"] for p in prs}
            merged_keys = {k for k, v in prev_state.items()
                           if k.startswith("pr:") and not k.startswith("prreview:")
                           and v in ("merged", "closed")}

            try:
                mr = subprocess.run(["gh", "pr", "list", "--repo", repo, "--state", "merged",
                                     "--json", "number,title,mergedAt,author,mergedBy",
                                     "--jq", ".[:10]"],
                                    capture_output=True, text=True, timeout=10)
                if mr.returncode == 0:
                    for mp in json.loads(mr.stdout):
                        pn = mp["number"]
                        mk = "pr:%d" % pn
                        if mk not in merged_keys and prev_state.get(mk) != "merged":
                            new_state[mk] = "merged"
                            author = mp.get("author", {}).get("login", "unknown")
                            asession = resolve_assignee_session(author, assignee_map, sm)
                            pu = "https://github.com/%s/pull/%d" % (repo, pn)
                            if asession:
                                msg = format_goal("PR #%d has been **MERGED**! — %s" % (pn, mp.get("title", "")),
                                                  "Your PR was merged by @%s.\nMerged at: %s" % (mp.get("mergedBy", {}).get("login", "?"), mp.get("mergedAt", "unknown")), pu)
                                queue_goal(output, asession, msg)
                                output["actions"].append({"node": mk, "state": "merged", "session": asession,
                                                          "reason": "pr_merged_recent", "sent": msg[:80], "result": "queued"})
                            else:
                                msg = format_goal("PR #%d was merged — unclear who to notify" % pn,
                                                  "PR by @%s was merged but @%s is not mapped." % (author, author), pu)
                                pi_session = sm.get("pi")
                                queue_goal(output, pi_session, msg)
                                output["actions"].append({"node": mk, "state": "merged", "session": sm.get("pi"),
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
                                             "--json", "state,mergedAt,author,title",
                                             "--jq", "{state, mergedAt, author: .author.login, title}"],
                                            capture_output=True, text=True, timeout=10)
                        if cl.returncode == 0:
                            info = json.loads(cl.stdout)
                            if info.get("state") == "MERGED":
                                new_state[key] = "merged"
                                author = info.get("author", "")
                                if author != "hh1985":
                                    asession = resolve_assignee_session(author, assignee_map, sm)
                                    if asession and prev_state.get(key) != "merged":
                                        pu = "https://github.com/%s/pull/%d" % (repo, pn)
                                        msg = format_goal("PR #%d has been **MERGED**! — %s" % (pn, info.get("title", "")),
                                                          "Your PR was merged by PI.\nMerged at: %s" % info.get("mergedAt", "unknown"), pu)
                                        queue_goal(output, asession, msg)
                                        output["actions"].append({"node": key, "state": "merged", "session": asession,
                                                                  "reason": "pr_merged", "sent": msg[:80], "result": "queued"})
                                    elif not asession:
                                        pu = "https://github.com/%s/pull/%d" % (repo, pn)
                                        msg = format_goal("PR #%d was merged — unclear who to notify" % pn,
                                                          "PR by @%s was merged but @%s is not mapped." % (author, author), pu)
                                        pi_session = sm.get("pi")
                                        queue_goal(output, pi_session, msg)
                                        output["actions"].append({"node": key, "state": "merged", "session": sm.get("pi"),
                                                                  "reason": "pr_merged_unmapped", "sent": msg[:80], "result": "queued"})
                    except Exception:
                        pass
    except Exception as e:
        output["warnings"].append("%s PR scan: %s" % (prefix, str(e)[:80]))

    # ── 3d. PR comments [TO: ...] fallback ──
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
                if not tsession:
                    new_state[ck] = "seen"
                    continue
                bp = body.strip()[:200]
                if len(body) > 200:
                    bp += "…"
                url = "https://github.com/%s/pull/%d" % (repo, pn)
                msg = format_goal("[TO: %s] from @%s on PR #%d — %s" % (target, author, pn, pr["title"]), bp, url)
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
                             "--jq", ".[] | select(.milestone != null) | {n: .number, title: .title[0:50], ms: .milestone.title, ms_due: .milestone.dueOn, status: [.projectItems[] | .status.name][0]}"],
                            capture_output=True, text=True, timeout=15)
        if mr.returncode == 0:
            milestones = {}
            now = time.time()
            for line in mr.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
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
                    pi_session = sm.get("pi")
                    if pi_session:
                        body = "Progress: %s/%s complete\n" % (ms_data["done"], ms_data["total"])
                        if overdue:
                            body += "Deadline: %s\n" % overdue
                        if ms_data["issues"]:
                            body += "Open: " + ", ".join(ms_data["issues"][:5])
                            if len(ms_data["issues"]) > 5:
                                body += " … (+%d more)" % (len(ms_data["issues"]) - 5)
                        msg = format_goal("Milestone %s — %s" % (ms_name, progress), body, "https://github.com/%s/milestones" % repo)
                        queue_goal(output, pi_session, msg)
                        output["actions"].append({"node": mk, "state": progress, "session": pi_session,
                                                  "reason": "milestone_update", "sent": msg[:80], "result": "queued"})
                new_state[mk] = {"progress": progress, "overdue": overdue}
    except Exception as e:
        output["warnings"].append("%s milestone scan: %s" % (prefix, str(e)[:80]))

    delivery_ok = flush_goals(output)
    if delivery_ok:
        save_state(state_file, new_state)
    else:
        output["warnings"].append("%s delivery failed; prior state retained for retry" % prefix)
        save_state(state_file, prev_state)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
