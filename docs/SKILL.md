---
name: dispatcher-framework
category: devops
description: "Configure and operate a generic GitHub Project-driven dispatcher."
---

# Dispatcher Framework

Use this framework when a team wants a one-minute, no-agent polling job to route
GitHub coordination events into agent-deck sessions.

## Architecture

```text
local dispatcher.yaml
        |
        v
dispatcher.py --repo <key>  -- one polling tick
        |
        +-- GitHub CLI reads Issues, Issue Graph, Projects, PRs, comments
        +-- state file deduplicates previously observed events
        +-- agent-deck receives at most one message per session
```

The scheduler supplies the cadence. No repository-specific wrapper is needed.

## Source of truth

- Native Issue Graph fields define dependencies and decomposition.
- Project V2 membership identifies the owner role.
- `session_map` maps owner roles to delivery sessions.
- Issue bodies, comments, labels, and local state cannot override Graph or
  Project ownership.

If required GitHub control-plane data is unavailable, the scan must fail closed
and expose a warning rather than manufacture relationships.

## Routing behavior

### Project events

- `Ready` -> Project owner as the first-dispatch event. The delivered payload
  starts with `/goal`; successful delivery also submits Enter.
- `In Progress` -> no initial dispatch. This status records that execution has
  already started.
- `Blocked` with a linked PR requesting changes -> mapped PR author.
- `Review` -> PI or mapped PR author, depending on the PR review state.
- `Done` -> Project owner.
- Related Graph nodes -> their Project owners.

The dispatcher does not write Project Status or Graph relationships.

Use this lifecycle order for new work:

```text
1. PI/control-plane owner writes all native graph and Project routing fields.
2. PI/control-plane owner sets Status to Ready.
3. Dispatcher sends /goal and submits Enter to the Project owner session.
4. After the tick reports ok + Enter, the authorized owner sets In Progress.
```

Do not create work directly in `In Progress` when an initial dispatcher launch
is required. Such a transition is observed and stored, but it is not routed as
`issue_ready`.

### Pull requests

- New PR -> PI.
- Draft to Ready -> PI.
- Review decision change -> mapped author, otherwise PI.
- Merge -> mapped author receipt, otherwise PI.

### Explicit directives

The first non-empty, non-quoted line of an Issue or PR comment may contain:

```text
[TO: role]
```

Role matching is case-insensitive and supports letters, digits, underscores,
and hyphens. The role must resolve through `mention_map` and `session_map`.

## Safe onboarding

1. Copy `dispatcher.example.yaml` to a local ignored config.
2. Replace all placeholders and validate the selected repository key.
3. Ensure the required GitHub identity and agent-deck sessions exist.
4. Run `--dry-run` and review every proposed action.
5. Enable a one-minute `no_agent` job.

## Guardrails

- Never commit live config, tokens, identities, session inventories, or state.
- Preserve one delivery per session per tick.
- Do not clear state to bypass deduplication.
- Do not deploy a branch or unreviewed commit.
- Keep code publication and runtime deployment as separate approvals.
