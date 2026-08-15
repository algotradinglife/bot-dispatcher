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
    """Ready + blockedBy 开放 → 通知一次 (告知等待) + 不派发 worker."""
    sk = dp.project_state_key(2, 100)
    out, new_state = _run_tick([_item(100, "Ready", blocked_count=1, blocked_by=[208])], {})
    # 通知一次给 owner (告知等待)
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "should notify owner once (dep-wait)"
    assert new_state.get("sent_ready:" + sk) == "d1", "sent_ready recorded"
    # 第二次 tick → 静默 (dedup)
    out2, _ = _run_tick([_item(100, "Ready", blocked_count=1, blocked_by=[208])], dict(new_state))
    assert not out2["notifications"], "should be silent after first notify"


def test_ready_dep_release_dispatches():
    """等待中 (sent_ready=d1) → 依赖解除 → 自然触发派发."""
    sk = dp.project_state_key(2, 100)
    # 等待时快照 d1 → 依赖解除后 count=0 → 快照 d1≠d0 → 触发
    prev = {sk: "Ready", "sent_ready:" + sk: "d1"}
    out, new_state = _run_tick([_item(100, "Ready", blocked_count=0)], prev)
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "should dispatch on dep release"
    assert new_state.get("sent_ready:" + sk) == "d0", "sent_ready updated to d0"


def test_ready_dep_wait_reevaluates_each_tick():
    """等待中: 首次通知一次 (告知等待), 之后静默 (dedup)."""
    sk = dp.project_state_key(2, 100)
    prev = {sk: "Ready"}
    out, new_state = _run_tick([_item(100, "Ready", blocked_count=2)], prev)
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "should notify once (dep-wait)"
    assert new_state.get("sent_ready:" + sk) == "d2", "sent_ready recorded"
    # 第二 tick → 静默
    out2, _ = _run_tick([_item(100, "Ready", blocked_count=2)], dict(new_state))
    assert not out2["notifications"], "silent after first notify"


def test_ready_dep_rework_dispatches():
    """EV REJECT 返工重入 (非停留, prev=EV Review) + 依赖开放 → 派发 (不依赖门禁).

    契约: 返工是修复缺陷, 不依赖上游 — 进入 Ready 那一下永远派发.
    """
    sk = dp.project_state_key(2, 100)
    prev = {sk: "EV Review"}  # 从 EV Review 拨回 Ready (返工重入)
    out, _ = _run_tick([_item(100, "Ready", blocked_count=1, blocked_by=[208])], prev)
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "rework re-entry should dispatch despite open dep"


def test_ready_dedup_no_resend():
    """同状态 (Ready, 无依赖) 已通知过 → 不重复唤醒 (sent_ready 去重)."""
    sk = dp.project_state_key(2, 100)
    prev = {sk: "Ready", "sent_ready:" + sk: "d0"}
    out, _ = _run_tick([_item(100, "Ready", blocked_count=0)], prev)
    assert not out["notifications"], "should NOT re-dispatch same state"


def test_ready_reentry_redispatches():
    """Ready → In Progress → Ready 第二次要能唤醒 (sent_* 离开状态清除)."""
    sk = dp.project_state_key(2, 100)
    # 模拟 tick3: 状态已是 Ready, sent_ready 已被 tick2 (In Progress) 清除
    prev = {sk: "Ready"}
    out, _ = _run_tick([_item(100, "Ready", blocked_count=0)], prev)
    roles = {n["role"] for n in out["notifications"]}
    assert "analyst" in roles, "re-entry Ready should re-dispatch"


def test_cross_tick_no_double_notify():
    """真实跨 tick 链: tick1 输出 new_state 成为 tick2 prev → 不双通知.

    复现 codex P1-A: 状态变化 tick 清 sent 后又写 → 下 tick 又触发.
    """
    sk = dp.project_state_key(2, 100)
    # tick1: In Progress → Ready (状态变化, 触发通知 + 写 sent)
    out1, ns1 = _run_tick([_item(100, "Ready", blocked_count=0)], {sk: "In Progress"})
    assert len([n for n in out1["notifications"] if n["role"] == "analyst"]) == 1
    assert ns1.get("sent_ready:" + sk) == "d0", "sent written in tick1"
    # tick2: 用 tick1 的 new_state 作为 prev → 不重复通知
    out2, ns2 = _run_tick([_item(100, "Ready", blocked_count=0)], dict(ns1))
    assert not out2["notifications"], "tick2 should NOT re-notify"
    # tick3: 转 In Progress (清 sent) → tick4 再 Ready → 重新通知 (重入)
    out3, ns3 = _run_tick([_item(100, "In Progress")], dict(ns2))
    out4, _ = _run_tick([_item(100, "Ready", blocked_count=0)], dict(ns3))
    assert len([n for n in out4["notifications"] if n["role"] == "analyst"]) == 1


def test_ev_reentry_redispatches():
    """EV Review → Ready → EV Review 第二次要能唤醒 auditor."""
    sk = dp.project_state_key(2, 100)
    # 模拟重入: 状态是 EV Review, sent_ev 已被中间状态清除
    prev = {sk: "EV Review"}
    out, _ = _run_tick([_item(100, "EV Review")], prev)
    roles = {n["role"] for n in out["notifications"]}
    assert "auditor" in roles, "re-entry EV Review should re-wake auditor"


def test_ev_dedup_same_state():
    """EV Review 已通知过 (同状态) → 不重复唤醒."""
    sk = dp.project_state_key(2, 100)
    prev = {sk: "EV Review", "sent_ev:" + sk: "1"}
    out, _ = _run_tick([_item(100, "EV Review")], prev)
    assert not out["notifications"], "should NOT re-wake same EV Review state"


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
