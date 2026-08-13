"""dispatcher_monitor C 项测试: 交付链断链检测.

场景:
- In Progress 超阈值 + open PR 且 finalize check-only 全绿 → 断链报警
- In Progress 未超阈值 → 不断链 (worker 还在开发)
- PR 校验失败 (draft / 不 Closes issue) → 不断链 (交付链未建立)
- 限频: 每 issue 最多 3 次, 间隔 1h
- _open_prs_for_issue: gh pr list --search "issue:N" 解析
"""
import json
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, ".")
import dispatcher_monitor as dm


def _ok(run_result, stdout="", returncode=0):
    r = mock.Mock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = ""
    return r


# ─── _open_prs_for_issue ─────────────────────────────────────────────────

def test_open_prs_parses_gh_output():
    out = json.dumps([{"number": 42, "isDraft": False,
                       "headRefOid": "a" * 40, "updatedAt": "2026-08-13"}])
    with mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, out)):
        prs, err = dm._open_prs_for_issue("beijing-lot", 7)
    assert err is None
    assert len(prs) == 1 and prs[0]["number"] == 42


def test_open_prs_accepts_string_issue_num():
    """issue_num 可能是字符串 (run_monitor 的 current key) — 不得 TypeError."""
    out = json.dumps([{"number": 42, "isDraft": False,
                       "headRefOid": "a" * 40, "updatedAt": "2026-08-13"}])
    seen = {}
    def fake_run(cmd, *a, **k):
        seen["search"] = [x for x in cmd if str(x).startswith("issue:")]
        return _ok(None, out)
    with mock.patch.object(dm.subprocess, "run", fake_run):
        prs, err = dm._open_prs_for_issue("beijing-lot", "7")
    assert err is None
    assert len(prs) == 1
    assert seen["search"] == ["issue:7"]


def test_open_prs_empty_on_failure():
    with mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, "", returncode=1)):
        prs, err = dm._open_prs_for_issue("beijing-lot", 7)
    assert prs == [] and err is not None  # 失败必须可观测 (err 非 None)


def test_open_prs_bad_json_empty():
    with mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, "not json")):
        prs, err = dm._open_prs_for_issue("beijing-lot", 7)
    assert prs == [] and err is not None


# ─── _check_delivery_chain ───────────────────────────────────────────────

def test_chain_broken_when_check_only_green():
    """finalize check-only 全绿 (交付链完整) → broken=True."""
    def fake_finalize(repo, issue, pr, expected_head="", check_only=False,
                      verbose=True):
        return (True, [{"step": "pr_ready", "ok": True},
                       {"step": "pr_closes_issue", "ok": True},
                       {"step": "head_match", "ok": True},
                       {"step": "issue_in_progress", "ok": True},
                       {"step": "transition_allowed", "ok": True},
                       {"step": "write_skipped", "ok": True}], None)
    fd = mock.Mock()
    fd.finalize = fake_finalize
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}):
        res = dm._check_delivery_chain("beijing-lot", 7, {"number": 42,
                                                          "headRefOid": "a" * 40})
    assert res["broken"] is True
    assert "PR #42" in res["detail"]


def test_chain_not_broken_when_check_fails():
    """PR draft → check-only 失败 → 不断链 (交付链未建立)."""
    def fake_finalize(repo, issue, pr, expected_head="", check_only=False,
                      verbose=True):
        return (False, [{"step": "pr_ready", "ok": False,
                         "detail": "state=OPEN draft=True"}], None)
    fd = mock.Mock()
    fd.finalize = fake_finalize
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}):
        res = dm._check_delivery_chain("beijing-lot", 7, {"number": 42,
                                                          "headRefOid": "a" * 40})
    assert res["broken"] is False
    assert "pr_ready" in res["detail"]


def test_chain_exception_not_broken():
    def fake_finalize(*a, **k):
        raise RuntimeError("boom")
    fd = mock.Mock()
    fd.finalize = fake_finalize
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}):
        res = dm._check_delivery_chain("beijing-lot", 7, {"number": 42})
    assert res["broken"] is False
    assert "异常" in res["detail"]


# ─── run_monitor 断链集成 ────────────────────────────────────────────────

def _monitor_state_dir(tmp, entered=None, chain=None):
    d = Path(tmp)
    d.mkdir(parents=True, exist_ok=True)
    st = {"entered": entered or {}, "human_notify": {},
          "chain_notify": chain or {}}
    (d / dm.MONITOR_STATE).write_text(json.dumps(st))
    return d


def _fake_finalize_green(*a, **k):
    return (True, [{"step": "pr_ready", "ok": True},
                   {"step": "pr_closes_issue", "ok": True},
                   {"step": "head_match", "ok": True},
                   {"step": "issue_in_progress", "ok": True},
                   {"step": "transition_allowed", "ok": True},
                   {"step": "write_skipped", "ok": True}], None)


def test_run_monitor_chain_break_detected(tmp_path):
    """In Progress 超阈值 + PR check-only 全绿 → 断链通知."""
    old = time.time() - 7200  # 2h 前进入 In Progress (阈值 1h)
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40, "updatedAt": "x"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green

    real_run = dm.subprocess.run
    def fake_run(cmd, *a, **k):
        if cmd[:3] == ["gh", "pr", "list"]:
            return _ok(None, prs_out)
        return real_run(cmd, *a, **k)

    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run", fake_run):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    msgs = [n["message"] for n in out["notifications"]]
    assert any("交付链断链" in m and "PR #42" in m for m in msgs)


def test_run_monitor_chain_no_break_before_threshold(tmp_path):
    """In Progress 未超阈值 → 不断链 (worker 还在开发)."""
    old = time.time() - 600  # 10 分钟前 (阈值 1h)
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40, "updatedAt": "x"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    msgs = [n["message"] for n in out["notifications"]]
    assert not any("交付链断链" in m for m in msgs)


def test_run_monitor_chain_rate_limited(tmp_path):
    """限频: 同 issue 最多 3 次, 间隔 1h."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}},
                            chain={"7": 3, "7:last": time.time()})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40, "updatedAt": "x"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    msgs = [n["message"] for n in out["notifications"]]
    assert not any("交付链断链" in m for m in msgs)  # 已 3 次 → 不再提醒


def test_run_monitor_draft_pr_no_break(tmp_path):
    """PR 是 draft → check-only 失败 → 不断链."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": True,
                           "headRefOid": "a" * 40, "updatedAt": "x"}])
    fd = mock.Mock()
    fd.finalize = lambda *a, **k: (False, [{"step": "pr_ready", "ok": False}], None)
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    msgs = [n["message"] for n in out["notifications"]]
    assert not any("交付链断链" in m for m in msgs)


def test_check_delivery_chain_passes_check_only_and_head(tmp_path=None):
    """_check_delivery_chain 必须传 check_only=True + expected_head=headRefOid."""
    seen = {}
    def fake_finalize(repo, issue, pr, expected_head="", check_only=False,
                      verbose=True):
        seen["expected_head"] = expected_head
        seen["check_only"] = check_only
        return (True, [{"step": "pr_ready", "ok": True}], None)
    fd = mock.Mock()
    fd.finalize = fake_finalize
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}):
        res = dm._check_delivery_chain("beijing-lot", 7, {"number": 42,
                                                          "headRefOid": "b" * 40})
    assert res["broken"] is True
    assert seen["check_only"] is True
    assert seen["expected_head"] == "b" * 40


def test_run_monitor_chain_notify_persisted(tmp_path):
    """首次断链报警后 chain_notify 状态持久化 (限频依赖)."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40,
                           "updatedAt": "2000-01-01T00:00:00Z"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    assert any("交付链断链" in n["message"] for n in out["notifications"])
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") == 1
    assert state["chain_notify"].get("7:last", 0) > 0


def test_run_monitor_chain_repeat_after_1h(tmp_path):
    """>1h 后再次报警 (计数递增到 2)."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}},
                            chain={"7": 1, "7:last": time.time() - 7200})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40,
                           "updatedAt": "2000-01-01T00:00:00Z"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    msgs = [n["message"] for n in out["notifications"]]
    assert any("交付链断链" in m for m in msgs)  # 间隔已 >1h → 再报
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") == 2


def test_run_monitor_chain_suppressed_within_1h(tmp_path):
    """<1h 抑制: 上次报警 10 分钟前 → 不再报."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}},
                            chain={"7": 1, "7:last": time.time() - 600})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40,
                           "updatedAt": "2000-01-01T00:00:00Z"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    msgs = [n["message"] for n in out["notifications"]]
    assert not any("交付链断链" in m for m in msgs)  # <1h → 抑制


def test_run_monitor_chain_grace_window(tmp_path):
    """grace window: PR 刚更新 (<15min) → 不报断链 (worker 可能还没跑 finalize)."""
    old = time.time() - 7200
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 300))
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40, "updatedAt": recent}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    msgs = [n["message"] for n in out["notifications"]]
    assert not any("交付链断链" in m for m in msgs)  # grace window → 不报


def test_pr_updated_recently_boundaries():
    """grace window 边界: 899s=True / 900s=False / 901s=False."""
    now = time.time()
    def pr_at(age):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age))
        return {"updatedAt": ts}
    assert dm._pr_updated_recently(pr_at(899), now) is True
    assert dm._pr_updated_recently(pr_at(900), now) is False
    assert dm._pr_updated_recently(pr_at(901), now) is False


def test_pr_updated_recently_future_time_recent():
    """未来时间戳 (时钟偏差) → 视为 recent (不报, 防误报)."""
    now = time.time()
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600))
    assert dm._pr_updated_recently({"updatedAt": future}, now) is True


def test_pr_updated_recently_bad_format_not_recent():
    """解析失败 → 不算 recent (允许检测继续, 不因格式问题漏报)."""
    assert dm._pr_updated_recently({"updatedAt": "not-a-date"}, time.time()) is False
    assert dm._pr_updated_recently({}, time.time()) is False


def test_run_monitor_chain_reset_on_status_change(tmp_path):
    """issue 离开 In Progress → chain_notify 计数清空 (下次断链重新计)."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}},
                            chain={"7": 3, "7:last": time.time()})
    # 状态已变: issue 7 现在 EV Review
    items = [{"number": 7, "status": "EV Review"}]
    import importlib
    importlib.reload(dm)
    out = dm.run_monitor("beijing-lot", None, sd, items=items)
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") is None  # 计数已清


def test_run_monitor_chain_failure_observable(tmp_path):
    """finalize 异常 → 写入 warnings (监控失明可观测), 不静默."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40,
                           "updatedAt": "2000-01-01T00:00:00Z"}])
    fd = mock.Mock()
    fd.finalize = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    assert any("断链检测异常" in w for w in out["warnings"])
    assert not any("交付链断链" in n["message"] for n in out["notifications"])


def test_run_monitor_chain_pr_read_fail_observable(tmp_path):
    """finalize 读取类步骤失败 (pr_read) → error 非 None → warnings (非静默)."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40,
                           "updatedAt": "2000-01-01T00:00:00Z"}])
    fd = mock.Mock()
    fd.finalize = lambda *a, **k: (False, [{"step": "pr_read", "ok": False,
                                            "detail": "graphql error"}], None)
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    assert any("断链检测异常" in w for w in out["warnings"])
    assert not any("交付链断链" in n["message"] for n in out["notifications"])


def test_run_monitor_chain_pr_query_fail_observable(tmp_path):
    """gh pr list 查询失败 → warnings (可观测), 不静默."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    import importlib
    importlib.reload(dm)
    with mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, "", returncode=1)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items)
    assert any("PR 查询失败" in w for w in out["warnings"])
    assert not any("交付链断链" in n["message"] for n in out["notifications"])


def test_run_monitor_dry_run_no_state_write(tmp_path):
    """dry_run: 检测照跑, 但 chain_notify/heartbeat/事件日志都不写盘."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40,
                           "updatedAt": "2000-01-01T00:00:00Z"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    # 记录 dry_run 前的文件集合
    before = set(p.name for p in sd.iterdir())
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items,
                             dry_run=True)
    assert any("交付链断链" in n["message"] for n in out["notifications"])
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") is None  # dry-run 不写计数
    after = set(p.name for p in sd.iterdir())
    assert "dispatcher.heartbeat" not in after  # 不写心跳
    assert "dispatcher_events.log" not in after  # 不写事件日志
    # 允许新增 .lock (临时文件, 非状态)


def test_run_monitor_items_query_fail_no_reset(tmp_path):
    """items 查询失败 → 不 reset chain_notify (计数保留, 防恢复后漏报)."""
    sd = _monitor_state_dir(tmp_path,
                            chain={"7": 2, "7:last": time.time() - 7200})
    # 独立查询 (items=None) + project 存在 + gh api 失败 → items_ok=False
    project = {"node": "PVT_123"}
    import importlib
    importlib.reload(dm)
    with mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, "", returncode=1)):
        out = dm.run_monitor("beijing-lot", project, sd)
    assert any("状态查询失败" in w for w in out["warnings"])
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") == 2  # 计数未被清


def test_chain_notify_acquire_concurrent_second_blocked(tmp_path):
    """并发双 tick: 第一个 acquire 记账成功, 第二个被限频拦截 (无双发)."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    import importlib
    importlib.reload(dm)
    now = time.time()
    # tick A: 锁内 acquire + emit (模拟 run_monitor 同锁回调)
    a_ok = dm._with_state_lock(
        sd, lambda: dm._chain_notify_acquire(sd, "7", now,
                                             expected_since=old))
    # tick B: 同一时刻第二个 tick — 限频拦截
    b_ok = dm._with_state_lock(
        sd, lambda: dm._chain_notify_acquire(sd, "7", now,
                                             expected_since=old))
    assert a_ok is True and b_ok is False  # B 被拦截, 无双发
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") == 1  # 账面计数 1


def test_chain_notify_acquire_resets_after_24h(tmp_path):
    """限频时间衰减: 首次提醒超 24h → 计数重置 (防永久静默)."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}},
                            chain={"7": 3,  # 已耗尽
                                   "7:last": time.time() - 7200,
                                   "7:first": time.time() - 25 * 3600})  # 25h 前
    import importlib
    importlib.reload(dm)
    notified = dm._with_state_lock(
        sd, lambda: dm._chain_notify_acquire(sd, "7", time.time(),
                                             expected_since=old))
    assert notified is True  # 24h 后重置 → 重新计
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") == 1  # 重置后计 1


def test_chain_notify_acquire_within_24h_still_blocked(tmp_path):
    """限频时间衰减: 未超 24h → 计数不重置 (仍拦截)."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}},
                            chain={"7": 3,
                                   "7:last": time.time() - 7200,
                                   "7:first": time.time() - 2 * 3600})  # 2h 前
    import importlib
    importlib.reload(dm)
    notified = dm._with_state_lock(
        sd, lambda: dm._chain_notify_acquire(sd, "7", time.time(),
                                             expected_since=old))
    assert notified is False  # 未超 24h → 仍拦截


def test_run_monitor_dry_run_empty_dir_no_side_effects(tmp_path):
    """全新目录 dry_run: 零磁盘副作用 (不创建目录/文件)."""
    sd = Path(tmp_path) / "fresh"
    items = [{"number": 7, "status": "In Progress"}]
    prs_out = json.dumps([{"number": 42, "isDraft": False,
                           "headRefOid": "a" * 40,
                           "updatedAt": "2000-01-01T00:00:00Z"}])
    fd = mock.Mock()
    fd.finalize = _fake_finalize_green
    import importlib
    importlib.reload(dm)
    with mock.patch.dict(sys.modules, {"finalize_delivery": fd}), \
         mock.patch.object(dm.subprocess, "run",
                           lambda *a, **k: _ok(None, prs_out)):
        out = dm.run_monitor("beijing-lot", None, sd, items=items,
                             dry_run=True)
    assert not sd.exists()  # 目录都没创建 → 零副作用


def test_open_prs_accepts_owner_name_repo():
    """repo 参数为完整 owner/name (配置契约) — gh CLI 原样接受, 不重写 owner."""
    out = json.dumps([{"number": 42, "isDraft": False,
                       "headRefOid": "a" * 40, "updatedAt": "x"}])
    seen = {}
    def fake_run(cmd, *a, **k):
        seen["repo"] = cmd[cmd.index("--repo") + 1]
        return _ok(None, out)
    with mock.patch.object(dm.subprocess, "run", fake_run):
        prs, err = dm._open_prs_for_issue("example-org/sample-research", 7)
    assert err is None and len(prs) == 1
    assert seen["repo"] == "example-org/sample-research"  # owner 保留原样


def test_chain_notify_acquire_epoch_stale_tick_no_notify(tmp_path):
    """epoch 校验: 断链判断后状态已离开 In Progress → 旧 tick 不通知."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    # 模拟 tick A 判断断链时 since=old; 随后状态变 EV Review (reset 已清)
    mstate = json.loads((sd / dm.MONITOR_STATE).read_text())
    mstate["entered"]["7"] = {"status": "EV Review", "since": time.time()}
    mstate["chain_notify"] = {}
    (sd / dm.MONITOR_STATE).write_text(json.dumps(mstate))
    import importlib
    importlib.reload(dm)
    notified = dm._with_state_lock(
        sd, lambda: dm._chain_notify_acquire(sd, "7", time.time(),
                                             expected_since=old))
    assert notified is False  # epoch 不符 → 不通知
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") is None  # 计数未重建


def test_chain_notify_acquire_dry_run_no_write(tmp_path):
    """_chain_notify_acquire dry_run=True: 判断通过但不写计数."""
    old = time.time() - 7200
    sd = _monitor_state_dir(tmp_path, entered={"7": {"status": "In Progress",
                                                     "since": old}})
    import importlib
    importlib.reload(dm)
    notified = dm._with_state_lock(
        sd, lambda: dm._chain_notify_acquire(sd, "7", time.time(),
                                             expected_since=old, dry_run=True),
        dry_run=True)
    assert notified is True
    state = json.loads((sd / dm.MONITOR_STATE).read_text())
    assert state["chain_notify"].get("7") is None  # dry-run 不写
