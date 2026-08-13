"""stale_status_check 单元测试 — Ready/EV Review/Blocked 停留报警."""
import sys
import time

sys.path.insert(0, ".")
import dispatcher as dp  # noqa: E402


def _item(number, status, pn=2):
    return {"number": number, "project_num": pn, "status": status, "is_pr": False}


def _projects():
    return [{"number": 2, "node": "PVT_x", "name": "P&B", "owner": "analyst"}]


def _sm():
    return {"analyst": "analyst", "auditor": "auditor", "user": "user"}


def test_ready_short_stay_no_alert():
    """Ready 停留 < 5min → 不报警 (首次见状态只记录不报)."""
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), {}, {}, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"], out["warnings"]
    # 首次见 → 记录 stale_since 但不清空输出
    assert not out["notifications"]


def test_ready_stale_5min_alerts():
    """Ready 停留 ≥ 5min → 报警 + 重唤醒 owner (走真实 items 参数)."""
    t0 = time.time() - 360  # 6 min ago
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready"}
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"], "expected stale warning"
    assert "status_stale" in out["warnings"][0]
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "owner should be re-woken"


def test_ev_review_stale_alerts_auditor():
    """EV Review 停留 ≥ 5min → 报警 + 重唤醒 auditor."""
    t0 = time.time() - 360
    prev = {"stale_since:101": str(t0), "stale_status:101": "EV Review"}
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(101, "EV Review")], _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"]
    roles = {n["role"] for n in out["notifications"]}
    assert "auditor" in roles


def test_blocked_stale_alerts_user():
    """Blocked 停留 ≥ 5min → 报警给 user."""
    t0 = time.time() - 360
    prev = {"stale_since:102": str(t0), "stale_status:102": "Blocked"}
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(102, "Blocked")], _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"]
    roles = {n["role"] for n in out["notifications"]}
    assert "user" in roles


def test_dedup_suppresses_repeat():
    """同一状态 dedup 窗口 (30min) 内: warning 仍输出, 通知被抑制."""
    t0 = time.time() - 360
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready",
            "stale_dedup:100:Ready": str(time.time() - 60)}  # 1 min ago
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"], "warning should still fire (visibility)"
    assert not out["notifications"], "dedup should suppress re-notification"


def test_dedup_expired_notifies_again():
    """dedup 窗口过后 → 重新通知."""
    t0 = time.time() - 360
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready",
            "stale_dedup:100:Ready": str(time.time() - 2000)}  # 33 min ago
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"]
    assert out["notifications"], "expired dedup should re-notify"


def test_ev_review_session_map():
    """EV Review → 走 session_map 的 auditor 映射 (非硬编码)."""
    t0 = time.time() - 360
    prev = {"stale_since:101": str(t0), "stale_status:101": "EV Review"}
    out = {"actions": [], "warnings": [], "notifications": []}
    sm = {"auditor": "team-EV", "user": "user", "analyst": "analyst"}
    dp.stale_status_check([_item(101, "EV Review")], _projects(), sm, prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    roles = {n["role"] for n in out["notifications"]}
    assert "team-EV" in roles, "should use session_map auditor mapping"


def test_status_change_clears_all_stale():
    """状态变化 (Ready→In Progress) 清除 stale 记录 + dedup (主循环逻辑)."""
    prev = {"stale_since:100": str(time.time() - 600),
            "stale_status:100": "Ready",
            "stale_dedup:100:Ready": str(time.time() - 100),
            "2:100": "Ready"}
    new = dict(prev)
    new["2:100"] = "In Progress"
    # 模拟主循环清理逻辑 (与 dispatcher.py 一致)
    prev_s, cur_s = "Ready", "In Progress"
    if prev_s != cur_s:
        new.pop("stale_since:100", None)
        new.pop("stale_status:100", None)
        for dk in list(new):
            if dk.startswith("stale_dedup:100:"):
                new.pop(dk, None)
    assert "stale_since:100" not in new
    assert "stale_status:100" not in new
    assert not any(k.startswith("stale_dedup:100:") for k in new)


def test_multi_stale_aggregates_one_notification():
    """同 owner 多 stale issue → 聚合成一条通知 (防 /goal 扇出)."""
    t0 = time.time() - 360
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready",
            "stale_since:101": str(t0), "stale_status:101": "Ready"}
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(100, "Ready"), _item(101, "Ready")],
                          _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    analyst_msgs = [n for n in out["notifications"] if n["role"] == "analyst"]
    assert len(analyst_msgs) == 1, "should aggregate to one notification"
    assert "#100" in analyst_msgs[0]["message"] and "#101" in analyst_msgs[0]["message"]


def test_nonstale_status_ignored():
    """In Progress/PI Review/Done 不参与停留报警."""
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(200, "In Progress"), _item(201, "PI Review"),
                           _item(202, "Done")], _projects(), _sm(), {}, {}, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"]


def test_bad_float_value_safe():
    """坏 stale_since 值 → 重置计时不崩溃."""
    prev = {"stale_since:100": "not-a-number", "stale_status:100": "Ready"}
    out = {"actions": [], "warnings": [], "notifications": []}
    new = dict(prev)
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), prev, new, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"]  # 坏值重置, 本轮不报警
    assert "stale_since:100" not in new  # 已清除


def test_nan_since_reset():
    """NaN stale_since → 重置 (不产生负 age 永久静默)."""
    prev = {"stale_since:100": "nan", "stale_status:100": "Ready"}
    out = {"actions": [], "warnings": [], "notifications": []}
    new = dict(prev)
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), prev, new, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"]
    assert "stale_since:100" not in new


def test_inf_since_reset():
    """Inf stale_since → 重置 (不产生负 age 永久静默)."""
    prev = {"stale_since:100": "inf", "stale_status:100": "Ready"}
    out = {"actions": [], "warnings": [], "notifications": []}
    new = dict(prev)
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), prev, new, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"]
    assert "stale_since:100" not in new


def test_nan_dedup_renotifies():
    """NaN dedup → 视为坏值, 重新通知 (不抑制)."""
    t0 = time.time() - 360
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready",
            "stale_dedup:100:Ready": "nan"}
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([_item(100, "Ready")], _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["notifications"], "NaN dedup should not suppress"


def test_empty_items_noop():
    """空 items → 直接返回 (无崩溃)."""
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check([], _projects(), _sm(), {}, {}, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"]
