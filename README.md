# Bot Dispatcher

Bot Dispatcher is a small, configuration-driven polling service for teams that
coordinate work through GitHub Issues, Project V2 boards, pull requests, and
agent-deck sessions. One invocation performs one scan; a scheduler can run it as
a `no_agent` job every minute.

The tracked tree contains no live repository IDs, session names, GitHub
accounts, tokens, or machine-specific paths. Runtime configuration stays
outside Git.

## Boundary

The dispatcher observes GitHub and delivers notifications. It does not decide
roadmap, mutate Issue Graph or Project Status, merge PRs, close Issues, or
authorize deployment. PI owns those decisions.

## What it routes

- Project Status changes to the role that owns that Project.
- Native Issue Graph relationships (`blockedBy`, `blocking`, `parent`, and
  `subIssues`) to related owners.
- `[TO: role]` directives in Issue and PR comments.
- New PRs, Draft-to-Ready transitions, review changes, and merge receipts.
- Milestone progress and deadline warnings.

All events for one session are combined into one digest per scan. State is
committed only after every digest is accepted; a failed delivery retains the
previous state so the event can be retried.

## Issue lifecycle: Ready vs In Progress

`Ready` is the first-dispatch trigger for an Issue. When an Issue changes to
`Ready`, the dispatcher sends the Project owner a message beginning with
`/goal` and submits Enter to the resolved agent-deck tmux session. A successful
tick reports the final digest delivery as `ok + Enter`.

`In Progress` means execution has already begun. It is deliberately not a
first-dispatch trigger. If an Issue is created or moved directly to
`In Progress`, the dispatcher records that status but does not send the initial
goal. The required lifecycle is:

```text
Inbox/planning -> Ready -> dispatcher: /goal + Enter -> In Progress
```

After successful `Ready` delivery, the PI or other authorized control-plane
operator advances the Issue to `In Progress`. The dispatcher never performs
that status mutation itself.

## Requirements

- Python 3.10+
- [GitHub CLI](https://cli.github.com/) authenticated for the configured repos
- `agent-deck` with the configured sessions
- PyYAML 6.x

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## Configure

Copy the sanitized example to the default local config path:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/bot-dispatcher"
cp dispatcher.example.yaml \
  "${XDG_CONFIG_HOME:-$HOME/.config}/bot-dispatcher/dispatcher.yaml"
```

Edit the copy with your GitHub repository, Project V2 node IDs, roles, and
agent-deck session names. `dispatcher.yaml` is ignored by Git.

Validate without contacting GitHub or agent-deck:

```bash
python dispatcher.py --repo sample-research --validate-config
```

For a config stored elsewhere, pass `--config` or set
`BOT_DISPATCHER_CONFIG`. State defaults to the XDG state directory under
`bot-dispatcher`; override it with `--state-dir` or
`BOT_DISPATCHER_STATE_DIR`.

## Run safely

First perform a dry run. It reads GitHub but sends no agent-deck messages and
writes no state:

```bash
python dispatcher.py --repo sample-research --dry-run
```

Then run one real polling tick:

```bash
python dispatcher.py --repo sample-research
```

## One-minute no-agent schedule

Point the scheduler directly at the generic entrypoint; per-repository wrapper
scripts are unnecessary:

```text
schedule: every 1 minute
mode: no_agent
command: python3 /opt/bot-dispatcher/dispatcher.py --repo sample-research
```

Use one scheduled job per repository key when multiple repositories share the
same config. Keep authentication and agent-deck session setup in the scheduler
environment rather than in this repository.

## Configuration model

Routing follows:

```text
Issue -> GitHub Project membership -> configured owner role -> session_map
```

`assignee_map` is the fallback for PRs without a linked Issue. For PRs that do
link an Issue, its Project ownership wins. `mention_map` maps the value in a
`[TO: role]` directive to a role. Every referenced role must exist in
`session_map`, and a `pi` session is required.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest -q
python -m py_compile dispatcher.py
```

CI runs the pytest suite on every PR and push to `main`. See
[operations](docs/OPERATIONS.md) for deployment and failure handling, and
[dispatcher framework](docs/SKILL.md) for routing semantics.
