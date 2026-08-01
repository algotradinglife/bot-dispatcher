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

- `Ready` -> Project owner.
- `Blocked` with a linked PR requesting changes -> mapped PR author.
- `Review` -> PI or mapped PR author, depending on the PR review state.
- `Done` -> Project owner.
- Related Graph nodes -> their Project owners.

The dispatcher does not write Project Status or Graph relationships.

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
