# Bot Dispatcher

Bot Dispatcher is a small, configuration-driven polling service for teams that
coordinate work through GitHub Issues, Project V2 boards, pull requests, and
agent-deck sessions. One invocation performs one scan; a scheduler can run it as
a `no_agent` job every minute.

The tracked tree contains no live repository IDs, session names, GitHub
accounts, tokens, or machine-specific paths. Runtime configuration stays
outside Git.

## One-shot deployment (`deploy.py`)

三层管理体系一键部署：给定项目参数，自动初始化 GitHub 控制面 + kanban 执行面
+ 治理模板资产（含闭环治理约束：一 issue 一 worker、owner 不可变更、
EV 流程、路线图维护）。

```bash
python3 deploy.py --repo owner/name --key project-key --board kanban-board \
    --out /path/to/deploy-dir [--project-title "Title"] [--dry-run]
```

生成资产：
- `.github/workflows/pr-status-sync.yaml` — PR 生命周期 → Project 状态（参数化 ID）
- `dispatcher.yaml` — session_map（researcher/engineer/auditor + pm/pi）
- `<key>_tick.sh` — no_agent 观察 cron（dispatcher + sync_job --archive）
- `AGENTS.md` / `ROADMAP.md` / `README.md` — 治理模板（templates/）

`--dry-run` 纯预览无副作用。真实部署后仍需手动：
`gh secret set PROJECT_SYNC_TOKEN`、`hermes kanban boards create`、注册 cron。

治理约束详见 `templates/AGENTS.md`：闭环 PI→worker→EV→PI review+merge→
roadmap update；一 issue 一 worker；owner 与 Project 归属严格绑定不可变更；
跨 worker 协作拆 issue；改变=重开 issue 落对应 Project。

## Boundary

The dispatcher observes GitHub and delivers notifications. It does not decide
roadmap, mutate Issue Graph or Project Status, merge PRs, close Issues, or
authorize deployment. PI owns business decisions and final domain acceptance;
an optional dedicated PM can own workflow coordination.

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

After successful `Ready` delivery, the configured workflow coordinator or
other authorized control-plane operator advances the Issue to `In Progress`.
The dispatcher never performs that status mutation itself.

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

### Dedicated PM mode

Set `workflow_role` to a role in `session_map` to separate operational
coordination from PI decisions:

```yaml
session_map:
  pi: team-PI
  pm: team-PM
  strategist: team-Strategy
workflow_role: pm
mention_map:
  PI: pi
  PM: pm
```

With this mode enabled, the PM receives new-PR and Draft-to-Ready events,
Review coordination, unresolved ownership, merged/unmapped receipts, Milestone
updates, and copies of Issue lifecycle changes. Project owners still receive
their own execution events, including the initial `Ready` goal. Explicit
`[TO: PI]` directives continue to reach PI directly. If `workflow_role` is
omitted, operational events continue to use `pi` for backward compatibility.

## Development

```bash
python -m pip install -r requirements-dev.txt
pytest -q
python -m py_compile dispatcher.py
```

CI runs the pytest suite on every PR and push to `main`. See
[operations](docs/OPERATIONS.md) for deployment and failure handling, and
[dispatcher framework](docs/SKILL.md) for routing semantics.
