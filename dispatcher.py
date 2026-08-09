#!/usr/bin/env python3
from __future__ import annotations
"""
Config-driven GitHub dispatcher for no-agent polling jobs.
Reads repository routing from a local YAML file.
Usage: dispatcher.py --repo <repo_key> [--config <path>]

Routes based on GitHub Issue Graph (blockedBy/blocking/parent/subIssues)
and GitHub Project Status. Sends one /goal per session per tick.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG_HOME = Path(os.environ.get(
    "XDG_CONFIG_HOME", Path.home() / ".config"
)).expanduser()
DEFAULT_STATE_HOME = Path(os.environ.get(
    "XDG_STATE_HOME", Path.home() / ".local" / "state"
)).expanduser()
DEFAULT_CONFIG_FILE = Path(os.environ.get(
    "BOT_DISPATCHER_CONFIG",
    DEFAULT_CONFIG_HOME / "bot-dispatcher" / "dispatcher.yaml",
)).expanduser()
DEFAULT_STATE_DIR = Path(os.environ.get(
    "BOT_DISPATCHER_STATE_DIR",
    DEFAULT_STATE_HOME / "bot-dispatcher",
)).expanduser()
GRAPH_PAGE_SIZE = 100


class ControlPlaneUnavailable(RuntimeError):
    """Raised when GitHub cannot provide authoritative routing state."""


def validate_repo_config(repo_name, cfg):
    if not isinstance(cfg, dict):
        raise ValueError("repo '%s' config must be a mapping" % repo_name)
    repo = cfg.get("repo")
    if not isinstance(repo, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise ValueError("repo '%s' must set repo: owner/name" % repo_name)

    session_map = cfg.get("session_map")
    if not isinstance(session_map, dict) or not session_map.get("pi"):
        raise ValueError("repo '%s' must map the pi session" % repo_name)
    if any(not isinstance(role, str) or not isinstance(session, str) or not session
           for role, session in session_map.items()):
        raise ValueError("repo '%s' session_map entries must be non-empty strings" % repo_name)

    projects = cfg.get("projects", [])
    if not isinstance(projects, list):
        raise ValueError("repo '%s' projects must be a list" % repo_name)
    project_numbers = set()
    for project in projects:
        if not isinstance(project, dict):
            raise ValueError("repo '%s' project entries must be mappings" % repo_name)
        number = project.get("number")
        node = project.get("node")
        owner = project.get("owner")
        if (not isinstance(number, int) or isinstance(number, bool)
                or number < 1 or number in project_numbers):
            raise ValueError("repo '%s' project numbers must be unique positive integers" % repo_name)
        if not isinstance(node, str) or not node:
            raise ValueError("repo '%s' project %s must set a node ID" % (repo_name, number))
        if owner not in session_map:
            raise ValueError("repo '%s' project %s owner is not in session_map" % (repo_name, number))
        project_numbers.add(number)

    for map_name in ("assignee_map",):
        role_map = cfg.get(map_name, {})
        if not isinstance(role_map, dict):
            raise ValueError("repo '%s' %s must be a mapping" % (repo_name, map_name))
        unknown_roles = sorted({role for role in role_map.values() if role not in session_map})
        if unknown_roles:
            raise ValueError("repo '%s' %s references unknown roles: %s" % (
                repo_name, map_name, ", ".join(unknown_roles)))


def load_config(repo_name, config_file=DEFAULT_CONFIG_FILE):
    config_file = Path(config_file).expanduser()
    if not config_file.exists():
        raise ValueError("config not found at %s" % config_file)
    raw = yaml.safe_load(config_file.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    repos = raw.get("repos", {})
    if not isinstance(repos, dict):
        raise ValueError("config 'repos' must be a mapping")
    cfg = repos.get(repo_name)
    if not cfg:
        raise ValueError("repo '%s' not in config. Available: %s" % (
            repo_name, ", ".join(sorted(repos.keys()))))
    validate_repo_config(repo_name, cfg)
    return cfg


def project_state_key(project_number, item_number, field="status"):
    return "project:%d:%d:%s" % (project_number, item_number, field)


def build_session_map(cfg):
    sm = cfg.get("session_map", {})
    return sm, {v: k for k, v in sm.items()}



def resolve_owner(proj_num, projects, sm):
    for p in projects:
        if p["number"] == proj_num:
            role = p.get("owner")
            return sm.get(role) if role else None
    return None



def format_goal(title, url):
    """One-line task reminder: action + link, no prose. Sets the session goal."""
    return "/goal %s\n%s" % (title, url)


def format_notice(title, url):
    """One-line informational notice: action + link, no goal semantics."""
    return "%s\n%s" % (title, url)


def queue_goal(output, role, message, issue_num=None):
    """Record a notification for a role (worker/auditor/user/PI).

    v0_3: dispatcher 只产生通知事件 (不投递卡片/不写 GitHub).
    实际发送由 tick 脚本 / 通知通道 (飞书/邮件短信) 完成.
    role 取值: worker / auditor / user (PI 不接收通知, 主动轮询).
    """
    if not role:
        return
    notifications = output.setdefault("notifications", [])
    # dedupe identical messages for the same role within one tick
    for n in notifications:
        if n["role"] == role and n["message"] == message:
            return
    notifications.append({"role": role, "message": message,
                          "issue": issue_num})



def load_state(state_file):
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            if not isinstance(data, dict):
                raise ValueError("顶层必须是 JSON 对象")
            return data
        except Exception as exc:
            # 损坏 → fail-closed: 直接退出要求人工处理 (codex P1-5).
            # 不静默转 baseline (会吞掉此前的历史状态变化).
            raise RuntimeError(
                "state 文件损坏 (%s): %s — 已停止, 需人工检查 %s "
                "(勿自动转 baseline)" % (state_file.name, exc, state_file))
    return None


def save_state(state_file, state):
    """原子写: tmp + rename, 防进程中断产生半文件 (损坏→重放的根因)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_name(state_file.name + ".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(state_file)


def gql_query(query, retries=4, base_delay=1.0):
    """Run a GraphQL query with retry on transient failures (rate limit, network)."""
    last_err = None
    for attempt in range(retries):
        try:
            r = subprocess.run(["gh", "api", "graphql", "-f", "query=%s" % query],
                               capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        else:
            if r.returncode != 0:
                last_err = r.stderr.strip()[:160]
            else:
                try:
                    payload = json.loads(r.stdout)
                except Exception as exc:
                    last_err = "invalid JSON: %s" % exc
                else:
                    if payload.get("errors"):
                        # Retry only on transient error kinds; fail fast otherwise
                        err_str = str(payload["errors"])
                        transient = any(k in err_str for k in (
                            "rate limit", "rate_limit", "abuse", "internal", "timeout",
                            "connection", "Network error", "ETIMEDOUT", "EOF",
                            "connection reset", "refused", "TLS", "503", "502",
                        ))
                        if not transient:
                            raise ControlPlaneUnavailable(
                                "GitHub GraphQL errors: %s" % err_str[:160])
                        last_err = err_str[:160]
                    else:
                        return payload
        if attempt < retries - 1:
            time.sleep(base_delay * (2 ** attempt))
    raise ControlPlaneUnavailable("GitHub GraphQL failed: %s" % (last_err or "unknown"))




def get_project_items(cfg):
    """Fetch all items from configured Projects with cursor pagination."""
    result = []
    for proj in cfg.get("projects", []):
        pn = proj["number"]
        pid = proj["node"]
        cursor = None
        while True:
            after = "null" if cursor is None else json.dumps(cursor)
            query = (
                '{node(id:%s){... on ProjectV2{items(first:%d,after:%s){'
                'nodes{id content{__typename ... on Issue{number title} '
                '... on PullRequest{number title}} fieldValues(first:20){nodes{'
                '__typename ... on ProjectV2ItemFieldSingleSelectValue{name '
                'field{... on ProjectV2SingleSelectField{name}}}}}} '
                'pageInfo{hasNextPage endCursor}}}}}'
            ) % (json.dumps(pid), GRAPH_PAGE_SIZE, after)
            data = gql_query(query)
            project = data.get("data", {}).get("node")
            if not isinstance(project, dict):
                raise ControlPlaneUnavailable("Project #%d unavailable" % pn)
            connection = project.get("items")
            if not isinstance(connection, dict):
                raise ControlPlaneUnavailable("Project #%d items unavailable" % pn)
            for node in connection.get("nodes", []):
                content = node.get("content")
                if not content or not content.get("number"):
                    continue
                status = "Inbox"
                for value in node.get("fieldValues", {}).get("nodes", []):
                    if not isinstance(value, dict):
                        continue
                    field = value.get("field")
                    if (value.get("__typename") == "ProjectV2ItemFieldSingleSelectValue"
                            and isinstance(field, dict) and field.get("name") == "Status"):
                        status = value.get("name", "Inbox")
                        break
                result.append({
                    "number": content["number"],
                    "status": status,
                    "project_num": pn,
                    "project_name": proj.get("name", str(pn)),
                    "_item_id": node["id"],
                    "is_pr": content.get("__typename") == "PullRequest",
                })
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
            if not cursor:
                raise ControlPlaneUnavailable("Project #%d pagination cursor missing" % pn)
    return result


def build_issue_proj_map(cfg):
    items = get_project_items(cfg)
    mm = {}
    for item in items:
        if item.get("is_pr"):
            continue
        number = item["number"]
        project_num = item["project_num"]
        if number in mm and mm[number] != project_num:
            raise ControlPlaneUnavailable(
                "Issue #%d has conflicting configured Project memberships" % number
            )
        mm[number] = project_num
    return mm, items





def flush_goals(output, dry_run=False, baseline=False, cfg=None):
    """Emit queued notifications as structured output.

    v0_3: 不投递 agent-deck / kanban. 通知事件随 JSON 输出,
    由外层 tick 脚本 / cron 投递到对应通道 (飞书主 / 邮件短信兜底).
    baseline=True (first run): 记录但不输出通知 (历史事件不重放).
    Returns True (通知已就绪, 状态可保存).
    """
    notifications = output.get("notifications", [])
    if baseline:
        for n in notifications:
            n["baseline_skipped"] = True
        output["notifications"] = []
        return True
    if dry_run:
        for n in notifications:
            n["dry_run"] = True
        return True
    return True


def main():
    parser = argparse.ArgumentParser(description="Generic repo dispatcher")
    parser.add_argument("--repo", required=True, help="Repository key from the config file")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_FILE,
                        help="YAML config path (default: %(default)s)")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="State directory (default: %(default)s)")
    parser.add_argument("--validate-config", action="store_true",
                        help="Validate the selected repository config and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan once without sending messages or updating state")
    args = parser.parse_args()

    try:
        cfg = load_config(args.repo, args.config)
    except ValueError as exc:
        parser.error(str(exc))
    if args.validate_config:
        print(json.dumps({"repo_key": args.repo, "repo": cfg["repo"], "valid": True}))
        return

    repo = cfg["repo"]
    projects = cfg.get("projects", [])
    sm, _ = build_session_map(cfg)
    safe_repo_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.repo)
    state_file = args.state_dir.expanduser() / ("dispatcher_%s_state.json" % safe_repo_key)
    prefix = "[%s]" % args.repo

    output = {"ts": time.time(), "actions": [], "warnings": [], "_pending": {}}
    loaded_state = load_state(state_file)
    prev_state = loaded_state if loaded_state is not None else {}
    new_state = dict(prev_state)
    # First run (no state file, or corrupt state backed up): baseline only —
    # never replay historical events. New repos join the dispatcher from the
    # moment of first tick; backlog is dropped.
    first_run = not state_file.exists()
    proj_map = {}
    control_plane_ok = False

    # ── 1. Project Status changes ──
    # 契约层八态: 状态变化 → 通知对应角色 (谁裁决谁操作).
    # 通知对象: worker (Ready), auditor (EV Review), 用户 (Human).
    # PI 不接收通知 (主动轮询 GitHub); In Progress/PI Review/Blocked/Done
    # 仅记录状态 (PI 轮询自行感知, 成果汇报走 PM 对话接口).
    try:
        proj_map, items = build_issue_proj_map(cfg)
        for item in items:
            issue_num = item["number"]
            pn = item["project_num"]
            sk = project_state_key(pn, issue_num)
            prev_s = prev_state.get(sk, "Inbox")
            cur_s = item.get("status", "Inbox")

            if prev_s == cur_s:
                if sk not in prev_state:
                    new_state[sk] = cur_s
                continue

            # Fetch title (pr view for PRs, issue view for issues)
            if item.get("is_pr"):
                ir = subprocess.run(["gh", "pr", "view", str(issue_num), "--repo", repo,
                                     "--json", "title", "--jq", ".title"],
                                    capture_output=True, text=True, timeout=10)
            else:
                ir = subprocess.run(["gh", "issue", "view", str(issue_num), "--repo", repo,
                                     "--json", "title", "--jq", ".title"],
                                    capture_output=True, text=True, timeout=10)
            if ir.returncode != 0:
                raise ControlPlaneUnavailable(
                    "Unable to read title for %s #%d" %
                    ("PR" if item.get("is_pr") else "Issue", issue_num))
            title = ir.stdout.strip()
            url = ("https://github.com/%s/pull/%d" if item.get("is_pr") else "https://github.com/%s/issues/%d") % (repo, issue_num)
            owner = resolve_owner(pn, projects, sm)

            notify_role = None
            msg = None
            reason = None

            if cur_s == "Ready":
                # 契约: PI 拨 Ready → 通知 owner worker (project owner 角色) 开工
                notify_role = owner
                msg = format_goal("Issue #%d is READY — %s" % (issue_num, title), url)
                reason = "issue_ready"

            elif cur_s == "EV Review":
                # 契约: worker 完成拨 EV Review → 通知 auditor 独立审计
                notify_role = "auditor"
                msg = format_goal(
                    "Issue #%d in EV Review — %s (worker 已完成, 待独立审计)"
                    % (issue_num, title), url)
                reason = "ev_review_ready"

            elif cur_s == "Human":
                # 契约: PI 判定需真人干预 → 通知用户 (飞书主通道)
                notify_role = "user"
                msg = format_goal(
                    "⛔ Issue #%d 需【人工干预】— %s (owner: %s) — "
                    "PI 判定超出 AI 循环, 等待真人决策/处理"
                    % (issue_num, title, owner), url)
                reason = "issue_human_escalate"

            # 其余状态 (In Progress / PI Review / Blocked / Done / Inbox):
            # PI 主动轮询自行感知, dispatcher 仅记录, 不通知.
            if notify_role and msg:
                queue_goal(output, notify_role, msg, issue_num=issue_num)
                output["actions"].append({"node": sk, "state": cur_s, "role": notify_role,
                                          "reason": reason, "prev_status": prev_s,
                                          "sent": msg[:80], "result": "queued"})

            new_state[sk] = cur_s

        control_plane_ok = True
    except Exception as e:
        output["_pending"] = {}
        new_state = dict(prev_state)
        output["warnings"].append("%s control plane unavailable: %s" % (prefix, str(e)[:160]))

    # ── 2. 滞留监控 (WARN) + 活性检测 + 资源监控 ──
    # (v0_3: dispatcher 监控职责; 详见 monitor.py / tick 脚本)
    try:
        from dispatcher_monitor import run_monitor
        # notify 不传回调: 避免 queue_goal 先以 issue=None 写入再被带 issue
        # 的去重 (codex P1 r3: 锚点丢失). 统一从返回值取通知.
        mon = run_monitor(repo, project=projects[0] if projects else None,
                          state_dir=args.state_dir, notify=None,
                          items=items if control_plane_ok else None)
        for w in mon.get("warnings", []):
            output["warnings"].append("%s %s" % (prefix, w))
        for n in mon.get("notifications", []):
            # 保留 issue 锚点: Human 兜底计数依赖它 (codex P1)
            queue_goal(output, n["role"], n["message"], issue_num=n.get("issue"))
    except Exception as e:
        output["warnings"].append("%s monitor: %s" % (prefix, str(e)[:120]))

    delivery_ok = flush_goals(output, dry_run=args.dry_run, baseline=first_run, cfg=cfg)
    # Human 兜底: dispatcher 侧仅尽力而为的飞书提醒 (monitor 锁内计数限频);
    # 真正的 8h×3 邮件/短信兜底由 PI 第二通道负责 (独立轮询 GitHub,
    # 不依赖 dispatcher — 防 dispatcher 失灵). 见 docs/architecture-v0_3.md.
    if first_run:
        output["warnings"].append(
            "%s first run: baseline recorded, historical events not replayed" % prefix)
    if not args.dry_run:
        if delivery_ok:
            save_state(state_file, new_state)
        else:
            output["warnings"].append(
                "%s delivery failed; prior state retained for retry" % prefix)
            save_state(state_file, prev_state)
    print(json.dumps(output))


if __name__ == "__main__":
    main()
