"""Ready 依赖门禁测试 — 状态机级 (mock GraphQL, 走真实 build_issue_proj_map)."""
import sys
import time
from unittest import mock

sys.path.insert(0, ".")
import dispatcher as dp  # noqa: E402


def _item(number, status, blocked_count=0, blocked_by=None, pn=2):
    return {
        "number": number, "status": status, "project_num": pn,
        "project_name": "P%d" % pn, "_item_id": "i%d" % number,
        "is_pr": False, "blocked_by_count": blocked_count,
        "blocked_by": blocked_by or [],
    }


def _projects():
    return [{"number": 2, "owner": "analyst", "name": "P2"}]


def _mkout():
    return {"actions": [], "warnings": [], "notifications": []}


def _mock_map(items):
    """mock build_issue_proj_map → 直接返回 items."""
    patcher = mock.patch.object(dp, "build_issue_proj_map",
                                return_value=({}, items))
    patcher.start()
    return patcher


def _run_tick(items, prev_state, projects=None, sm=None):
    """走真实 run_status_loop (title_fetcher 注入, 无 GitHub 依赖)."""
    projects = projects or _projects()
    sm = sm or {"analyst": "analyst", "auditor": "auditor", "user": "user"}
    out = _mkout()
    new_state = dict(prev_state)
    dp.run_status_loop(items, projects, sm, prev_state, new_state, out,
                       dry_run=True, repo="test",
                       title_fetcher=lambda n, is_pr: "Issue %d" % n)
    return out, new_state


def test_ready_no_dep_dispatches():
    """Ready 无依赖 → 派发 owner + 无 dependency warning."""
    out, _ = _run_tick([_item(100, "Ready")], {})
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles
    assert not any("dependency" in w for w in out["warnings"])


def test_ready_dep_open_defers():
    """Ready + blockedBy 开放 → 不派发 + dependency warning + 重置 sent_ready."""
    out, new_state = _run_tick([_item(100, "Ready", blocked_count=1, blocked_by=[208])], {})
    assert not out["notifications"], "should NOT dispatch"
    assert any("dependency" in w for w in out["warnings"])
    assert not any(k.startswith("sent_ready:") for k in new_state), "sent_ready reset"


def test_ready_dep_release_dispatches():
    """等待中 (sent_ready 已记录) → 依赖解除 → 自然触发派发."""
    sk = dp.project_state_key(2, 100)
    prev = {sk: "Ready", "sent_ready:" + sk: "d1"}  # 之前依赖数=1 已通知过? 不 — 等待中无 sent
    # 等待中: sent_ready 被清 → 依赖解除后无快照 → 触发
    out, new_state = _run_tick([_item(100, "Ready", blocked_count=0)], {sk: "Ready"})
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "should dispatch on dep release"
    assert new_state.get("sent_ready:" + sk) == "d0", "sent_ready recorded"


def test_ready_dep_wait_reevaluates_each_tick():
    """等待中每 tick 重评估 (prev==cur 也检查) — 不 continue."""
    prev = {"2:100": "Ready"}
    out, _ = _run_tick([_item(100, "Ready", blocked_count=2)], prev)
    assert not out["notifications"], "still waiting"


def test_ready_dedup_no_resend():
    """同状态 (Ready, 无依赖) 已通知过 → 不重复唤醒 (sent_ready 去重)."""
    sk = dp.project_state_key(2, 100)
    prev = {sk: "Ready", "sent_ready:" + sk: "d0"}
    out, _ = _run_tick([_item(100, "Ready", blocked_count=0)], prev)
    assert not out["notifications"], "should NOT re-dispatch same state"


def test_stale_skips_dep_wait_ready():
    """stale checker 跳过依赖等待的 Ready (不报 unclaimed)."""
    t0 = time.time() - 600  # 10 min stale
    prev = {"stale_since:100": str(t0), "stale_status:100": "Ready"}
    items = [_item(100, "Ready", blocked_count=1, blocked_by=[208])]
    out = _mkout()
    sm = {"analyst": "analyst", "user": "user"}
    dp.stale_status_check(items, _projects(), sm, prev, {}, out, now=time.time())
    assert not any("status_stale" in str(w) for w in out["warnings"])
    assert not out["notifications"]
