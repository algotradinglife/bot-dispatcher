"""dispatcher_monitor — v0_3 监控模块 (dispatcher 监控职责).

检测:
  1. 滞留 WARN: 状态超阈值 → token 成本提示 (不介入)
  2. 活性检测: In Progress 但 heartbeat/文件写入停止 → 可能挂起
  3. 资源监控: 磁盘 >90% / 僵尸进程爆量
  4. Human 超时兜底: Human 持续 8h+ → 每小时 1 次, 上限 3 次 (邮件/短信通道)
  5. 心跳写入: 每次 tick 更新心跳文件 (外层 watchdog 检测 dispatcher 活性)
  6. 事件日志: append 所有检测结果 (排错可回溯)
  7. 交付链断链 (C 项): In Progress 超阈值 + 有 ready PR → 复用
     finalize_delivery.finalize(--check-only) 同一套校验: PR ready /
     Closes issue / HEAD 匹配全部通过但状态仍 In Progress → 断链报警
     (worker 推了 PR 但 finalize 未跑/失败)

通知: 统一经 notify(role, message) 输出; role=user 走飞书主通道,
level 3 异常由外层触发邮件/短信双通道.

独立可跑 (python3 dispatcher_monitor.py --repo X --state-dir Y),
也可被 dispatcher 内嵌调用 (run_monitor).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# 默认阈值 (秒): 滞留 WARN
STALE_THRESHOLDS = {
    "Ready": 3 * 86400,        # 3 天没人接
    "In Progress": 3600,       # 1 小时 (试点收紧: worker 卡死快速发现)
    "EV Review": 2 * 86400,    # 2 天 EV 积压
    "PI Review": 2 * 86400,    # 2 天 PI 积压
    "Blocked": 2 * 86400,      # 2 天诉求未处理
}
HUMAN_ESCAPE_HOURS = 8          # Human 持续 8h → 兜底通知
HUMAN_ESCAPE_MAX = 3            # 每小时 1 次, 上限 3 次
DISK_WARN_PCT = 90              # 磁盘使用率告警线
ZOMBIE_WARN_COUNT = 5           # 同脚本僵尸进程数告警线
HEARTBEAT_INTERVAL = 300        # 心跳文件新鲜度阈值 (5 分钟)

# C 项: 交付链断链检测
# PR ready 后多久未 finalize 视为断链 (复用滞留阈值, worker 推 PR 后
# 应立刻跑 finalize; 超过 In Progress 阈值仍未拨状态 = 断链)
CHAIN_BREAK_NOTIFY_MAX = 3      # 每 issue 最多提醒次数
CHAIN_BREAK_NOTIFY_INTERVAL = 3600  # 两次提醒最小间隔 (1h)
CHAIN_BREAK_RESET_HOURS = 24    # 限频时间衰减: 首次提醒 24h 后重置计数

MONITOR_STATE = "monitor_state.json"


def _now() -> float:
    return time.time()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_heartbeat(state_dir: Path) -> None:
    """每次 tick 更新心跳文件 (外层 watchdog 检查新鲜度)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    hb = state_dir / "dispatcher.heartbeat"
    hb.write_text(json.dumps({"ts": _ts(), "tick": _now()}))
    # 事件日志 append
    log = state_dir / "dispatcher_events.log"
    with log.open("a") as f:
        f.write("%s tick\n" % _ts())


def _load_monitor_state(state_dir: Path) -> dict:
    f = state_dir / MONITOR_STATE
    if f.exists():
        try:
            data = json.loads(f.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"entered": {}, "human_notify": {}, "chain_notify": {}}


def _save_monitor_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / ("%s.tmp.%d" % (MONITOR_STATE, os.getpid()))
    tmp.write_text(json.dumps(state))
    tmp.replace(state_dir / MONITOR_STATE)


def _with_state_lock(state_dir: Path, fn, dry_run: bool = False):
    """flock 包裹状态读改写, 防并发覆盖 (codex P1: 无锁竞态).

    macOS flock 同进程二次 open 会阻塞自己 (不可重入) — 锁内
    不得再调用本函数或嵌套 flock. 调用方必须一次性完成读-改-写.

    dry_run=True: 不加锁直接执行 (只读快照, 不创建 .lock 文件 —
    dry-run 承诺零磁盘副作用; 且 dry-run 不写状态, 无需互斥).
    """
    state_dir = Path(state_dir)
    if dry_run:
        return fn()  # 只读快照: 不加锁、不建目录、不创建 .lock
    state_dir.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock_f = open(state_dir / (MONITOR_STATE + ".lock"), "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
        finally:
            lock_f.close()


def _disk_usage_pct() -> float | None:
    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        return usage.used / usage.total * 100
    except Exception:
        return None


def _zombie_processes(pattern: str, exclude_self: bool = True) -> list[int]:
    """查找匹配 pattern 的进程 (僵尸/挂起检测)."""
    try:
        r = subprocess.run(["pgrep", "-f", pattern],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        pids = [int(x) for x in r.stdout.split() if x.strip()]
        if exclude_self:
            pids = [p for p in pids if p != os.getpid()]
        return pids
    except Exception:
        return []


def _check_activity(state_dir: Path, workdirs: list[Path]) -> list[str]:
    """活性检测: 工作目录 heartbeat 文件 / 最新文件 mtime."""
    stale = []
    for wd in workdirs:
        if not wd.exists():
            continue
        try:
            latest = 0.0
            for f in wd.rglob("*"):
                if f.is_file():
                    try:
                        latest = max(latest, f.stat().st_mtime)
                    except OSError:
                        pass
            if latest and (_now() - latest) > HEARTBEAT_INTERVAL * 12:
                stale.append(str(wd))
        except Exception:
            continue
    return stale


def _open_prs_for_issue(repo: str, issue_num) -> tuple[list, str | None]:
    """查关联 issue 的 open PR (gh pr list --search "issue:N").

    Returns (prs, err): prs=[{"number","isDraft","headRefOid","updatedAt"}],
    err=None 成功 / 非 None 查询失败 (调用方应可观测, 不得静默).
    repo 为完整 owner/name (配置契约) — gh CLI 原样接受, 不重写 owner.
    """
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--repo", repo,
             "--state", "open", "--search", "issue:%s" % issue_num,
             "--json", "number,isDraft,headRefOid,updatedAt"],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return [], "gh pr list 失败: %s" % r.stderr.strip()[:120]
        data = json.loads(r.stdout)
        if not isinstance(data, list):
            return [], "gh pr list 输出非数组"
        return data, None
    except Exception as e:
        return [], "gh pr list 异常: %s" % str(e)[:120]


def _check_delivery_chain(repo: str, issue_num, pr: dict) -> dict:
    """复用 finalize_delivery.finalize(--check-only) 校验交付链.

    check-only 全过 (PR ready / Closes issue / HEAD 匹配 / In Progress /
    白名单转换) → 交付链完整但状态未拨 → 断链.
    Returns {"broken": bool, "detail": str, "error": str|None, "steps": list}.
    error 非 None 表示基础设施/读取失败 (调用方应可观测, 不得静默).
    """
    from finalize_delivery import finalize
    # finalize 内部硬编码 algotradinglife 组织 → 传 name 段 (其契约);
    # gh CLI 侧 (调用方) 用完整 owner/name, 不在此重写.
    repo_name = repo.split("/")[-1]
    try:
        ok, steps, _ = finalize(repo_name, int(issue_num), int(pr["number"]),
                                expected_head=pr.get("headRefOid") or "",
                                check_only=True, verbose=False)
    except Exception as e:
        return {"broken": False, "detail": "finalize 调用异常",
                "error": "finalize 调用异常: %s" % str(e)[:100],
                "steps": []}
    if ok:
        # check-only 全绿 → 交付链完整但状态仍是 In Progress → 断链
        return {"broken": True, "detail": "PR #%d ready + Closes issue + HEAD 匹配, "
                "但 issue 仍 In Progress (finalize 未跑/失败)" % pr["number"],
                "error": None, "steps": steps}
    # 校验失败: 看哪一步断了. 读取类步骤失败 (pr_read/issue_project) 是
    # 基础设施问题 → error 非 None (可观测); 业务校验失败 (draft/不 closes)
    # 只是交付链未建立, 不算失明. (field_ids/ev_option_exists 只在非
    # check-only 路径执行, check_only=True 时不可达, 不列入.)
    failed = [s for s in steps if not s["ok"]]
    names = ", ".join(s["step"] for s in failed) if failed else "?"
    read_fail = [s for s in failed if s["step"] in ("pr_read", "issue_project")]
    if read_fail:
        return {"broken": False, "detail": "PR #%d 读取失败 (%s)"
                % (pr["number"], names),
                "error": "finalize 读取失败 (%s): %s"
                % (names, read_fail[0].get("detail", "")),
                "steps": steps}
    return {"broken": False, "detail": "PR #%d 校验未通过 (%s), 交付链未建立"
            % (pr["number"], names), "error": None, "steps": steps}


def _chain_notify_acquire(state_dir: Path, num: str, now: float,
                          expected_since: float | None = None,
                          dry_run: bool = False) -> bool:
    """锁内原子: 限频判断 + 记账 + 持久化 (read-modify-write).

    调用方必须在**同一个锁回调内**完成判断后的 emit (append), 否则并发
    tick 可突破限频 (tick→emit→commit 解锁窗口). 本函数只返回是否应发,
    记账已在此完成 (emit 后崩溃 → 下 tick 被限频拦截属正常: 通知已入
    内存输出, dispatcher at-least-once 兜底投递).

    expected_since: 断链判断时的 entered.since 快照 — 确认状态未变
    (防 reset 后旧 tick 重建计数 / 防状态已离开 In Progress 仍通知).
    dry_run=True: 只判断不记账 (不消耗额度, 不写盘).
    """
    mstate = _load_monitor_state(state_dir)
    chain = mstate.setdefault("chain_notify", {})
    # epoch 校验: issue 状态离开 In Progress → 不通知 (旧 tick 作废)
    if expected_since is not None:
        ent = (mstate.get("entered") or {}).get(num) or {}
        if ent.get("status") != "In Progress" or ent.get("since") != expected_since:
            return False
    count = chain.get(num, 0)
    last = chain.get(num + ":last", 0)
    first = chain.get(num + ":first", 0)
    # 限频时间衰减: 首次提醒超 CHAIN_BREAK_RESET_HOURS (24h) → 重置计数
    # (防'通知丢失/投递失败导致额度永久耗尽 → 断链永久静默'; 24h 后重新计)
    if first and (now - first) > CHAIN_BREAK_RESET_HOURS * 3600:
        count = 0
        first = 0
    if count >= CHAIN_BREAK_NOTIFY_MAX:
        return False
    if count > 0 and (now - last) < CHAIN_BREAK_NOTIFY_INTERVAL:
        return False
    if not dry_run:
        if not first:
            chain[num + ":first"] = now
        chain[num] = count + 1
        chain[num + ":last"] = now
        _save_monitor_state(state_dir, mstate)
    return True


def _pr_updated_recently(pr: dict, now: float, grace: int = 900) -> bool:
    """PR 刚创建/刚更新 (<grace 秒) → grace window 内, 不算断链.

    PR 的 updatedAt 是 GitHub ISO 时间; 解析失败 → 不算 recent (保守,
    允许检测继续, 不因格式问题漏报). 未来时间 (时钟偏差, age<0) →
    视为 recent (不报, 防误报).
    """
    updated = pr.get("updatedAt") or ""
    if not updated:
        return False
    try:
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(updated.replace("Z", "+00:00"))
        age = now - dt.timestamp()
        if age < 0:
            return True  # 未来时间戳 (时钟偏差) → 保守视为 recent
        return age < grace
    except Exception:
        return False


def _reset_chain_notify(state_dir: Path, active_issues) -> None:
    """清掉当前不在 In Progress 的 issue 的 chain_notify 计数.

    issue 离开 In Progress (finalize/驳回/完成) → 限频计数作废,
    下次再断链重新计.
    """
    mstate = _load_monitor_state(state_dir)
    chain = mstate.setdefault("chain_notify", {})
    changed = False
    for key in list(chain.keys()):
        num = key.split(":")[0]
        if num not in active_issues:
            del chain[key]
            changed = True
    if changed:
        _save_monitor_state(state_dir, mstate)


def run_monitor(repo: str, project: dict | None, state_dir: Path,
                notify=None, items: list | None = None,
                workdirs: list[Path] | None = None,
                dry_run: bool = False) -> dict:
    """执行一轮监控检测, 返回 {warnings, notifications}.

    notify(role, message) 回调: 记录通知事件 (role: user/worker/auditor).
    dry_run=True: 检测照跑, 但不写任何状态 (monitor_state / chain_notify),
    遵守 dispatcher 'Dry-run 不写状态' 铁律.
    """
    state_dir = Path(state_dir)
    # dry_run 承诺零磁盘副作用: 目录不存在则不创建 (不写盘)
    if not dry_run:
        state_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    notifications: list[dict] = []
    notify_cb = notify  # 回调只在锁外统一调用 (防锁内慢回调阻塞)

    def emit(role, message, level=1, issue=None):
        """append 通知到内存输出 (可锁内调用, 微秒级).

        notify 回调不在此调用 — 由 _flush_notify 在锁外统一执行,
        保证限频判断/记账/入队原子, 而慢回调不持锁.
        """
        notifications.append({"role": role, "message": message, "level": level,
                              "issue": issue})

    def _flush_notify():
        if notify_cb:
            for n in notifications:
                try:
                    notify_cb(n["role"], n["message"])
                except Exception:
                    pass  # 回调失败不影响主流程

    if not dry_run:
        _write_heartbeat(state_dir)

    # ── 1. 滞留 WARN + Human 超时兜底 (基于 state 里的 entered 时间) ──
    now = _now()

    # items: [{number, status}] — 来自 dispatcher 或独立查询
    # items_ok: items 是否成功加载 (查询失败 → False, 后续不得 reset 计数,
    # 防'查询失败清空限频计数' — P2)
    items_ok = items is not None  # dispatcher 传入 = 已成功加载
    if items is None:
        items = []
        if project and project.get("node"):
            try:
                r = subprocess.run(
                    ["gh", "api", "graphql",
                     "-f", "query=query($p: ID!) { node(id: $p) { "
                           "... on ProjectV2 { items(first: 100) { nodes {"
                           " content { ... on Issue { number } }"
                           " status: fieldValueByName(name: \"Status\") {"
                           " ... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }",
                     "-F", "p=%s" % project["node"]],
                    capture_output=True, text=True, timeout=20)
                if r.returncode == 0:
                    data = json.loads(r.stdout)
                    for node in data["data"]["node"]["items"]["nodes"]:
                        content = node.get("content") or {}
                        status = ((node.get("status") or {}).get("name")
                                  or "Inbox")
                        items.append({"number": content.get("number"),
                                      "status": status})
                    items_ok = True
                else:
                    warnings.append("monitor: 状态查询失败 %s"
                                    % r.stderr.strip()[:100])
            except Exception as exc:
                warnings.append("monitor: 状态查询失败 %s" % str(exc)[:100])

    current = {str(it.get("number")): it.get("status", "Inbox")
               for it in items if it.get("number") is not None}

    # ── 状态读-改-写原子化 (codex P1: 无锁并发覆盖) ──
    # 锁内: 加载 mstate → 更新 entered → 判断滞留/Human → 写回;
    # 锁外: emit 通知 (不阻塞锁, 回调可能慢).
    # dry_run: 内存态判断照跑, 不 _save_monitor_state (遵守不写状态铁律).
    def _judge():
        mstate = _load_monitor_state(state_dir)
        entered = mstate.setdefault("entered", {})
        human_notify = mstate.setdefault("human_notify", {})
        pending = []

        # 记录状态进入时间 (状态未变则保留原 since)
        for num, st in current.items():
            prev = entered.get(num)
            if not isinstance(prev, dict) or prev.get("status") != st:
                entered[num] = {"status": st, "since": now}

        # 滞留 WARN (状态超阈值且仍在该状态)
        for num, info in entered.items():
            st = info.get("status")
            since = info.get("since", now)
            thr = STALE_THRESHOLDS.get(st)
            if not thr:
                continue
            if current.get(num) != st:
                continue  # 状态已变
            age = now - since
            if age > thr:
                # WARN: token 成本提示, 不介入
                pending.append(("user",
                    "⚠️ 滞留 WARN: issue #%s 在 [%s] 已 %d 天 "
                    "(token 成本提示, 不介入)" % (num, st, int(age / 86400)),
                    1, num))

        # Human 超时兜底: 持续 8h+ → 每小时 1 次, 上限 3 次
        # 计数在锁内自增 (尽力而为的提醒限频, 防每 tick 刷屏).
        # 真正的 8h×3 邮件/短信兜底由 PI 第二通道负责 (独立轮询 GitHub,
        # 不依赖 dispatcher — 防 dispatcher 失灵). 见 docs/architecture-v0_3.md.
        for num, info in entered.items():
            if info.get("status") != "Human":
                continue
            if current.get(num) != "Human":
                continue
            since = info.get("since", now)
            age_h = (now - since) / 3600
            if age_h >= HUMAN_ESCAPE_HOURS:
                count = human_notify.get(num, 0)
                if count < HUMAN_ESCAPE_MAX:
                    # 已发过且距上次 <1h 则跳过
                    last = human_notify.get(num + ":last", 0)
                    if count > 0 and (now - last) < 3600:
                        continue
                    # 锁内自增计数 + 记录时间
                    human_notify[num] = count + 1
                    human_notify[num + ":last"] = now
                    pending.append(("user",
                        "⛔ Human 状态超时兜底: issue #%s 已 %d 小时无人处理 "
                        "(第 %d/3 次提醒, 邮件/短信通道)" % (
                            num, int(age_h), count + 1), 3, num))

        if not dry_run:
            _save_monitor_state(state_dir, mstate)
        return pending

    pending = _with_state_lock(state_dir, _judge, dry_run=dry_run)
    for role, message, level, issue in pending:
        emit(role, message, level=level, issue=issue)

    # ── 2. 活性检测 (仅当有 In Progress issue 时检查工作目录活动) ──
    in_progress = [num for num, st in current.items() if st == "In Progress"]
    if workdirs and in_progress:
        stale_dirs = _check_activity(state_dir, workdirs)
        if stale_dirs:
            emit("user", "⚠️ 活性消失: 工作目录无写入活动 %s "
                         "(worker 可能挂起)" % ", ".join(stale_dirs[:2]),
                 level=2)

    # ── 3. 资源监控 ──
    disk = _disk_usage_pct()
    if disk is not None and disk > DISK_WARN_PCT:
        emit("user", "🚨 磁盘使用率 %.0f%% (>%d%%) — 写入可能失败"
                     % (disk, DISK_WARN_PCT), level=3)
    # 僵尸进程: 同脚本多实例 (dispatcher 自身重复运行)
    zombies = _zombie_processes("dispatcher.py")
    if len(zombies) > ZOMBIE_WARN_COUNT:
        emit("user", "🚨 dispatcher 多实例并存 %d 个 (>%d) — 疑似僵尸累积"
                     % (len(zombies), ZOMBIE_WARN_COUNT), level=3)

    # ── 4. 交付链断链 (C 项): In Progress 超阈值 + open PR → 复用
    #      finalize --check-only 校验. 全绿但状态未拨 = 断链. ──
    for num in in_progress:
        entered_info = {}
        try:
            entered_info = _with_state_lock(
                state_dir, lambda: _load_monitor_state(state_dir)
                .setdefault("entered", {}).get(num) or {},
                dry_run=dry_run)
        except Exception:
            pass
        since = entered_info.get("since", now) if entered_info else now
        age = now - since
        thr = STALE_THRESHOLDS.get("In Progress", 3600)
        if age <= thr:
            continue  # 未超阈值: worker 可能还在开发, 不算断链
        prs, pr_err = _open_prs_for_issue(repo, num)
        if pr_err:
            # 可观测性 (codex P1): 查询失败写入 warnings, 不得静默失明
            warnings.append("monitor: 断链检测 PR 查询失败 (可能失明): %s"
                            % pr_err)
            continue
        for pr in prs:
            # grace window: PR 刚创建/刚更新 (<15min) → worker 可能还没跑
            # finalize, 不报断链 (减少误报)
            if _pr_updated_recently(pr, now, grace=900):
                continue
            res = _check_delivery_chain(repo, num, pr)
            if not res["broken"]:
                # 可观测性 (codex P1): finalize 读取失败不能静默
                if res.get("error"):
                    warnings.append(
                        "monitor: 断链检测异常 (可能失明): %s" % res["error"])
                continue
            # 限频判断 + 记账 + 入队在同一锁回调内原子完成 (防并发 tick
            # 突破限频: tick→emit→commit 之间的解锁窗口会双发). emit 是
            # 内存 append (微秒级), notify 回调由 _flush_notify 锁外统一跑.
            notified = _with_state_lock(
                state_dir,
                lambda: _chain_notify_acquire(
                    state_dir, num, now,
                    expected_since=since, dry_run=dry_run),
                dry_run=dry_run)
            if notified:
                emit("user",
                     "🔗 交付链断链: issue #%s %s" % (num, res["detail"]),
                     level=2, issue=num)

    # 限频计数重置: issue 离开 In Progress (finalize/驳回/完成) → 清计数,
    # 下次再断链重新计 (codex P1: 限频按断链事件而非 issue 永久计数).
    # dry_run 不写状态; items 加载失败 → 不重置 (查询失败不得清计数).
    if not dry_run and items_ok:
        try:
            _with_state_lock(state_dir, lambda: _reset_chain_notify(
                state_dir, in_progress))
        except Exception:
            pass

    # notify 回调统一在锁外执行 (慢回调不持锁; 失败不影响主流程).
    # 必须在事件日志**之前** — 日志写入可失败 (磁盘满), 通知投递优先.
    _flush_notify()

    # 事件日志 (dry_run 不写盘; 失败不阻断返回)
    if not dry_run:
        try:
            log = state_dir / "dispatcher_events.log"
            with log.open("a") as f:
                for n in notifications:
                    f.write("%s %s level=%s %s\n" % (
                        _ts(), n["role"], n.get("level", 1), n["message"][:120]))
        except Exception:
            pass

    return {"warnings": warnings, "notifications": notifications}


def main() -> None:
    parser = argparse.ArgumentParser(description="dispatcher monitor")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", type=Path,
                        default=Path("~/.hermes/bot-dispatcher").expanduser())
    parser.add_argument("--workdirs", nargs="*", default=[])
    args = parser.parse_args()

    out = run_monitor(args.repo, None, args.state_dir,
                      workdirs=[Path(w) for w in args.workdirs])
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
