"""dispatcher.g05_reconcile 单元测试 (分页全量 + 游标轮转 + 去重)."""
import json
import sys
from unittest import mock

sys.path.insert(0, ".")
import dispatcher  # noqa: E402


class FakeResult:
    def __init__(self, stdout, code=0):
        self.stdout = stdout
        self.returncode = code


def _mk_prs(n):
    return [{"number": i, "mergedAt": "2026-08-12T00:%02d:00Z" % (i % 60)}
            for i in range(1, n + 1)]


def test_g05_pagination_all_merged():
    """分页获取全部 merged PR (250 个 → 3 页, 不再只扫 15 个)."""
    prs = _mk_prs(250)
    pages = [prs[i:i + 100] for i in (0, 100, 200)]
    page_requests = []

    def fake_run(cmd, capture_output=True, text=True, timeout=20):
        if cmd[1:3] == ["pr", "list"]:
            page_requests.append(int(cmd[cmd.index("--page") + 1]))
            idx = int(cmd[cmd.index("--page") + 1]) - 1
            return FakeResult(json.dumps(pages[idx]))
        if cmd[1:3] == ["pr", "view"]:
            return FakeResult("")  # 无 closing issues
        return FakeResult("")

    with mock.patch.object(dispatcher.subprocess, "run", fake_run), \
         mock.patch.object(dispatcher, "check_g05_only", return_value=("PASS", "")) \
         if hasattr(dispatcher, "check_g05_only") else mock.patch.object(dispatcher, "g05_reconcile", lambda *a, **k: None):
        pass  # 用真实 g05_reconcile

    # 直接调 g05_reconcile (mock subprocess + check_g05_only)
    with mock.patch.object(dispatcher.subprocess, "run", fake_run), \
         mock.patch("pi_gates.check_g05_only", return_value=("PASS", "")):
        out = {"warnings": []}
        ns = {}
        dispatcher.g05_reconcile("testrepo", {}, ns, "[t]", out, batch=30)
    assert page_requests == [1, 2, 3], "应请求 3 页, got %s" % page_requests
    assert "g05_cursor:testrepo" in ns
    assert ns["g05_cursor:testrepo"] == "30"  # 第一批 30 个


def test_g05_cursor_rotation():
    """游标轮转: 从上次位置继续; 一轮完成重置."""
    prs = _mk_prs(50)
    prs_json = json.dumps(prs)

    def fake_run(cmd, capture_output=True, text=True, timeout=20):
        if cmd[1:3] == ["pr", "list"]:
            return FakeResult(prs_json)
        if cmd[1:3] == ["pr", "view"]:
            return FakeResult("")
        return FakeResult("")

    with mock.patch.object(dispatcher.subprocess, "run", fake_run), \
         mock.patch("pi_gates.check_g05_only", return_value=("PASS", "")):
        # 第一次: cursor=0 → 30
        out = {"warnings": []}
        ns = {}
        dispatcher.g05_reconcile("r", {}, ns, "[t]", out, batch=30)
        assert ns["g05_cursor:r"] == "30"
        # 第二次: cursor=30 → 60 (但只有 50, 取 30-50=20 个)
        out2 = {"warnings": []}
        ns2 = {}
        dispatcher.g05_reconcile("r", ns, ns2, "[t]", out2, batch=30)
        assert ns2["g05_cursor:r"] == "60"
        # 第三次: cursor=60 >= 50 → 重置 0 → 0-30
        out3 = {"warnings": []}
        ns3 = {}
        dispatcher.g05_reconcile("r", ns2, ns3, "[t]", out3, batch=30)
        assert ns3["g05_cursor:r"] == "30"


def test_g05_warning_dedup():
    """同一 PR 的同一缺失项 24h 内只报警一次."""
    prs = _mk_prs(3)
    prs_json = json.dumps(prs)

    def fake_run(cmd, capture_output=True, text=True, timeout=20):
        if cmd[1:3] == ["pr", "list"]:
            return FakeResult(prs_json)
        if cmd[1:3] == ["pr", "view"]:
            return FakeResult("1\n2\n")  # PR #1/#2/#3 都 close issue 1,2
        return FakeResult("")

    def fake_g05(repo, issue):
        return ("REMIND", "issue not done")

    with mock.patch.object(dispatcher.subprocess, "run", fake_run), \
         mock.patch("pi_gates.check_g05_only", side_effect=fake_g05):
        # 第一次: 全部报警
        out = {"warnings": []}
        ns = {}
        dispatcher.g05_reconcile("r", {}, ns, "[t]", out, batch=3)
        assert len(out["warnings"]) == 6  # 3 PR × 2 issues
        # 第二次 (同 tick 内 state 已记): 全部去重
        out2 = {"warnings": []}
        ns2 = {}
        dispatcher.g05_reconcile("r", ns, ns2, "[t]", out2, batch=3)
        assert len(out2["warnings"]) == 0
