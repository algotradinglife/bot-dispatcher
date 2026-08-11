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
    """无 closing issue → success (docs/CI-only, 不阻断)."""
    pr = json.dumps({"headRefOid": "a" * 40, "title": "docs: x",
                     "closingIssuesReferences": [], "state": "OPEN"})
    fake = FakeGh([pr])
    with mock.patch.object(gate_check, "_gh", fake):
        c, t, s = gate_check.gate_for_pr("r", 5)
    assert c == "success"
    assert "no linked issue" in t


def test_gate_for_pr_closing_fail():
    """有 closing issue 但 gate FAIL → failure."""
    pr = json.dumps({"headRefOid": "a" * 40, "title": "feat: x",
                     "closingIssuesReferences": [{"number": 1}],
                     "state": "OPEN"})
    # check_pi_gates 会调用很多 gh — 全返回失败 → G01 FAIL
    fake = FakeGh([pr, None, None, None, None, None, None, None, None])
    with mock.patch.object(gate_check, "_gh", fake):
        c, t, s = gate_check.gate_for_pr("r", 5)
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
    prs = json.dumps([{"number": 5, "headRefOid": "a" * 40}])
    pr_detail = json.dumps({"headRefOid": "a" * 40, "title": "t",
                            "closingIssuesReferences": [], "state": "OPEN"})
    fake = FakeGh([prs, pr_detail, json.dumps({"id": 1})])
    with mock.patch.object(gate_check, "_gh", fake):
        out = gate_check.scan_and_publish("r")
    assert out[0]["pr"] == 5
    assert out[0]["conclusion"] == "success"
