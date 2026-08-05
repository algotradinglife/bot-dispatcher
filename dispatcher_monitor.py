"""dispatcher_monitor — v0_3 监控模块 (dispatcher 监控职责).

检测:
  1. 滞留 WARN: 状态超阈值 → token 成本提示 (不介入)
  2. 活性检测: In Progress 但 heartbeat/文件写入停止 → 可能挂起
  3. 资源监控: 磁盘 >90% / 僵尸进程爆量
  4. Human 超时兜底: Human 持续 8h+ → 每小时 1 次, 上限 3 次 (邮件/短信通道)
  5. 心跳写入: 每次 tick 更新心跳文件 (外层 watchdog 检测 dispatcher 活性)
  6. 事件日志: append 所有检测结果 (排错可回溯)

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
    "In Progress": 5 * 86400,  # 5 天 (配合活性检测, 活性正常则不报)
    "EV Review": 2 * 86400,    # 2 天 EV 积压
    "PI Review": 2 * 86400,    # 2 天 PI 积压
    "Blocked": 2 * 86400,      # 2 天诉求未处理
}
HUMAN_ESCAPE_HOURS = 8          # Human 持续 8h → 兜底通知
HUMAN_ESCAPE_MAX = 3            # 每小时 1 次, 上限 3 次
DISK_WARN_PCT = 90              # 磁盘使用率告警线
ZOMBIE_WARN_COUNT = 5           # 同脚本僵尸进程数告警线
HEARTBEAT_INTERVAL = 300        # 心跳文件新鲜度阈值 (5 分钟)

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
    return {"entered": {}, "human_notify": {}}


def _save_monitor_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / ("%s.tmp.%d" % (MONITOR_STATE, os.getpid()))
    tmp.write_text(json.dumps(state))
    tmp.replace(state_dir / MONITOR_STATE)


def _with_state_lock(state_dir: Path, fn):
    """flock 包裹状态读改写, 防并发覆盖 (codex P1: 无锁竞态).

    macOS flock 同进程二次 open 会阻塞自己 (不可重入) — 锁内
    不得再调用本函数或嵌套 flock. 调用方必须一次性完成读-改-写.
    """
    state_dir = Path(state_dir)
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


def _bump_human_count(state_dir: Path, issue_num) -> None:
    """在锁内读-改-写 human_notify 计数."""
    def _inner():
        mstate = _load_monitor_state(state_dir)
        human_notify = mstate.setdefault("human_notify", {})
        now = _now()
        human_notify[str(issue_num)] = human_notify.get(str(issue_num), 0) + 1
        human_notify[str(issue_num) + ":last"] = now
        _save_monitor_state(state_dir, mstate)
        return human_notify[str(issue_num)]
    return _with_state_lock(state_dir, _inner)


def confirm_human_notify(state_dir: Path, issue_num) -> int:
    """外部投递确认: tick 脚本在飞书投递成功后调用, 递增 Human 兜底计数.

    替代旧的 confirm_delivery (假确认 — dispatcher 无法感知外部投递结果).
    返回递增后的计数.
    """
    if issue_num is None:
        return 0
    return _bump_human_count(state_dir, issue_num)


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


def run_monitor(repo: str, project: dict | None, state_dir: Path,
                notify=None, items: list | None = None,
                workdirs: list[Path] | None = None) -> dict:
    """执行一轮监控检测, 返回 {warnings, notifications}.

    notify(role, message) 回调: 记录通知事件 (role: user/worker/auditor).
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    notifications: list[dict] = []

    def emit(role, message, level=1, issue=None):
        notifications.append({"role": role, "message": message, "level": level,
                              "issue": issue})
        if notify:
            notify(role, message)

    _write_heartbeat(state_dir)

    # ── 1. 滞留 WARN + Human 超时兜底 (基于 state 里的 entered 时间) ──
    now = _now()

    # items: [{number, status}] — 来自 dispatcher 或独立查询
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
            except Exception as exc:
                warnings.append("monitor: 状态查询失败 %s" % str(exc)[:100])

    current = {str(it.get("number")): it.get("status", "Inbox")
               for it in items if it.get("number") is not None}

    # ── 状态读-改-写原子化 (codex P1: 无锁并发覆盖) ──
    # 锁内: 加载 mstate → 更新 entered → 判断滞留/Human → 写回;
    # 锁外: emit 通知 (不阻塞锁, 回调可能慢).
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
        # 计数由外部投递方 confirm_human_notify 递增 (codex P1: 防投递失败吞掉)
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
                    pending.append(("user",
                        "⛔ Human 状态超时兜底: issue #%s 已 %d 小时无人处理 "
                        "(第 %d/3 次提醒, 邮件/短信通道)" % (
                            num, int(age_h), count + 1), 3, num))

        _save_monitor_state(state_dir, mstate)
        return pending

    pending = _with_state_lock(state_dir, _judge)
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

    # 事件日志
    log = state_dir / "dispatcher_events.log"
    with log.open("a") as f:
        for n in notifications:
            f.write("%s %s level=%s %s\n" % (
                _ts(), n["role"], n.get("level", 1), n["message"][:120]))

    return {"warnings": warnings, "notifications": notifications}


def main() -> None:
    parser = argparse.ArgumentParser(description="dispatcher monitor")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", type=Path,
                        default=Path("~/.hermes/bot-dispatcher").expanduser())
    parser.add_argument("--workdirs", nargs="*", default=[])
    parser.add_argument("--confirm-issue", type=int, default=None,
                        help="外部投递确认: 飞书投递成功后递增 Human 兜底计数")
    args = parser.parse_args()

    if args.confirm_issue is not None:
        count = confirm_human_notify(args.state_dir, args.confirm_issue)
        print(json.dumps({"confirmed": args.confirm_issue,
                          "count": count}, ensure_ascii=False))
        return

    out = run_monitor(args.repo, None, args.state_dir,
                      workdirs=[Path(w) for w in args.workdirs])
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
