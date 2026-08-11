"""gate_check 单元测试 — mock gh CLI."""
import json
import sys
from unittest import mock

import pytest

sys.path.insert(0, ".")
import gate_check  # noqa: E402


class FakeGh:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, timeout=30):
        self.calls.append(list(args))
        idx = len(self.calls) - 1
        if idx < len(self.responses):
            r = mock.Mock()
            if self.responses[idx] is None:
                r.returncode = 1
                r.stdout = ""
            else:
                r.returncode = 0
                r.stdout = self.responses[idx]
            r.stderr = ""
            return r
        r = mock.Mock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "no fake"
        return r


def test_gate_for_pr_no_closing():
    """无 closing issue → failure (治理契约, 不显示绿)."""
    pr = json.dumps({"headRefOid": "a" * 40, "title": "docs: x",
                     "closingIssuesReferences": [], "state": "OPEN"})
    fake = FakeGh([pr])
    with mock.patch.object(gate_check, "_gh", fake):
        c, t, s, head = gate_check.gate_for_pr("r", 5)
    assert c == "failure"
    assert "no closing issue" in t
    assert head == "a" * 40


def test_gate_for_pr_closing_fail():
    """有 closing issue 但 gate FAIL → failure."""
    pr = json.dumps({"headRefOid": "a" * 40, "title": "feat: x",
                     "closingIssuesReferences": [{"number": 1}],
                     "state": "OPEN"})
    # gate_for_pr 的 pr view + check_pi_gates 内部多个 gh 调用
    fake = FakeGh([pr, None, None, None, None, None, None, None, None])
    with mock.patch.object(gate_check, "_gh", fake):
        # 同时 mock pi_gates._gh (check_pi_gates 内部用)
        import pi_gates
        with mock.patch.object(pi_gates, "_gh", FakeGh([None] * 20)):
            c, t, s, head = gate_check.gate_for_pr("r", 5)
    assert c == "failure"


def test_publish_status():
    def fake_run(args, input=None, capture_output=True, text=True, timeout=30):
        joined = " ".join(args)
        assert "statuses/" in joined and "-f" in joined
        r = mock.Mock()
        r.returncode = 0
        r.stdout = json.dumps({"id": 42})
        r.stderr = ""
        return r
    with mock.patch.object(gate_check.subprocess, "run", fake_run):
        ok = gate_check.publish_status("r", "a" * 40, "success",
                                       "title", "summary")
    assert ok is True


def test_publish_status_failure_returns_false():
    """发布失败 → False (P1-B: 显式报警, 不静默)."""
    def fake_run(args, input=None, capture_output=True, text=True, timeout=30):
        r = mock.Mock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "error"
        return r
    with mock.patch.object(gate_check.subprocess, "run", fake_run):
        ok = gate_check.publish_status("r", "a" * 40, "failure", "t", "s")
    assert ok is False


def test_publish_failure_state():
    """failure 结论 → state=failure."""
    seen = {}
    def fake_run(args, input=None, capture_output=True, text=True, timeout=30):
        seen["args"] = args
        r = mock.Mock()
        r.returncode = 0
        r.stdout = json.dumps({"id": 1})
        r.stderr = ""
        return r
    with mock.patch.object(gate_check.subprocess, "run", fake_run):
        gate_check.publish_status("r", "a" * 40, "failure", "t", "s")
    assert "state=failure" in " ".join(seen["args"])


def test_scan_publish():
    prs = json.dumps([{"number": 5, "headRefOid": "a" * 40,
                       "updatedAt": "2026-08-11T00:00:00Z"}])
    pr_detail = json.dumps({"headRefOid": "a" * 40, "title": "t",
                            "closingIssuesReferences": [], "state": "OPEN"})
    fake = FakeGh([prs, pr_detail, json.dumps({"id": 1})])
    with mock.patch.object(gate_check, "_gh", fake):
        out = gate_check.scan_and_publish("r", limit=5)
    assert out[0]["pr"] == 5
    assert out[0]["conclusion"] == "failure"  # 无 closing issue
    assert out[0]["head"] == "a" * 12
    assert "published" in out[0]  # P1-B: published 字段


def test_scan_publish_single_pr_no_dup():
    """P1-A: 单 PR + limit=5 → 只取 1 次 (不重复)."""
    prs = json.dumps([{"number": 7, "headRefOid": "b" * 40,
                       "updatedAt": "2026-08-11T00:00:00Z"}])
    pr_detail = json.dumps({"headRefOid": "b" * 40, "title": "t",
                            "closingIssuesReferences": [], "state": "OPEN"})
    fake = FakeGh([prs, pr_detail, json.dumps({"id": 1})])
    with mock.patch.object(gate_check, "_gh", fake):
        out = gate_check.scan_and_publish("r", limit=5)
    assert len(out) == 1  # 只有 1 个 PR → 只发布 1 次
    assert out[0]["pr"] == 7


def test_fetch_comments_meta_array():
    """P0: jq 输出为数组 [ {a,t,b} ] — 多评论正确解析."""
    import pi_gates
    meta_json = json.dumps([
        {"a": "everything-bot-engineer", "t": "2026-08-11T03:47:12Z",
         "b": "[EV-VERDICT]\nauditor=x\nsha=abc\nverdict=PASS\n[/EV-VERDICT]"},
        {"a": "hh1985", "t": "2026-08-11T04:00:00Z",
         "b": "[ADVERSARIAL] baseline=pass"},
    ])
    fake = FakeGh([meta_json])
    with mock.patch.object(pi_gates, "_gh", fake):
        out = pi_gates._fetch_comments_meta("r", 1, None)
    assert len(out) == 2
    assert out[0]["author"] == "everything-bot-engineer"
    assert out[1]["author"] == "hh1985"
    assert "EV-VERDICT" in out[0]["body"]
