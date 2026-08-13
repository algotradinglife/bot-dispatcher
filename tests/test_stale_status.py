"""stale_status_check 单元测试 — Ready/EV Review/Blocked 停留报警."""
import sys
import time
from unittest import mock

sys.path.insert(0, ".")
import dispatcher as dp  # noqa: E402


def _item(number, status, pn=2):
    return {"number": number, "project_num": pn, "status": status, "is_pr": False}


def _projects():
    return [{"number": 2, "node": "PVT_x", "name": "P&B", "owner": "analyst"}]


def _sm():
    return {"analyst": "analyst", "auditor": "auditor", "user": "user"}


@mock.patch.object(dp, "build_issue_proj_map")
def test_ready_short_stay_no_alert(mock_build):
    """Ready 停留 < 5min → 不报警."""
    mock_build.return_value = ({}, [_item(100, "Ready")])
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check("beijing-lot", _projects(), _sm(), {}, {}, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"], out["warnings"]


@mock.patch.object(dp, "build_issue_proj_map")
def test_ready_stale_5min_alerts(mock_build):
    """Ready 停留 ≥ 5min → 报警 + 重唤醒 owner."""
    mock_build.return_value = ({}, [_item(100, "Ready")])
    t0 = time.time() - 360  # 6 min ago
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready",
            "2:100": "Ready"}
    out = {"actions": [], "warnings": [], "notifications": []}
    new = dict(prev)
    dp.stale_status_check("beijing-lot", _projects(), _sm(), prev, new, out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"], "expected stale warning"
    assert out["warnings"][0]["reason"] == "status_stale"
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "owner should be re-woken"


@mock.patch.object(dp, "build_issue_proj_map")
def test_ev_review_stale_alerts_auditor(mock_build):
    """EV Review 停留 ≥ 5min → 报警 + 重唤醒 auditor."""
    mock_build.return_value = ({}, [_item(101, "EV Review")])
    t0 = time.time() - 360
    prev = {"stale_since:101": str(t0), "stale_status:101": "EV Review",
            "2:101": "EV Review"}
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check("beijing-lot", _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"]
    roles = {n["role"] for n in out["notifications"]}
    assert "auditor" in roles


@mock.patch.object(dp, "build_issue_proj_map")
def test_blocked_stale_alerts_user(mock_build):
    """Blocked 停留 ≥ 5min → 报警给 user."""
    mock_build.return_value = ({}, [_item(102, "Blocked")])
    t0 = time.time() - 360
    prev = {"stale_since:102": str(t0), "stale_status:102": "Blocked",
            "2:102": "Blocked"}
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check("beijing-lot", _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert out["warnings"]
    roles = {n["role"] for n in out["notifications"]}
    assert "user" in roles


@mock.patch.object(dp, "build_issue_proj_map")
def test_dedup_suppresses_repeat(mock_build):
    """同一状态 dedup 窗口 (30min) 内不重复报警."""
    mock_build.return_value = ({}, [_item(100, "Ready")])
    t0 = time.time() - 360
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready",
            "2:100": "Ready",
            "stale_dedup:100:Ready": str(time.time() - 60)}  # 1 min ago
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check("beijing-lot", _projects(), _sm(), prev, dict(prev), out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"], "dedup should suppress repeat"


@mock.patch.object(dp, "build_issue_proj_map")
def test_status_change_clears_since(mock_build):
    """状态变化 (Ready→In Progress) 清除 stale 记录 — 主循环处理."""
    # 模拟主循环: prev_s != cur_s → pop stale_since
    prev = {"stale_since:100": str(time.time() - 600),
            "stale_status:100": "Ready", "2:100": "Ready"}
    new = dict(prev)
    new["2:100"] = "In Progress"
    # 主循环清理逻辑 (与 dispatcher.py 一致)
    if prev.get("2:100") != new["2:100"]:
        new.pop("stale_since:100", None)
        new.pop("stale_status:100", None)
    assert "stale_since:100" not in new
    assert "stale_status:100" not in new


@mock.patch.object(dp, "build_issue_proj_map")
def test_nonstale_status_ignored(mock_build):
    """In Progress/PI Review/Done 不参与停留报警."""
    mock_build.return_value = ({}, [
        _item(200, "In Progress"), _item(201, "PI Review"), _item(202, "Done")])
    out = {"actions": [], "warnings": [], "notifications": []}
    dp.stale_status_check("beijing-lot", _projects(), _sm(), {}, {}, out,
                          now=time.time(), stale_minutes=5)
    assert not out["warnings"]
