---
name: dispatcher-framework
category: devops
description: "Generic GitHub Project-driven dispatcher. YAML config + single engine manages any repo's issue graph routing, Project Status notifications, PR monitoring, and milestone tracking."
---

# Dispatcher Framework

## Architecture

```
~/.hermes/config/dispatcher.yaml          ← repo config (projects, owner roles, sessions)
~/.hermes/scripts/dispatcher.py --repo X  ← generic engine (no code changes per repo)
~/.hermes/scripts/<name>-dispatcher.sh    ← thin wrapper (exec dispatcher.py --repo <name>)
```

One engine shared across all repos. Config defines per-repo: projects, owner roles, session names, assignee/mention maps.

## Owner Resolution

Owner is **Issue → Project → Owner Role**:

```yaml
projects:
  - number: 1
    node: "PVT_xxxxx"
    name: "Data & Market State"
    owner: data          # references session_map["data"]
```

Session map converts role to actual Hermes session:
```yaml
session_map:
  pi: paired-trading-PI
  engineer: paired-trading-Engineer
  strategist: paired-trading-Strategy
  data: paired-trading-Data
```

## Dispatcher Capabilities

### 1. Project Status Changes (Issues + PRs)
Detects issue/PR status changes on board → notifies owner session.
- Issues: Ready → owner, Blocked → PR author or warn, Review → PR author or PI, Done → owner
- PRs: **Review** → PI (only status that matters for PRs)

### 2. Issue Graph Routing
Queries `blockedBy/blocking/parent/subIssues`. When X status changes:
- `blocking` (X blocks Y) → notify Y's owner
- `blocked_by` (Z blocks X) → notify Z's owner
- `parent` → notify parent's owner
- `sub_issues` → notify sub-issue's owner

### 3. PR Notifications
Track open PRs for new, review state changes, and merges:
- **New PR** → PI
- **Review state change** (APPROVED/CHANGES_REQUESTED) → PR author
- **PR merged** → author receipt (also scans recent merged PRs for catch-up)
- **PR board status → Review** → PI (needs review)

### 4. [TO: ...] Comment Forwarding
Scans all issue/PR comments for `[TO: Role]` → forwards to mapped session.
Each comment wrapped in try/except — one bad comment doesn't block others.
State saved incrementally after each issue to survive cron interruption.

### 5. Milestone Monitoring
Reports progress + deadline warnings to PI on change.
Handles line-delimited JSON (parse line-by-line, not with single json.loads).

### 6. Stale Card Detection
Warnings only — auto-Blocked on 1h inactivity, no session notification.

## Setup New Repo

1. **Add config** to `dispatcher.yaml` (see `templates/dispatcher-config.yaml`)
2. **Create wrapper**: `~/.hermes/scripts/<name>-dispatcher.sh`
3. **Register cron**: `cronjob action=create name=<name>-dispatcher schedule=every 1m script=<name>-dispatcher.sh no_agent=True workdir=/home/drwho1985`
4. **Ensure sessions exist** for each role

## GitHub Identity Separation

**PI uses hh1985, workers use everything-bot-engineer (bot).**

- **PI session**: wrapper injects `GH_TOKEN` + git env vars (see `scripts/gh-identity-pi.sh`)
  ```
  agent-deck session set <repo>-PI wrapper "bash ~/.hermes/scripts/gh-identity-pi.sh {command}"
  ```
- **Worker sessions**: no override → global gh config (bot)
- **Git identity**: main repo → hh1985; worktrees → bot
  ```bash
  git config extensions.worktreeConfig true
  git config --worktree user.name "hh1985"
  git -C .worktrees/develop config --worktree user.name "everything-bot-engineer"
  ```

## Pitfalls

- **Authoritative reference:** See `github-event-dispatcher` skill for full routing rules, all 12 implementation patterns (including state dedup, GraphQL pitfalls, `/goal` delivery, PR Draft→Ready, PR Project Status rules, merged-PR catch-up, and GitHub identity separation), config format, and 4500+ words of troubleshooting guidance.
- PRs can't be fetched with `gh issue view` — use `gh pr view` instead
- `gh issue list --jq` returns line-delimited JSON → parse line-by-line, not single `json.loads()`
- Session restart loses ephemeral env vars — use wrapper pattern for persistence
- State file cleared between script versions → catch-up via merged PR scan + incremental save
- Per-worktree git config requires `extensions.worktreeConfig` enabled first
- First-run with empty state is slow (forwards all accumulated [TO:...] comments) — subsequent runs are fast

## Reference Files

- `references/paired-trading-config.md` — full working config example
- `templates/dispatcher-config.yaml` — blank skeleton for new repos
- `scripts/gh-identity-pi.sh` — PI identity wrapper script (chmod +x, then set as session wrapper)
