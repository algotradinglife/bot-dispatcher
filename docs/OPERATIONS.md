# Operations Guide

## Deployment boundary

Merging code and deploying a polling job are separate decisions. A deployment
must identify the exact commit, config path, state directory, repository key,
GitHub identity, and agent-deck sessions. Do not copy a developer's local config
into the repository.

## Installation

1. Check out an approved commit on the runtime host.
2. Create a virtual environment and install `requirements.txt`.
3. Copy `dispatcher.example.yaml` to a host-owned config path and replace every
   placeholder.
4. Run `--validate-config`.
5. Confirm `gh auth status` and that every configured agent-deck session exists.
6. Run `--dry-run` and inspect the JSON output.
7. Register one `no_agent` job per repository key at a one-minute interval.

Example command:

```bash
python3 /opt/bot-dispatcher/dispatcher.py \
  --config /etc/bot-dispatcher/dispatcher.yaml \
  --state-dir /var/lib/bot-dispatcher \
  --repo sample-research
```

The scheduler should capture stdout and stderr. A successful tick writes one
JSON object to stdout with `actions` and `warnings` arrays.

## Identity and secrets

Provide GitHub authentication through the scheduler's secret mechanism or the
host's authenticated GitHub CLI. Do not store tokens or identity wrapper scripts
in this repository. Configure Git commit identity separately from runtime API
authentication.

The GitHub account needs read access to Issues, pull requests, Project V2, and
Issue Graph data.

## State

Each repository key receives an independent state file:

```text
dispatcher_<repo-key>_state.json
```

Back up the state directory before moving hosts. Starting with empty state is
safe but can surface historical events as new. Use `--dry-run` before the first
stateful tick.

## Monitoring

Alert on:

- a non-zero process exit;
- malformed JSON output;
- repeated non-empty `warnings`;
- `FAILED:` delivery results;
- a missing tick for more than two schedule intervals.

The dispatcher does not modify native Project Status or Issue Graph
relationships.

## Ready and In Progress runbook

For a newly authorized Issue, set the Project Status to `Ready`. The next tick
should contain two related actions:

- a Project action with `reason: issue_ready` and `result: queued`;
- a session digest action with `result: ok + Enter`.

Only after the second action succeeds should the PI or authorized
control-plane operator set the Issue to `In Progress`. `In Progress` is an
execution record, not an initial-dispatch trigger, and the dispatcher does not
write it automatically.

If an Issue was moved directly from planning to `In Progress`, do not delete or
edit the dispatcher state file. Move the Issue to `Ready`, confirm a later tick
reports `ok + Enter`, and then move it to `In Progress`. If delivery fails, keep
the Issue in `Ready`; at-least-once state handling preserves the transition for
retry.

## Rollback

1. Disable the scheduled job.
2. Restore the previously approved code revision.
3. Keep the state file unless the rollback specifically changes its schema.
4. Run a dry run against the restored revision.
5. Re-enable the job after PI approval.

Never erase state merely to force re-delivery. Route a deliberate manual message
instead.
