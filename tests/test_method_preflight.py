"""method_preflight 单元测试."""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, ".")
import method_preflight as mp  # noqa: E402

VALID_YAML = """\
protocol_version: 1
candidate_pool: {source: "frozen", size: 100}
splits: {selection_split: CV_2022, terminal_split: CV_2023, holdout_isolated: true}
selection_algorithm: {method: frozen, preregistered: true, seed: 42}
metrics:
  - {name: hhi, definition: "agg by identity", golden_test: "t.py::test_hhi"}
budget: {max_runtime_h: 8, max_cpu_hours: 64}
required_artifacts: ["results/ledger.parquet"]
schema_ref: "results/schema.yaml"
receipt: {bind_to: "results/manifest.sha256"}
"""


def _make_worktree(with_method=True, tamper_manifest=False):
    d = tempfile.mkdtemp()
    if with_method:
        open(os.path.join(d, "method.yaml"), "w").write(VALID_YAML)
    os.makedirs(os.path.join(d, "results"), exist_ok=True)
    open(os.path.join(d, "results", "ledger.parquet"), "w").write("x")
    h = "dummyhash"
    if with_method:
        import hashlib
        h = hashlib.sha256(
            open(os.path.join(d, "method.yaml"), "rb").read()).hexdigest()
        if tamper_manifest:
            h = "0" * 64
    open(os.path.join(d, "results", "manifest.sha256"), "w").write(
        "%s  method.yaml\n" % h)
    return d


def test_pass_valid():
    d = _make_worktree()
    res, code = mp.run(d, verbose=False)
    assert code == 0, res
    assert res["status"] == "PASS"
    assert res["checks"]["schema"]["ok"]
    assert res["checks"]["receipt_hash"]["ok"]


def test_missing_method_fails():
    d = _make_worktree(with_method=False)
    res, code = mp.run(d, verbose=False)
    assert code == 1
    assert res["status"] == "FAIL"


def test_tampered_manifest_fails():
    """协议被改但 manifest 未更新 → FAIL（worker 不得改协议不声明）."""
    d = _make_worktree(tamper_manifest=True)
    res, code = mp.run(d, verbose=False)
    assert code == 1
    assert not res["checks"]["receipt_hash"]["ok"]


def test_missing_artifact_fails():
    d = _make_worktree()
    os.remove(os.path.join(d, "results", "ledger.parquet"))
    res, code = mp.run(d, verbose=False)
    assert code == 1
    assert not res["checks"]["artifacts"]["ok"]


def test_bad_schema_fails():
    d = _make_worktree()
    with open(os.path.join(d, "method.yaml"), "w") as f:
        f.write("protocol_version: 1\n")  # 缺必需字段
    res, code = mp.run(d, verbose=False)
    assert code == 1
    assert not res["checks"]["schema"]["ok"]


def test_stages_schema():
    """stages 结构校验: 缺 skill 字段 → FAIL."""
    d = _make_worktree()
    bad = VALID_YAML.replace(
        "required_artifacts: [\"results/ledger.parquet\"]",
        "stages:\n  - {name: eda, deliverable: x}\n"
        "required_artifacts: [\"results/ledger.parquet\"]")
    with open(os.path.join(d, "method.yaml"), "w") as f:
        f.write(bad)
    res, code = mp.run(d, verbose=False)
    assert code == 1
    assert not res["checks"]["schema"]["ok"]


def test_stages_valid():
    """stages 合法 → PASS."""
    d = _make_worktree()
    ok = VALID_YAML.replace(
        "required_artifacts: [\"results/ledger.parquet\"]",
        "stages:\n"
        "  - {name: data_stats, deliverable: x, skill: eda-checklist, constraints: []}\n"
        "  - {name: model_selection, deliverable: y, skill: split-strategy, constraints: [\"CV_2022 only\"]}\n"
        "required_artifacts: [\"results/ledger.parquet\"]")
    with open(os.path.join(d, "method.yaml"), "w") as f:
        f.write(ok)
    # 重建 manifest 匹配新 method.yaml hash
    import hashlib
    h = hashlib.sha256(open(os.path.join(d, "method.yaml"), "rb").read()).hexdigest()
    with open(os.path.join(d, "results", "manifest.sha256"), "w") as f:
        f.write("%s  method.yaml\n" % h)
    res, code = mp.run(d, verbose=False)
    assert code == 0, res
    assert res["checks"]["schema"]["ok"]


def test_json_output():
    d = _make_worktree()
    with mock.patch.object(sys, "argv", ["method_preflight.py", "--worktree", d, "--json"]):
        res, code = mp.run(d, verbose=False)
    assert code == 0
