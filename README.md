# Bot Dispatcher

Generic Research OS dispatcher: GitHub Issue Graph + Project Status routing, config-driven, multi-repo.

## Architecture

- `dispatcher.py` — generic engine, `--repo <name>` selects config
- `dispatcher.yaml` — per-repo config: projects, owner roles, session names
- `*-dispatcher.sh` — thin wrappers per repo (cron no_agent, every 1m)

## Capabilities

| Feature | How it works |
|---------|-------------|
| Project Status changes | Issue moves column -> notify owner session (Project -> Owner Role -> session) |
| Issue Graph routing | blockedBy/blocking/parent/subIssues. Status change of X notifies graph stakeholders' owners |
| `[TO: ...]` forwarding | Scans issue/PR comments for `[TO: Role]` -> forwards to matching session |
| PR monitoring | New PR -> PI. Draft->Ready -> PI. Review change -> author. Merged -> author receipt |
| Milestone tracking | Progress + overdue warnings to PI |
| Per-tick dedup | One message per session per tick (queue then flush) |

## Setup for a new repo

1. Add a section to `dispatcher.yaml` (projects with node IDs, owner roles, session_map)
2. Create `<name>-dispatcher.sh` wrapper: `exec .../dispatcher.py --repo <name>`
3. Register cron job: no_agent, every 1m, deliver local
4. Ensure agent-deck sessions exist (PI, Engineer, Strategy, Data per repo)

## Owner resolution

Issue -> Project membership -> Project's `owner` role -> `session_map[role]` -> session.

## Notes

- Issue Graph is the control-plane source of truth (see AGENTS.md in algotradinglife/beijing-lot)
- PI owns graph edges and routing decisions; dispatcher is execution-only
