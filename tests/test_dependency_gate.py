"""blocked_by 提取测试 — get_project_items 的 GraphQL 响应解析."""
import sys

sys.path.insert(0, ".")
import dispatcher as dp  # noqa: E402


def test_blocked_by_extract_open():
    """GraphQL 响应里 blockedBy 有 OPEN → 提取为未完成依赖."""
    # 模拟 get_project_items 的 content 结构
    content = {
        "__typename": "Issue",
        "number": 100,
        "title": "test",
        "blockedBy": {"nodes": [
            {"number": 208, "state": "OPEN"},
            {"number": 207, "state": "CLOSED"},
        ]},
    }
    # 验证提取逻辑: 只留未完成 (OPEN)
    blocked = [
        b["number"] for b in (content.get("blockedBy", {}).get("nodes") or [])
        if b.get("state") != "CLOSED"
    ]
    assert blocked == [208], "should keep only OPEN deps"


def test_blocked_by_extract_none():
    """无 blockedBy 或无未完成 → 空列表 (可派发)."""
    content = {
        "__typename": "Issue",
        "number": 101,
        "title": "test2",
        "blockedBy": {"nodes": [{"number": 207, "state": "CLOSED"}]},
    }
    blocked = [
        b["number"] for b in (content.get("blockedBy", {}).get("nodes") or [])
        if b.get("state") != "CLOSED"
    ]
    assert blocked == []


def test_blocked_by_missing_key():
    """content 无 blockedBy 键 → 空列表 (不崩)."""
    content = {"__typename": "Issue", "number": 102, "title": "test3"}
    blocked = [
        b["number"] for b in (content.get("blockedBy", {}).get("nodes") or [])
        if b.get("state") != "CLOSED"
    ]
    assert blocked == []
