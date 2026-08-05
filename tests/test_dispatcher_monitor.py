"""dispatcher_monitor 测试: 滞留 WARN / Human 超时兜底 / 资源 / 心跳."""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import importlib.util

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dispatcher_monitor",
                                              ROOT / "dispatcher_monitor.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def test_heartbeat_written(tmp_path):
    MOD._write_heartbeat(tmp_path)
    hb = tmp_path / "dispatcher.heartbeat"
    assert hb.exists()
    data = json.loads(hb.read_text())
    assert "tick" in data
    assert (tmp_path / "dispatcher_events.log").exists()


def test_stale_warn_emitted(tmp_path):
    """Ready 滞留超阈值 → user WARN (token 成本提示)."""
    MOD.STALE_THRESHOLDS["Ready"] = 1  # 1 秒就超
    notif = []
    items = [{"number": 7, "status": "Ready"}]
    try:
        out = MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                              items=items)
        # 第一次调用: 记录进入时间, 未超时
        assert out["notifications"] == []
        time.sleep(1.2)
        out2 = MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                               items=items)
        roles = [r for r, _ in notif]
        assert "user" in roles, "滞留必须通知 user"
        assert any("滞留 WARN" in m for _, m in notif)
    finally:
        MOD.STALE_THRESHOLDS["Ready"] = 3 * 86400


def test_human_escape_notifies_max_3(tmp_path):
    """Human 持续 8h+ → 每小时 1 次, 上限 3 次."""
    MOD.HUMAN_ESCAPE_HOURS = 0  # 立即触发
    MOD.HUMAN_ESCAPE_MAX = 3
    notif = []
    items = [{"number": 9, "status": "Human"}]
    # 首次调用记录进入时间
    MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                    items=items)
    # 模拟 8h 过去: 改 entered 时间
    st = MOD._load_monitor_state(tmp_path)
    st["entered"]["9"]["since"] = time.time() - 9 * 3600
    MOD._save_monitor_state(tmp_path, st)
    # 第 1 次 (8h+)
    MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                    items=items)
    # 第 2 次 (1h 后) — 手动改 last
    st = MOD._load_monitor_state(tmp_path)
    st["human_notify"]["9:last"] = time.time() - 3600
    MOD._save_monitor_state(tmp_path, st)
    MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                    items=items)
    # 第 3 次
    st = MOD._load_monitor_state(tmp_path)
    st["human_notify"]["9:last"] = time.time() - 3600
    MOD._save_monitor_state(tmp_path, st)
    MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                    items=items)
    # 第 4 次 — 应停止 (上限 3)
    st = MOD._load_monitor_state(tmp_path)
    st["human_notify"]["9:last"] = time.time() - 3600
    MOD._save_monitor_state(tmp_path, st)
    MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                    items=items)
    esc = [m for _, m in notif if "Human 状态超时" in m]
    assert len(esc) == 3, "上限 3 次, 实际 %d" % len(esc)
    assert all("邮件/短信" in m for m in esc)
    MOD.HUMAN_ESCAPE_HOURS = 8


def test_disk_warn_emitted(tmp_path):
    with patch.object(MOD, "_disk_usage_pct", return_value=95.0):
        out = MOD.run_monitor("o/r", None, tmp_path, items=[])
    levels = [n.get("level") for n in out["notifications"]]
    assert 3 in levels, "磁盘 >90% 必须 level 3"


def test_zombie_warn_emitted(tmp_path):
    with patch.object(MOD, "_zombie_processes",
                      return_value=[1, 2, 3, 4, 5, 6]):
        out = MOD.run_monitor("o/r", None, tmp_path, items=[])
    msgs = [n["message"] for n in out["notifications"]]
    assert any("多实例并存" in m for m in msgs)


def test_no_notify_when_nothing_stale(tmp_path):
    MOD.STALE_THRESHOLDS["Ready"] = 999999
    notif = []
    items = [{"number": 1, "status": "Ready"}]
    MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                    items=items)
    MOD.run_monitor("o/r", None, tmp_path, notify=lambda r, m: notif.append((r, m)),
                    items=items)
    assert notif == [], "无滞留不应通知"
    MOD.STALE_THRESHOLDS["Ready"] = 3 * 86400
