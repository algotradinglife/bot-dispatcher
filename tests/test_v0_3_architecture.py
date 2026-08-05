"""v0_3 架构测试: 契约层八态 + 角色通知语义 (dispatcher 纯通知+监控).

覆盖:
  - Ready → 通知 worker
  - EV Review → 通知 auditor
  - Human → 通知 user
  - In Progress / PI Review / Blocked / Done → 不通知 (PI 轮询)
  - queue_goal 按 role 记录 + 同 tick 去重
  - flush_goals 输出结构化通知 (非 agent-deck/kanban)
  - 评论路由已删: 无 [TO:] 解析 / mention_map / 评论扫描
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bot_dispatcher", ROOT / "dispatcher.py")
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def make_items(statuses):
    """Build fake project items list with given statuses."""
    return [
        {"number": n, "project_num": 1, "is_pr": False, "status": s}
        for n, s in enumerate(statuses, start=1)
    ]


def run_main(statuses, prev_state=None):
    """Run MOD.main() with mocked GitHub reads; return parsed JSON output."""
    items = make_items(statuses)
    with patch.object(MOD, "build_issue_proj_map", return_value=({}, items)), \
         patch.object(MOD.subprocess, "run",
                      side_effect=lambda *a, **k: SimpleNamespace(
                          returncode=0, stdout=json.dumps({"title": "T"}),
                          stderr="")), \
         patch.object(MOD, "load_config",
                      return_value={"repo": "o/r", "projects": [],
                                    "assignee_map": {}}), \
         patch.object(MOD, "save_state", return_value=None), \
         patch.object(MOD, "load_state", return_value=prev_state), \
         patch.object(MOD, "flush_goals", return_value=True), \
         patch.object(MOD, "extract_report_url", return_value=None), \
         patch("sys.argv", ["dispatcher", "--repo", "o/r",
                            "--config", "/nonexistent.yaml", "--dry-run"]):
        # capture print
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            MOD.main()
        return json.loads(buf.getvalue())


def test_ready_notifies_worker():
    out = run_main(["Inbox", "Ready"])
    roles = [n["role"] for n in out.get("notifications", [])]
    assert "worker" in roles, "Ready 必须通知 worker"


def test_ev_review_notifies_auditor():
    out = run_main(["Inbox", "EV Review"])
    roles = [n["role"] for n in out.get("notifications", [])]
    assert "auditor" in roles, "EV Review 必须通知 auditor"


def test_human_notifies_user():
    out = run_main(["Inbox", "Human"])
    roles = [n["role"] for n in out.get("notifications", [])]
    assert "user" in roles, "Human 必须通知 user"


def test_pi_review_no_notification():
    """PI 主动轮询, dispatcher 不通知 PI Review."""
    out = run_main(["Inbox", "PI Review"])
    roles = [n["role"] for n in out.get("notifications", [])]
    assert "pi" not in roles
    assert roles == [], "PI Review 不应产生任何通知"


def test_in_progress_no_notification():
    out = run_main(["Inbox", "In Progress"])
    roles = [n["role"] for n in out.get("notifications", [])]
    assert roles == [], "In Progress 纯可见状态, 不通知"


def test_blocked_no_notification():
    """Blocked 由 PI 轮询感知, dispatcher 不通知 (PI 不接收通知)."""
    out = run_main(["Inbox", "Blocked"])
    roles = [n["role"] for n in out.get("notifications", [])]
    assert "pi" not in roles
    assert "worker" not in roles


def test_done_no_notification():
    """Done 成果汇报走 PM 对话接口, dispatcher 不通知."""
    out = run_main(["Inbox", "Done"])
    roles = [n["role"] for n in out.get("notifications", [])]
    assert roles == [], "Done 不产生 dispatcher 通知"


def test_queue_goal_records_role_and_dedupes():
    output = {"notifications": []}
    MOD.queue_goal(output, "worker", "msg1", issue_num=5)
    MOD.queue_goal(output, "worker", "msg1", issue_num=5)  # dup
    MOD.queue_goal(output, "auditor", "msg1", issue_num=5)
    MOD.queue_goal(output, "worker", "msg2", issue_num=6)
    assert len(output["notifications"]) == 3
    assert output["notifications"][0] == {"role": "worker", "message": "msg1",
                                          "issue": 5}


def test_queue_goal_empty_role_ignored():
    output = {"notifications": []}
    MOD.queue_goal(output, None, "msg")
    assert output["notifications"] == []


def test_flush_goals_emits_notifications_not_cards():
    output = {"notifications": [{"role": "worker", "message": "m",
                                 "issue": 1}]}
    ok = MOD.flush_goals(output, dry_run=True)
    assert ok is True
    assert output["notifications"][0]["dry_run"] is True


def test_flush_goals_baseline_skips_notifications():
    output = {"notifications": [{"role": "user", "message": "m", "issue": 1}]}
    ok = MOD.flush_goals(output, baseline=True)
    assert ok is True
    assert output["notifications"] == [], "baseline 不输出通知 (历史不重放)"


def test_no_comment_routing_code_remains():
    """评论路由机制已删: 无 parse_to_directive / build_mention_map."""
    src = Path(MOD.__file__).read_text()
    assert "parse_to_directive" not in src
    assert "build_mention_map" not in src
    assert "resolve_author_default_session" not in src


def test_no_kanban_or_agentdeck_delivery():
    """投递逻辑已删: 无 kanban create / agent-deck send / tmux Enter."""
    src = Path(MOD.__file__).read_text()
    assert "kanban create" not in src
    assert "agent-deck" not in src.replace('不投递 agent-deck / kanban', '')
    assert "submit_session_enter" not in src
    assert "build_kanban_command" not in src
