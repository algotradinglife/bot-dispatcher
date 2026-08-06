# Agent Governance — bot-dispatcher

## Repository purpose

This repository contains a generic, configuration-driven dispatcher that
routes GitHub Issue Graph, Project Status, pull-request, comment, and milestone
events to agent-deck sessions. The engine performs one polling tick per process;
the scheduler owns cadence and `no_agent` execution.

## Roles

- **PI** owns routing policy, business and research decisions, final domain
  acceptance, and deployment authorization.
- **PM**, when configured through `workflow_role`, owns operational
  coordination: lifecycle tracking, dependency follow-up, review scheduling,
  merge/closure follow-up, and dispatcher exception handling. PM escalates
  business decisions to PI and does not decide them independently.
- **Executors** may implement changes through Issues and PRs but must not
  silently redefine routing policy or deploy a new runtime.
- The dispatcher is execution-only. It reports and routes configured state; it
  does not invent dependency or lifecycle relationships.

## Control plane

- Native GitHub Issue Graph fields (`blockedBy`, `blocking`, `parent`,
  `subIssues`, and `issueType`) are authoritative for dependencies and
  decomposition.
- GitHub Project membership determines functional ownership.
- The local runtime YAML determines how Project owners and explicit mentions map
  to agent-deck sessions.
- If required GitHub state cannot be read, lifecycle routing fails closed.

## Configuration policy

- `dispatcher.example.yaml` is the only tracked configuration and must contain
  placeholders only.
- Live `dispatcher.yaml`, tokens, personal identities, session inventories,
  machine paths, and state files must never be committed.
- Repository-specific wrapper scripts are not accepted. Schedulers invoke the
  generic CLI with a repository key.

## Change workflow

1. Open an Issue describing the behavior or routing change.
2. Implement on a branch and add tests for reusable behavior.
3. Open a draft PR referencing the Issue.
4. PI reviews and merges.
5. PI separately authorizes deployment of the merged version.

## Ground rules

- Preserve one digest per session per tick without dropping queued events.
- Preserve at-least-once delivery: failed delivery keeps the previous state for
  retry.
- Dry-run must not send messages or write state.
- Do not maintain copied runtime code. Schedulers point to a reviewed checkout.
- Do not start a reasoning agent from cron; the job is a `no_agent` observer and
  notification transport only.
- Keep the deployed runtime unchanged unless deployment is explicitly
  authorized.

## Engineering principles (owner-mandated)

1. **No backward-compat tax.** Delete obsolete code outright. No compat layers,
   no migrations, no fallbacks for old behavior — if it's outdated, remove it.
2. **Simplest implementation that satisfies the current need.** No speculative
   abstraction, no gratuitous config layers.
3. **Layered system — build thin first.** Get a minimal end-to-end version
   running, then add on top. Never rip out working code for unimplemented
   complexity.
4. **Modular components, separation of concerns.** Keep pieces decoupled and
   single-purpose.
5. **Prefer mature, maintained libraries.** No reinventing the wheel without a
   clear reason.
6. **Check existing dependencies first.** Before adding a package or writing
   your own, see what the project already has and what it can do. Don't assume
   the library is missing.
7. **Architecture decisions are made long-term.** No "ship it now, fix later"
   stopgaps — an accepted design is expected to persist.
8. **Learn from mature products.** See how established products solve the same
   problem and use proven patterns; don't invent from scratch.
