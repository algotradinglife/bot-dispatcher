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


def test_publish_checkrun():
    def fake_run(args, input=None, capture_output=True, text=True, timeout=30):
        assert "--input" in args and "-" in args
        r = mock.Mock()
        r.returncode = 0
        r.stdout = json.dumps({"id": 42})
        r.stderr = ""
        return r
    with mock.patch.object(gate_check.subprocess, "run", fake_run):
        cid = gate_check.publish_checkrun("r", "a" * 40, "success",
                                          "title", "summary")
    assert cid == 42


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
