"""finalize_delivery 单元测试 (mock gh)."""
import json
import sys
from unittest import mock

sys.path.insert(0, ".")
import finalize_delivery as fd  # noqa: E402


class FakeGQL:
    def __init__(self, responses):
        self.responses = responses  # list of (stdout, code)
        self.calls = []

    def __call__(self, query, timeout=25):
        self.calls.append(query)
        idx = len(self.calls) - 1
        if idx < len(self.responses):
            out, code = self.responses[idx]
            r = mock.Mock()
            r.stdout = out
            r.stderr = ""
            r.returncode = code
            return r
        r = mock.Mock()
        r.stdout = ""
        r.stderr = "unexpected call %d" % idx
        r.returncode = 1
        return r


def _item_json(status="In Progress"):
    return json.dumps({"data": {"repository": {"issue": {"projectItems": {
        "nodes": [{"id": "PVTI_item1",
                   "project": {"id": "PVT_proj1"},
                   "fieldValueByName": {"name": status}}]}}}}})


def _fields_json():
    return json.dumps({"data": {"node": {"field": {
        "id": "PVTSSF_f1",
        "options": [{"id": "opt_ev", "name": "EV Review"},
                    {"id": "opt_ip", "name": "In Progress"}]}}}})


def _set_json():
    return json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "x"}}}})


def _pr_json(head="a" * 40, state="OPEN", draft=False, closes=5):
    return json.dumps({"state": state, "headRefOid": head, "isDraft": draft,
                       "closingIssuesReferences": [{"number": closes}]})


def _run(repo, issue, pr, head, check_only=False):
    return fd.finalize(repo, issue, pr, head, check_only, verbose=False)


def test_pass_writes_ev_review():
    """全绿: 写入 EV Review + 读回确认."""
    fake = FakeGQL([(_item_json(), 0), (_fields_json(), 0), (_set_json(), 0), (_item_json("EV Review"), 0)])
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(fd, "_gh", lambda *a, **k: mock.Mock(
             returncode=0, stdout=_pr_json(), stderr="")) as ghm:
        ok, steps, receipt = _run("beijing-lot", 5, 10, "a" * 40)
    assert ok, steps
    assert receipt["status_after"] == "EV Review"
    # 写状态被调用
    set_calls = [c for c in fake.calls if "updateProjectV2ItemFieldValue" in c]
    assert len(set_calls) == 1


def test_check_only_no_write():
    """--check-only: 不写状态."""
    fake = FakeGQL([(_item_json(), 0), (_fields_json(), 0)])  # 无 set/readback 调用
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(fd, "_gh", lambda *a, **k: mock.Mock(
             returncode=0, stdout=_pr_json(), stderr="")):
        ok, steps, receipt = _run("beijing-lot", 5, 10, "a" * 40, check_only=True)
    assert ok, steps
    assert receipt["mode"] == "check-only"
    set_calls = [c for c in fake.calls if "updateProjectV2ItemFieldValue" in c]
    assert len(set_calls) == 0


def test_pr_draft_fails():
    """PR 还是 draft → FAIL."""
    fake = FakeGQL([(_item_json(), 0)])
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(fd, "_gh", lambda *a, **k: mock.Mock(
             returncode=0, stdout=_pr_json(draft=True), stderr="")):
        ok, steps, _ = _run("beijing-lot", 5, 10, "a" * 40)
    assert not ok
    assert not steps[1]["ok"]  # pr_ready


def test_head_mismatch_fails():
    """HEAD 不匹配 → FAIL."""
    fake = FakeGQL([(_item_json(), 0)])
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(fd, "_gh", lambda *a, **k: mock.Mock(
             returncode=0, stdout=_pr_json(head="b" * 40), stderr="")):
        ok, steps, _ = _run("beijing-lot", 5, 10, "a" * 40)
    assert not ok
    assert not steps[3]["ok"]  # head_match


def test_wrong_status_fails():
    """当前状态不是 In Progress (如 Ready) → FAIL."""
    fake = FakeGQL([(_item_json(status="Ready"), 0)])
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(fd, "_gh", lambda *a, **k: mock.Mock(
             returncode=0, stdout=_pr_json(), stderr="")):
        ok, steps, _ = _run("beijing-lot", 5, 10, "a" * 40)
    assert not ok
    assert not steps[5]["ok"]  # issue_in_progress


def test_multi_project_fails():
    """多 Project → FAIL."""
    multi = json.dumps({"data": {"repository": {"issue": {"projectItems": {
        "nodes": [
            {"id": "a", "project": {"id": "p1"},
             "fieldValueByName": {"name": "In Progress"}},
            {"id": "b", "project": {"id": "p2"},
             "fieldValueByName": {"name": "In Progress"}},
        ]}}}}})
    fake = FakeGQL([(multi, 0)])
    with mock.patch.object(fd, "_gql", fake), \
         mock.patch.object(fd, "_gh", lambda *a, **k: mock.Mock(
             returncode=0, stdout=_pr_json(), stderr="")):
        ok, steps, _ = _run("beijing-lot", 5, 10, "a" * 40)
    assert not ok
    assert "必须恰好 1 个" in steps[4]["detail"]  # issue_project
