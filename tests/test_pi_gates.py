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
            if self.responses[idx] is None:
                r.returncode = 1  # 模拟 gh 失败
                r.stdout = ""
            else:
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


def _graph_blocked_nodes():
    """gh 真实结构: blockedBy: {nodes: [{number, state}]}."""
    d = json.loads(_graph_ok())
    d["blockedBy"] = {"nodes": [{"number": 999, "state": "OPEN"}]}
    return json.dumps(d)


def _pr_ok():
    return json.dumps({"headRefOid": "5622a4091378abc" + "0" * 25,
                       "state": "OPEN"})


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


def test_g01_fail_nodes_blocker():
    """gh {nodes:[...]} 结构: OPEN blocker 正确检出."""
    fake = FakeGh([_graph_blocked_nodes()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G01"][0] == "FAIL"
    assert "999" in res["G01"][1]


def test_g01_fail_graph_unavailable():
    fake = FakeGh([])  # 无响应 → returncode 1
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G01"][0] == "FAIL"


# ── G02 ──

def test_g02_req_referenced():
    # 契约 body 含 REQ-E01; 评论含交付表行 | REQ-E01 |
    fake = FakeGh([_graph_ok(), "REQ-E01 required",
                   "| REQ-E01 | evidence | PASS |"])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G02"][0] == "PASS"


def test_g02_req_missing_evidence():
    """契约引 REQ-E01+E02, 交付表只有 E01 → FAIL."""
    fake = FakeGh([_graph_ok(), "REQ-E01 REQ-E02 required",
                   "| REQ-E01 | evidence | PASS |"])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G02"][0] == "FAIL"
    assert "E02" in res["G02"][1]


def test_g02_no_req_remind():
    fake = FakeGh([_graph_ok(), "no req here", ""])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G02"][0] == "REMIND"


# ── G03 ──
# 调用序: G01 graph, G02 body, G02 comments, G03 pr(head_before),
#         G03 issue comments meta, G03 pr comments meta, G03 pr(head_after)

def _ev_block(sha="5622a4091378abc" + "0" * 25,
              auditor="everything-bot-engineer",
              verdict="PASS",
              ts="2026-08-11T03:47:12Z"):
    return ("[EV-VERDICT]\n"
            "auditor=%s\nsha=%s\nverdict=%s\ntimestamp=%s\n[/EV-VERDICT]"
            % (auditor, sha, verdict, ts))


def _comments_json(blocks, authors=None, ts=None):
    """_fetch_comments_meta 的 gh 输出: [{a, t, b}] JSON."""
    items = []
    for i, b in enumerate(blocks):
        items.append({"a": (authors[i] if authors else "everything-bot-engineer"),
                      "t": (ts[i] if ts else "2026-08-11T03:47:12Z"),
                      "b": b})
    return json.dumps(items)


def test_g03_pass_structured():
    """结构化 EV-VERDICT: 完整 SHA + 可信 auditor + 匹配 HEAD → PASS."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _pr_ok(), _comments_json([_ev_block()]), "",
                   _pr_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "PASS"


def test_g03_fail_short_sha():
    """SHA 不足 40 位 → FAIL (不完整 SHA)."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _pr_ok(), _comments_json([_ev_block(sha="5622a4091378")]), "",
                   _pr_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_41char_sha():
    """41 位 SHA → FAIL (严格 40 位)."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _pr_ok(), _comments_json([_ev_block(sha="a" * 41)]), "",
                   _pr_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_forged_auditor():
    """伪造 auditor: 评论作者不是可信 auditor → FAIL (P0-2)."""
    # 正文写 auditor=everything-bot-engineer 但真实作者是 attacker
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _pr_ok(),
                   _comments_json([_ev_block()], authors=["attacker-account"]),
                   "", _pr_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_later_reject():
    """later REJECT (P0-1): 最新 verdict 是 REJECT → FAIL."""
    blocks = [_ev_block(verdict="PASS", ts="2026-08-11T00:11:42Z"),
              _ev_block(verdict="REJECT", ts="2026-08-11T03:47:12Z")]
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _pr_ok(), _comments_json(blocks), "", _pr_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_head_changed():
    """HEAD 二次变化 (P0-3): head_before != head_after → FAIL."""
    head1 = json.dumps({"headRefOid": "5622a4091378abc" + "0" * 25, "state": "OPEN"})
    head2 = json.dumps({"headRefOid": "deadbeef" + "0" * 33, "state": "OPEN"})
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   head1, _comments_json([_ev_block()]), "", head2])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_head_second_read_error():
    """P0-A fail-closed: 第二次 HEAD 读取失败 → FAIL (不得判定稳定)."""
    head1 = json.dumps({"headRefOid": "5622a4091378abc" + "0" * 25, "state": "OPEN"})
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   head1, _comments_json([_ev_block()]), "",
                   None])  # 第二次读失败 (FakeGh 超限返回 rc=1)
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_stale_sha():
    """结构化 SHA 与 HEAD 不一致 → FAIL (stale)."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _pr_ok(),
                   _comments_json([_ev_block(sha="deadbeef" * 5)]), "",
                   _pr_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_g03_fail_no_sha():
    """无 EV-VERDICT 也无 legacy AUDITED_SHA → FAIL."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _pr_ok(), _comments_json(["no verdict"]), "", _pr_ok()])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1, pr_num=5)
    assert res["G03"][0] == "FAIL"


def test_parse_ev_verdicts():
    text = ("[EV-VERDICT]\nauditor=a\nsha=111\nverdict=PASS\ntimestamp=T1\n"
            "[/EV-VERDICT]\n[EV-VERDICT]\nauditor=b\nsha=222\nverdict=REJECT"
            "\ntimestamp=T2\n[/EV-VERDICT]")
    vs = pi_gates.parse_ev_verdicts(text)
    assert len(vs) == 2
    assert vs[0]["sha"] == "111" and vs[1]["verdict"] == "REJECT"


# ── G04 ──
# 调用序: G01 graph, G02 body, G02 comments, G04 issue comments meta

def test_g04_structured_marker():
    """PI (hh1985) 评论含 [ADVERSARIAL] 键值对 → PASS."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["[ADVERSARIAL] baseline=pass leak=pass"],
                                  authors=["hh1985"])])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G04"][0] == "PASS"


def test_g04_invalid_marker_remind():
    """[ADVERSARIAL] hello (无键值对) → REMIND."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["[ADVERSARIAL] hello"],
                                  authors=["hh1985"])])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G04"][0] == "REMIND"


def test_g04_non_pi_author_remind():
    """非 PI 评论的对抗标记 → REMIND (P1-B 作者验证)."""
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["[ADVERSARIAL] baseline=pass"],
                                  authors=["attacker-account"])])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G04"][0] == "REMIND"


# ── G05 ──
# 调用序: G01 graph, G02 body, G02 comments, G04 issue comments meta,
#         G05 state

def test_g05_pass_closed_done():
    closed = json.dumps({
        "state": "CLOSED",
        "projectItems": [{"status": {"name": "Done"}}],
    })
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["adv"], authors=["hh1985"]), closed])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G05"][0] == "PASS"


def test_g05_remind_open():
    closed = json.dumps({
        "state": "OPEN",
        "projectItems": [{"status": {"name": "EV Review"}}],
    })
    fake = FakeGh([_graph_ok(), "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["adv"], authors=["hh1985"]), closed])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G05"][0] == "REMIND"


# ── G06 ──
# 调用序: G01 graph, G02 body, G02 comments, G04 issue comments meta,
#         G05 state, G06 child status, G06 child comments meta

def test_g06_premature_activation():
    """下游 Ready 且无 PI [ACTIVATE] → FAIL."""
    d = json.loads(_graph_ok())
    d["blocking"] = [{"number": 300, "state": "OPEN"}]
    graph = json.dumps(d)
    fake = FakeGh([graph, "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["adv"], authors=["hh1985"]),
                   "{}", '"Ready"',
                   _comments_json(["no activation"], authors=["hh1985"])])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G06"][0] == "FAIL"


def test_g06_authorized_activation_pass():
    """下游 Ready + PI [ACTIVATE] receipt → PASS."""
    d = json.loads(_graph_ok())
    d["blocking"] = [{"number": 300, "state": "OPEN"}]
    graph = json.dumps(d)
    fake = FakeGh([graph, "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["adv"], authors=["hh1985"]),
                   "{}", '"Ready"',
                   _comments_json(["[ACTIVATE] issue=300 by PI"],
                                  authors=["hh1985"])])
    with mock.patch.object(pi_gates, "_gh", fake):
        res = pi_gates.check_pi_gates("r", issue_num=1)
    assert res["G06"][0] == "PASS"


def test_g06_forged_activate_remind():
    """[ACTIVATE] 来自非 PI → 不算授权 → FAIL (P1-C)."""
    d = json.loads(_graph_ok())
    d["blocking"] = [{"number": 300, "state": "OPEN"}]
    graph = json.dumps(d)
    fake = FakeGh([graph, "REQ-E01", "| REQ-E01 | x | PASS |",
                   _comments_json(["adv"], authors=["hh1985"]),
                   "{}", '"Ready"',
                   _comments_json(["[ACTIVATE] issue=300"],
                                  authors=["attacker-account"])])
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


def test_render_receipt_json():
    """机器可解析 receipt (PI review item 4)."""
    res = {"G01": ("PASS", "ok"), "G02": ("FAIL", "no REQ"),
           "G03": ("SKIP", ""), "G04": ("REMIND", "x"),
           "G05": ("PASS", "y"), "G06": ("SKIP", "")}
    out = pi_gates.render_receipt(res, "merge", as_json=True)
    d = json.loads(out)
    assert d["operation"] == "merge"
    assert d["gates"]["G01"]["status"] == "PASS"
    assert d["blocked"] == ["G02"]
    assert "timestamp" in d
