# Bot Dispatcher

No-agent GitHub notification relay for PI-managed Codex sessions.

## Boundary

The dispatcher observes GitHub and delivers notifications. It does not decide
roadmap, mutate Issue Graph or Project Status, merge PRs, close Issues, or
authorize deployment. PI owns those decisions.

## Architecture

- `dispatcher.py` — generic engine; `--repo <name>` selects configuration
- `dispatcher.yaml` — Project-to-owner and owner-to-session mapping
- `bj-dispatcher.sh` / `pt-dispatcher.sh` — repo-local cron entry points
- `~/.local/state/bot-dispatcher/` — delivery/dedup state by default

## Signals

| Signal | Destination |
|---|---|
| Issue becomes Ready | Project owner session |
| Issue or PR enters Review | PI or linked Issue owner, depending on review state |
| Issue Graph stakeholder changes | Related Issue owners |
| `[TO: Role]` comment | Mapped role session |
| New PR / Draft→Ready | PI |
| PR review change / merge | Linked Issue owner or author fallback |
| Milestone risk | PI |

Multiple events for one session are combined into one digest per tick. Delivery
state is committed only after all digests are accepted; a failed tick retains
the prior state so events are retried.

## Run

```bash
python3 -m pip install -r requirements.txt
./bj-dispatcher.sh
./pt-dispatcher.sh
```

Configuration defaults to the repository's `dispatcher.yaml`. Override with
`BOT_DISPATCHER_CONFIG`. State defaults to
`~/.local/state/bot-dispatcher`; override with
`BOT_DISPATCHER_STATE_DIR`.

## Tests

```bash
python3 -m pip install -r requirements-dev.txt
pytest -q
```

## Governance

GitHub Issue Graph is the dependency source of truth. Project membership
determines functional ownership. When Graph or Project reads fail, lifecycle
routing fails closed. See `AGENTS.md`.
