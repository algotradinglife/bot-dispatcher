# Agent Governance — bot-dispatcher

## Repository purpose

This repo contains the generic **bot-dispatcher**: a config-driven daemon that
routes GitHub events (Issue Graph changes, Project Status changes, `[TO: ...]`
comments, PR events, milestone progress) to agent-deck sessions, one message
per session per tick.

## Roles

- **PI** owns: issue contracts, graph edges, routing decisions, final review,
  PR merges, and deployment decisions (which version runs in cron).
- **Executors** (Engineer/Strategy/Data sessions) may propose changes via
  Issues/PRs but must not silently redefine routing rules or deploy.
- The dispatcher itself is **execution-only** — it never decides policy.

## Control plane

- GitHub Issue Graph (`blockedBy`/`blocking`/`parent`/`subIssues`/`issueType`)
  is the source of truth for dependencies and lifecycle.
- GitHub Project membership determines functional ownership (Project → Owner Role).
- `dispatcher.yaml` is the single config; repo-specific overrides belong there.

## Change workflow

1. Open an Issue describing the change (routing rule, new repo, bug).
2. Executor implements in a branch/PR referencing the Issue.
3. PI reviews and merges (only PI merges).
4. PI authorizes deployment; sync to `~/.hermes/scripts/` and restart cron.

## Ground rules

- Never commit GitHub tokens, session configs, or `~/.hermes` internals.
- Keep `dispatcher.yaml` free of secrets; session names are not secrets.
- The local running copy and this repo must stay in sync — update both in one
  PR or note the drift explicitly in the PR body.
- When GitHub is unreadable, fail closed (report) — never fabricate state.
