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


def g05_reconcile(repo, prev_state, new_state, prefix, output,
                  batch=30, dedup_hours=24):
    """PI-GATE G05 合并后对账 — 分页全量扫描 merged PR.

    每 tick 处理一批 (batch 个), 游标 g05_cursor:<repo> 轮转推进,
    最终覆盖所有 merged PR (不再只扫最近 15 个). 同一 PR 的同一
    缺失项 dedup_hours 内只报警一次.

    Returns: 无 (结果写入 output["warnings"], 游标写入 new_state).
    """
    from pi_gates import check_g05_only
    # 分页获取全部 merged PR (按 merged 时间倒序)
    all_merged = []
    page = 1
    while True:
        mrun2 = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "merged",
             "--limit", "100", "--page", str(page),
             "--json", "number,mergedAt"],
            capture_output=True, text=True, timeout=20)
        if mrun2.returncode != 0:
            break
        batch_resp = json.loads(mrun2.stdout)
        if not batch_resp:
            break
        all_merged.extend(batch_resp)
        if len(batch_resp) < 100:
            break
        page += 1
        if page > 10:  # 最多 1000 个 PR 的安全上限
            break
    if not all_merged:
        return
    # 按 merged 时间倒序 (新的先对账)
    all_merged.sort(key=lambda p: p.get("mergedAt") or "", reverse=True)
    # 游标轮转: 从上次位置继续
    cursor_key = "g05_cursor:%s" % repo
    cursor = int(prev_state.get(cursor_key) or new_state.get(cursor_key) or 0)
    if cursor >= len(all_merged):
        cursor = 0  # 一轮完成, 重新开始
    batch_prs = all_merged[cursor:cursor + batch]
    new_state[cursor_key] = str(cursor + batch)
    for pr in batch_prs:
        pn = pr["number"]
        lr = subprocess.run(
            ["gh", "pr", "view", str(pn), "--repo", repo,
             "--json", "closingIssuesReferences",
             "--jq", ".closingIssuesReferences[].number"],
            capture_output=True, text=True, timeout=15)
        linked = [int(x) for x in lr.stdout.split() if x.strip()]
        for li in linked:  # P1-D: 所有 closing issues
            g05 = check_g05_only(repo, li)
            if g05[0] in ("REMIND", "FAIL"):
                item = g05[1][:60]
                dedup_key = "g05:%d:%d:%s" % (pn, li, item)
                now_ts = time.time()
                prev_ts = prev_state.get(dedup_key) or new_state.get(dedup_key)
                if prev_ts and (now_ts - float(prev_ts)) < dedup_hours * 3600:
                    continue  # 已报警过, 限频跳过
                icon = "⚠️" if g05[0] == "REMIND" else "⛔"
                output["warnings"].append(
                    "%s %s PI-GATE G05: PR #%d issue #%d 对账 — %s"
                    % (prefix, icon, pn, li, g05[1][:70]))
                new_state[dedup_key] = str(now_ts)


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
                'nodes{id content{__typename ... on Issue{number title '
                'blockedBy(first:10){nodes{... on Issue{number state}} '
                'issueDependenciesSummary{blockedBy}} '
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
                    "title": content.get("title", ""),
                    "status": status,
                    "project_num": pn,
                    "project_name": proj.get("name", str(pn)),
                    "_item_id": node["id"],
                    "is_pr": content.get("__typename") == "PullRequest",
                    # 门禁依据: issueDependenciesSummary.blockedBy 开放依赖总数 (不受 first 限制)
                    # 节点列表仅用于 warning 展示 (最多 10 个)
                    "blocked_by_count": (
                        content.get("issueDependenciesSummary", {}).get("blockedBy") or 0
                    ),
                    "blocked_by": [
                        b["number"] for b in (content.get("blockedBy", {}).get("nodes") or [])
                        if b.get("state") != "CLOSED"
                    ],
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





def stale_status_check(items, projects, sm, prev_state, new_state, output,
                       now=None, stale_minutes=5, dedup_minutes=30):
    """Ready/EV Review/Blocked 停留超阈值 → 报警 + 重唤醒 (防静默断链).

    2026-08-13: #208 Ready 静默 14min 无人认领 (dispatcher 通知一次失败即丢失).
    对每个卡住状态: 若停留 > stale_minutes 且 dedup 窗口内未报过 →
    warning + 重发唤醒消息 (Ready→owner / EV Review→auditor / Blocked→user).

    注意 (codex 审核修复):
    - items 由调用方传入 (主循环已查询, 避免重复控制面快照/API 消耗)
    - 同一 role 的多个 stale issue 聚合成一条通知 (防 /goal 扇出覆盖)
    - dedup 随状态变化清除 (防重入静默); warnings 用字符串 (兼容现有消费者)
    - float 解析安全 (坏值跳过); 无 try 外层崩溃
    """
    now = now or time.time()
    if not items:
        return
    # 契约: 只监控会 wake 的状态 (Ready→owner, EV Review→auditor)
    # Blocked 不 wake (PI 主动轮询 — 简化契约, codex P1-4)
    role_map = {"Ready": None, "EV Review": "auditor"}
    import math
    stale_hits = []  # (issue_num, cur_s, age_min, target_role, msg)
    for item in items:
        if item.get("is_pr"):
            continue
        issue_num = item["number"]
        pn = item["project_num"]
        cur_s = item.get("status", "Inbox")
        if cur_s not in role_map:
            continue
        # 依赖等待的 Ready (blockedBy 开放) 不是 stale — 跳过, 且清除旧计时
        # (等待期不计入 stale — codex P1-3)
        if cur_s == "Ready" and item.get("blocked_by_count", 0) > 0:
            new_state.pop("stale_since:%d" % issue_num, None)
            new_state.pop("stale_status:%d" % issue_num, None)
            continue
        sk = project_state_key(pn, issue_num)
        since_key = "stale_since:%d" % issue_num
        status_key = "stale_status:%d" % issue_num
        dedup_key = "stale_dedup:%d:%s" % (issue_num, cur_s)
        now_ts = now
        try:
            if prev_state.get(since_key) and prev_state.get(status_key) == cur_s:
                since_ts = float(prev_state[since_key])
                if not math.isfinite(since_ts):
                    raise ValueError("non-finite timestamp")
            else:
                since_ts = now_ts
                new_state[since_key] = str(now_ts)
                new_state[status_key] = cur_s
                continue  # 刚进入该状态, 未停留
            age_min = (now_ts - since_ts) / 60.0
            if age_min < stale_minutes:
                continue
            # dedup 只限制"通知频率" (30min 一次), 不抑制 warning —
            # warning 每 tick 输出 (cron digest 持续可见, 通知丢失不静默).
            # at-least-once 语义: 通知进队列即记 dedup 是 best-effort,
            # wrapper 无回写确认 — 由 warning 持续可见兜底 (codex P1-3 折中).
            last_dedup = prev_state.get(dedup_key)
            notify_now = True
            if last_dedup:
                try:
                    dedup_ts = float(last_dedup)
                    if not math.isfinite(dedup_ts):
                        raise ValueError("non-finite dedup")
                    if (now_ts - dedup_ts) < dedup_minutes * 60:
                        notify_now = False
                except (TypeError, ValueError):
                    pass  # 坏 dedup 值 → 忽略, 重新报警
        except (TypeError, ValueError):
            new_state.pop(since_key, None)
            new_state.pop(status_key, None)
            continue  # 坏状态值 → 重置计时, 跳过本轮
        owner = resolve_owner(pn, projects, sm)
        if cur_s == "Ready":
            target_role = owner
        elif cur_s == "EV Review":
            target_role = sm.get("auditor")  # session_map 映射; 缺失 → fail closed
        else:  # Blocked
            target_role = "user"
        if not target_role:
            # 映射缺失 → 报警 (fail closed, 不静默投递字面角色)
            output["warnings"].append(
                "config_error: stale %s for Issue #%d has no target role (session_map 缺映射)"
                % (cur_s, issue_num))
            continue
        # warning 每 tick 输出 (即使 dedup 抑制通知 — 保证可见性)
        output["warnings"].append(
            "status_stale: Issue #%d %s for %.0f min (role: %s%s)"
            % (issue_num, cur_s, age_min, target_role,
               " — re-notified" if notify_now else " — still stale"))
        if notify_now:
            stale_hits.append((issue_num, cur_s, age_min, target_role))
            new_state[dedup_key] = str(now_ts)
    # 按 role 聚合 → 一条通知 (防同一 session 多 /goal 扇出覆盖)
    by_role = {}
    for issue_num, cur_s, age_min, target_role in stale_hits:
        by_role.setdefault(target_role, []).append(issue_num)
    for role, nums in by_role.items():
        msg = format_goal(
            "Issue(s) %s 状态停留超阈值 (Ready/EV Review/Blocked, ≥%dmin) — 需动作"
            % (",".join("#%d" % n for n in nums), stale_minutes), "")
        queue_goal(output, role, msg)


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


def run_status_loop(items, projects, sm, prev_state, new_state, output,
                    dry_run=False, repo="", title_fetcher=None):
    """状态循环: 基于 GitHub 实时状态推导 → 通知对应角色 (简单推导).

    决策完全由当前 GitHub 状态 (status + blockedBy) 推导, state 只做去重:
      - Ready + 无开放依赖 → wake owner worker
      - EV Review → wake auditor
      - Human → wake user (飞书)
      其余状态不 wake (PI 主动轮询).

    去重: 用 "上次通知快照" (sent_ready/sent_ev/sent_human) 避免重复 wake;
    依赖解除 (blocked_by_count 变化) 自然触发新通知.
    """
    for item in items:
        issue_num = item["number"]
        pn = item["project_num"]
        sk = project_state_key(pn, issue_num)
        cur_s = item.get("status", "Inbox")
        dep_count = item.get("blocked_by_count", 0)

        # title 来自 GraphQL (item["title"]), 无需额外 gh view 查询 (codex P1-2)
        title = item.get("title") or title_fetcher(issue_num, item.get("is_pr")) if title_fetcher else ""
        if not title and not title_fetcher:
            raise ControlPlaneUnavailable("Unable to read title for #%d" % issue_num)
        url = ("https://github.com/%s/pull/%d" if item.get("is_pr") else "https://github.com/%s/issues/%d") % (repo, issue_num)
        owner = resolve_owner(pn, projects, sm)

        notify_role = None
        msg = None
        reason = None

        if cur_s == "Ready":
            # 简单推导: Ready + 无开放依赖 → wake owner
            if dep_count == 0:
                # 去重: 上次通知过 (同依赖数) → 跳过
                if prev_state.get("sent_ready:" + sk) == "d%d" % dep_count:
                    reason = None  # 已通知过, 静默
                else:
                    notify_role = owner
                    msg = format_goal("Issue #%d is READY — %s" % (issue_num, title), url)
                    reason = "issue_ready"
                    new_state["sent_ready:" + sk] = "d%d" % dep_count
            else:
                # 依赖等待: 不 wake; 记录 warning (每 tick 重算, 解除自然触发)
                reason = None
                new_state.pop("sent_ready:" + sk, None)  # 依赖变化 → 重置去重
                output["warnings"].append(
                    "dependency: Issue #%d READY but blockedBy %d open — 等待依赖, 不派发"
                    % (issue_num, dep_count))

        elif cur_s == "EV Review":
            # 简单推导: EV Review → wake auditor (session_map 映射)
            notify_role = sm.get("auditor")
            if not notify_role:
                output["warnings"].append(
                    "config_error: session_map missing 'auditor' — EV Review 通知无法路由")
            elif prev_state.get("sent_ev:" + sk) != "1":
                msg = format_goal(
                    "Issue #%d in EV Review — %s (worker 已完成, 待独立审计)"
                    % (issue_num, title), url)
                reason = "ev_review_ready"
                new_state["sent_ev:" + sk] = "1"

        elif cur_s == "PI Review":
            # 待 PI 终审: 不自动通知 (PI 主动轮询)
            reason = None

        elif cur_s == "Human":
            # 简单推导: Human → wake user (飞书主通道)
            if prev_state.get("sent_human:" + sk) != "1":
                notify_role = "user"
                msg = format_goal(
                    "⛔ Issue #%d 需【人工干预】— %s (owner: %s) — "
                    "PI 判定超出 AI 循环, 等待真人决策/处理"
                    % (issue_num, title, owner), url)
                reason = "issue_human_escalate"
                new_state["sent_human:" + sk] = "1"

        # 其余状态 (In Progress / Blocked / Done / Inbox / Cancelled): 不 wake
        # 状态切换事件对用户 digest 可见 (reason 非 None 即记录 action)
        if reason:
            action_msg = msg or format_notice(
                "Issue #%d 状态: %s — %s" % (issue_num, cur_s, title), url)
            output["actions"].append({"node": sk, "state": cur_s, "role": notify_role,
                                      "reason": reason, "prev_status": prev_state.get(sk, "Inbox"),
                                      "sent": action_msg[:80], "result": "queued"})

        if notify_role and msg:
            queue_goal(output, notify_role, msg, issue_num=issue_num)

        new_state[sk] = cur_s
        # 状态变化 → 清除 stale 停留记录 + dedup + sent_* 去重标记
        # (sent_* 必须离开状态时清除: Ready→In Progress→Ready 第二次要能唤醒 — codex P1-1)
        if prev_state.get(sk) != cur_s:
            new_state.pop("stale_since:%d" % issue_num, None)
            new_state.pop("stale_status:%d" % issue_num, None)
            for dk in list(new_state):
                if dk.startswith("stale_dedup:%d:" % issue_num):
                    new_state.pop(dk, None)
            new_state.pop("sent_ready:" + sk, None)
            new_state.pop("sent_ev:" + sk, None)
            new_state.pop("sent_human:" + sk, None)

    return output


def main():
    """CLI 入口: 一轮 tick (no_agent cron 调用)."""
    parser = argparse.ArgumentParser(description="GitHub Project dispatcher (one tick)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILE,
                        help="Path to dispatcher.yaml")
    parser.add_argument("--repo", required=True, help="Repository key (config section)")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR,
                        help="Directory for state files")
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
    items = []
    try:
        proj_map, items = build_issue_proj_map(cfg)
        run_status_loop(items, projects, sm, prev_state, new_state, output,
                        dry_run=args.dry_run, repo=repo)

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
                          items=items if control_plane_ok else None,
                          dry_run=args.dry_run)
        for w in mon.get("warnings", []):
            output["warnings"].append("%s %s" % (prefix, w))
        for n in mon.get("notifications", []):
            # 保留 issue 锚点: Human 兜底计数依赖它 (codex P1)
            queue_goal(output, n["role"], n["message"], issue_num=n.get("issue"))
    except Exception as e:
        output["warnings"].append("%s monitor: %s" % (prefix, str(e)[:120]))

    # ── 3. PR merged 检测 ──
    # v0_3 重构误删了 PR 监控; 恢复 merged 通知 (用户 digest 白名单 🎉).
    # 检测: gh pr list --state merged 近期合并 → 通知用户 (author receipt).
    # 路径 A: 最近合并的 PR 扫描 (catch-up, 不依赖 project membership).
    try:
        mrun = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "merged",
             "--limit", "10", "--json", "number,title,mergedAt,author"],
            capture_output=True, text=True, timeout=20)
        if mrun.returncode == 0:
            merged_prs = json.loads(mrun.stdout)
            for pr in merged_prs:
                pn = pr["number"]
                key = "pr:%d" % pn
                prev_m = prev_state.get(key)
                if prev_m == "merged":
                    continue
                if new_state.get(key) == "merged":
                    continue  # 同 tick 已处理
                merged_at = pr.get("mergedAt") or ""
                title = pr.get("title") or ""
                url = "https://github.com/%s/pull/%d" % (repo, pn)
                msg = format_notice(
                    "PR #%d has been MERGED! — %s" % (pn, title), url)
                reason = "pr_merged_recent" if not prev_m else "pr_merged"
                output["actions"].append({"node": key, "state": "merged",
                                          "role": None, "reason": reason,
                                          "prev_status": prev_m,
                                          "sent": msg[:80], "result": "queued"})
                queue_goal(output, "user", msg, issue_num=pn)
                new_state[key] = "merged"

    except Exception as e:
        output["warnings"].append("%s PR merged scan: %s" % (prefix, str(e)[:120]))

    # ── PI-GATE G05: 合并后对账 (独立段 — 每次 tick 都对已 merged 的
    #    PR 跑对账; P0-4: 不在首次 merged 检测内, 避免 prev_m==merged
    #    continue 导致 G05 只跑一次). 全量分页 + 游标轮转 + 去重限频.
    #    已抽成 g05_reconcile() (可单测). ──
    try:
        g05_reconcile(repo, prev_state, new_state, prefix, output)
    except Exception as e:
        output["warnings"].append(
            "%s PI-GATE G05 reconcile: %s" % (prefix, str(e)[:100]))

    # ── PI-GATE live StatusContext 发布 (advisory) ──
    # 受保护的 dispatcher 读取实时状态 → 向 open PRs 的 HEAD 发布
    # pi-gates-live commit status (advisory — 不阻断 merge, hard gate
    # 需 GitHub App, 已延期). 限频: 每 tick 最多 5 个.
    # C1: published 检查独立 if — gate=failure + 发布失败要明确提示
    #     "status 发布失败" (不只是 "gate 阻断").
    # C2: 所有结论 (success/failure) 都记 digest + gate evidence hash —
    #     failure→success→failure 且 HEAD 不变时, 第二次 failure 仍提醒.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from gate_check import scan_and_publish
        published = scan_and_publish(repo, limit=5)
        for p in published:
            dedup_key = "gate_warn:%s:%d" % (repo, p["pr"])
            # C2: digest = 结论 + HEAD + 发布状态 (全结论覆盖)
            digest = "%s:%s:%s" % (p.get("conclusion"), p.get("head", ""),
                                   p.get("published"))
            prev_digest = prev_state.get(dedup_key) or new_state.get(dedup_key)
            # C1: 发布失败独立检查 (无论 gate 结论)
            if not p.get("published"):
                output["warnings"].append(
                    "%s ⛔ PI-GATE live: PR #%d status 发布失败 (%s)"
                    % (prefix, p["pr"], p.get("conclusion")))
                new_state[dedup_key] = digest
            elif p.get("conclusion") == "failure" and prev_digest != digest:
                output["warnings"].append(
                    "%s ⛔ PI-GATE live: PR #%d 被 gate 阻断 (%s)"
                    % (prefix, p["pr"], digest))
                new_state[dedup_key] = digest
            elif p.get("conclusion") == "success" and prev_digest != digest:
                # C2: success 也更新 digest — 状态翻转会被下一轮 failure 检测
                new_state[dedup_key] = digest
    except Exception as e:
        output["warnings"].append(
            "%s PI-GATE live check-run: %s" % (prefix, str(e)[:100]))

    delivery_ok = flush_goals(output, dry_run=args.dry_run, baseline=first_run, cfg=cfg)
    # 状态停留报警: Ready/EV Review/Blocked 超过阈值未动作 → 报警 + 重唤醒
    # (items 用主循环已查询的, 不重复控制面查询; dry_run 下也标记不投递)
    if not first_run:
        stale_status_check(items, projects, sm, prev_state, new_state, output,
                           stale_minutes=5, dedup_minutes=30)
    if args.dry_run:
        for n in output.get("notifications", []):
            n["dry_run"] = True
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
