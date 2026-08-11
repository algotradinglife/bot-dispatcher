"""PI-GATE unit tests — mock gh CLI, no network.

Covers: G01 graph/owner/blockers, G02 REQ contract, G03 EV SHA binding,
G04 adversarial, G05 reconciliation, G06 downstream activation, and the
AUDITED_SHA parser.
"""
import json
import sys
from unittest import mock

import pytest

sys.path.insert(0, ".")
import pi_gates  # noqa: E402


class FakeGh:
    """Mock gh CLI: returns canned stdout in CALL ORDER (deterministic).

    Each check_pi_gates call sequence is fixed (G01 graph → G02 body →
    G03 pr/comments → G04 comments → G05 state → G06 children), so the
    Nth fake response maps to the Nth gh invocation.
    """

    def __init__(self, responses):
        self.responses = list(responses)  # list of stdout strings
        self.calls = []

    def __call__(self, args, timeout=20):
        self.calls.append(list(args))
        idx = len(self.calls) - 1
        if idx < len(self.responses):
            r = mock.Mock()
            r.returncode = 0
            r.stdout = self.responses[idx]
            r.stderr = ""
            return r
        r = mock.Mock()
        r.returncode = 1
        r.stdout = ""
        r.stderr = "no fake #%d for: %s" % (idx, " ".join(args))
        return r


def _graph_ok():
    return json.dumps({
        "projectItems": [{"status": {"name": "EV Review"}}],
        "blockedBy": [],
        "blocking": [],
        "parent": None,
        "milestone": {"title": "v0_4"},
        "assignees": ["everything-bot-engineer"],
    })


def _graph_blocked():
    d = json.loads(_graph_ok())
    d["blockedBy"] = [{"number": 999, "state": "OPEN"}]
    return json.dumps(d)


def _graph_blocked_closed():
    d = json.loads(_graph_ok())
    d["blockedBy"] = [{"number": 999, "state": "CLOSED"}]
    return json.dumps(d)


def _pr_ok():
    return json.dumps({"headRefOid": "5622a4091378abc", "state": "OPEN"})


# ── AUDITED_SHA parser ──

def test_ev_sha_parser():
    assert pi_gates.EV_SHA_RE.search("AUDITED_SHA=5622a4091378").group(1) \
        == "5622a4091378"
    assert pi_gates.EV_SHA_RE.search("audited_sha=822375ebfe9c").group(1) \
        == "822375ebfe9c"
    assert pi_gates.EV_SHA_RE.search("no marker") is None


# ── G01 ──

def test_g01_pass_unique_owner():
    fake = FakeGh([_graph_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G01"][0] == "PASS"


def test_g01_fail_open_blocker():
    fake = FakeGh([_graph_blocked()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G01"][0] == "FAIL"


def test_g01_pass_closed_blocker():
    """CLOSED blocker 不阻塞 (PI review item 3)."""
    fake = FakeGh([_graph_blocked_closed()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G01"][0] == "PASS"


def test_g01_fail_graph_unavailable():
    fake = FakeGh([])  # 无响应 → returncode 1
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G01"][0] == "FAIL"


# ── G02 ──

def test_g02_req_referenced():
    fake = FakeGh([_graph_ok(), "REQ-E01 applied"])  # G01, G02 body
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G02"][0] == "PASS"


def test_g02_no_req_remind():
    fake = FakeGh([_graph_ok(), "no req here"])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G02"][0] == "REMIND"


# ── G03 ──

def test_g03_pass_sha_matches_head():
    fake = FakeGh([_graph_ok(), "REQ-E01", _pr_ok(),
                   "AUDITED_SHA=5622a4091378"])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "PASS"


def test_g03_fail_stale_sha():
    fake = FakeGh([_graph_ok(), "REQ-E01", _pr_ok(),
                   "AUDITED_SHA=deadbeef0000"])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_no_sha():
    fake = FakeGh([_graph_ok(), "REQ-E01", _pr_ok(), "no audited sha"])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


# ── G04 ──

def test_g04_adversarial_evidence():
    fake = FakeGh([_graph_ok(), "REQ-E01",
                   "baseline comparison and leak check done"])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G04"][0] == "PASS"


# ── G05 ──

def test_g05_pass_closed_done():
    closed = json.dumps({
        "state": "CLOSED",
        "projectItems": [{"status": {"name": "Done"}}],
    })
    fake = FakeGh([_graph_ok(), "REQ-E01", "adv", closed])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G05"][0] == "PASS"


def test_g05_remind_open():
    closed = json.dumps({
        "state": "OPEN",
        "projectItems": [{"status": {"name": "EV Review"}}],
    })
    fake = FakeGh([_graph_ok(), "REQ-E01", "adv", closed])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G05"][0] == "REMIND"


# ── G06 ──

def test_g06_premature_activation():
    d = json.loads(_graph_ok())
    d["blocking"] = [{"number": 300, "state": "OPEN"}]
    graph = json.dumps(d)
    fake = FakeGh([graph, "REQ-E01", "adv", "{}", '"Ready"'])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G06"][0] == "FAIL"


def test_g06_skip_no_children():
    fake = FakeGh([_graph_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G06"][0] == "SKIP"


# ── receipt ──

def test_render_receipt():
    res = {"G01": ("PASS", "ok"), "G02": ("FAIL", "no REQ"),
           "G03": ("SKIP", ""), "G04": ("REMIND", "x"),
           "G05": ("PASS", "y"), "G06": ("SKIP", "")}
    out = pi_gates.render_receipt(res, "merge")
    assert "PI-G01" in out and "✅" in out and "⛔" in out
    assert "阻断" in out
