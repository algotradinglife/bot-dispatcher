#!/usr/bin/env python3
"""
Kanban → GitHub sync job (reporting channel of the three-layer system).

Reads done kanban cards and mirrors their evidence summary back to the
control plane: posts a GitHub issue comment and moves the linked issue to
Review on its Project board.

Direction is strictly one-way: kanban (execution state) → GitHub (decision
state). This script never reads GitHub status to drive kanban, never mutates
kanban, and never touches Issue Graph.

Usage:
  sync_job.py --repo <owner/name> --config <dispatcher.yaml> [--board <slug>]
              [--dry-run] [--state-dir <dir>]

Idempotency: a state file records card ids already synced; a card is only
reported once even if the job runs repeatedly.

Eventual consistency: cards are read with `hermes kanban list --status done`
(scoped to the configured board via --board when provided).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_STATE_HOME = Path(__file__).resolve().parent.parent / ".sync_state"
ISSUE_REF_RE = re.compile(r"#(\d+)")


# ── pure helpers (unit-testable) ──────────────────────────────────────

def extract_issue_number(card: dict) -> int | None:
    """Find the source issue number from a done card.

    状态表示原则: 优先解析**机器生成的格式** (dispatcher 建卡模板):
      1. body 结构化锚点 `issue: <num>` (dispatcher 写入, 最明确)
      2. title 固定模板 `[Issue #N]` (dispatcher 生成, 格式可靠)
    自由文本兜底 (body URL / #N mention) 仅当上面都没有时尝试,
    且失败返回 None (fail-closed, 不猜).
    """
    body = card.get("body") or ""
    m = re.search(r"(?:^|\s)issue[:#]\s*(\d+)", body, re.I)
    if m:
        return int(m.group(1))
    title = card.get("title") or ""
    m = re.match(r"^\[Issue #(\d+)\]", title.strip())
    if m:
        return int(m.group(1))
    # 兜底: 完整 URL (仅当上述机器格式都没有)
    m = re.search(r"github\.com/[^/\s]+/[^/\s]+/issues/(\d+)", body)
    if m:
        return int(m.group(1))
    return None


def build_review_comment(card: dict) -> str:
    """Compose the GitHub issue comment that mirrors the card's evidence.

    The comment is the *evidence handoff*: what was done, the result summary,
    and structured metadata (if any). It is deliberately conservative — it
    reports execution output, it does not claim acceptance.
    """
    title = card.get("title") or "(untitled)"
    result = card.get("result") or ""
    completed = card.get("completed_at")
    lines = [
        "## 🤖 执行完成（kanban 自动同步）",
        "",
        "**任务**: %s" % title,
    ]
    if completed:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(completed))
        lines.append("**完成时间**: %s" % ts)
    if result:
        lines += ["", "**结果摘要**:", "", result]
    summary = card.get("summary")
    if summary and summary != result:
        lines += ["", "**交接摘要**:", "", summary]
    metadata = card.get("metadata")
    if metadata:
        try:
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            if isinstance(metadata, dict) and metadata:
                lines += ["", "**结构化元数据**:", ""]
                for k, v in metadata.items():
                    lines.append("- %s: %s" % (k, v))
        except (json.JSONDecodeError, TypeError):
            lines += ["", "**元数据**: %s" % metadata]
    lines += [
        "",
        "> 状态由 kanban done 单向映射至 Review，等待 PI 评审。",
        "> 本评论由 sync job 自动生成；证据完整性由报告 watchdog 校验。",
    ]
    return "\n".join(lines)


# ── sync orchestration ────────────────────────────────────────────────

def list_done_cards(board: str | None = None) -> list[dict]:
    argv = ["hermes", "kanban", "list", "--status", "done", "--json"]
    if board:
        # Board selection is a global CLI switch (not a --tenant filter).
        r = subprocess.run(
            ["hermes", "kanban", "boards", "switch", board],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError("kanban boards switch failed: %s" % r.stderr.strip()[:200])
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("kanban list failed: %s" % r.stderr.strip()[:200])
    return json.loads(r.stdout or "[]")


def list_all_cards(board: str | None = None) -> list[dict]:
    """All cards regardless of status (for EV verdict sync)."""
    argv = ["hermes", "kanban", "list", "--json"]
    if board:
        r = subprocess.run(
            ["hermes", "kanban", "boards", "switch", board],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError("kanban boards switch failed: %s" % r.stderr.strip()[:200])
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("kanban list failed: %s" % r.stderr.strip()[:200])
    return json.loads(r.stdout or "[]")


EV_TITLE_RE = re.compile(r"^\[EV\] (?:issue )?#(\d+)\b", re.I)


def is_ev_card(card: dict) -> bool:
    """EV 卡识别: title 严格匹配 `[EV] issue #N` 机器格式.

    这是 dispatcher 建 EV 卡时的固定模板 (机器生成, 格式可控);
    不用宽松 startswith 以免 AI 生成的普通标题误判.
    """
    return bool(EV_TITLE_RE.match(str(card.get("title") or "").strip()))


def current_gh_user() -> str:
    """当前 gh 登录账号 (用于角色守卫)."""
    r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("gh user lookup failed: %s" % r.stderr.strip()[:160])
    return r.stdout.strip()


# 角色账号模型: 每个角色绑定唯一账号, 通过环境变量预传递 (调用方注入).
# 脚本/worker 只读环境变量, 不自行选择账号 —— 杜绝硬编码错位.
#   GH_USER_PI      — PI 账号 (评审/merge/验收)
#   GH_USER_WORKER  — worker 账号 (产出/PR)
#   GH_USER_AUDITOR — auditor 账号 (EV 裁决评论, 账号即物证)
def role_user(role: str) -> str:
    env_map = {
        "pi": "GH_USER_PI",
        "worker": "GH_USER_WORKER",
        "auditor": "GH_USER_AUDITOR",
    }
    var = env_map.get(role)
    if not var:
        raise RuntimeError("未知角色: %s" % role)
    user = os.environ.get(var, "").strip()
    if not user:
        raise RuntimeError(
            "账号缺失: 环境变量 %s 未设置 — 角色账号必须由调用方预传递 "
            "(fail-closed, 禁止默认账号)" % var)
    return user


def switch_gh_user(gh_user: str) -> None:
    """切换到指定账号, 并验证切换成功 (fail-closed: 失败即抛错, 不静默)."""
    r = subprocess.run(["gh", "auth", "switch", "--user", gh_user],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(
            "gh auth switch 到 %s 失败: %s" % (gh_user, r.stderr.strip()[:160]))
    actual = current_gh_user()
    if actual != gh_user:
        raise RuntimeError(
            "账号守卫: 期望 %s, 实际 %s — 拒绝继续 (防角色错位)" % (gh_user, actual))


def guard_gh_user(expected: str) -> None:
    """发评论前守卫: 当前账号必须是期望账号, 否则 fail-closed."""
    actual = current_gh_user()
    if actual != expected:
        raise RuntimeError(
            "账号守卫: EV 评论应由 %s 发布, 但当前账号是 %s — 拒绝写入 "
            "(账号即物证, 防止审计身份错位)" % (expected, actual))


def parse_verdict(result: str) -> str | None:
    """从 result 解析结构化裁决标记.

    约定: result 首行必须是机器可读标记 `VERDICT: PASS` 或 `VERDICT: REJECT`
    (Alan 按 auditor-ev-templates 写入), 后续行为人类可读详情.
    无法解析 → 返回 None (调用方 fail-closed, 绝不宽松猜文本).
    """
    for line in result.splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            if v == "PASS":
                return "PASS"
            if v == "REJECT":
                return "REJECT"
            return None  # VERDICT: 存在但值不合法 → 不猜
    return None  # 无结构化标记 → 不猜


def sync_ev_verdict(card: dict, repo: str, dry_run: bool = False,
                    gh_user: str | None = None) -> dict:
    """把 Alan 的 EV 裁决作为 issue comment 同步到 GitHub.

    EV 卡完成 (status=done) 时, result 必须含结构化裁决标记
    `VERDICT: PASS|REJECT` (auditor-ev-templates 约定) + 人类可读详情.
    此同步是汇报通道的一部分 (与证据摘要同步同一哲学):
    - 评论贴到 EV 卡关联的 issue
    - 评论即 PI Review 的输入; REJECT 时 PI 据此驳回回卡, 不置 Review
    - 账号守卫: 发评论前验证当前 gh 账号 == gh_user (auditor 账号),
      否则 fail-closed —— 账号即物证, 防止审计身份错位
    - 状态表示: 裁决只认结构化标记, 不解析自由文本 (防误判)
    """
    if not gh_user:
        raise RuntimeError("sync_ev_verdict 必须指定 gh_user (auditor 账号)")
    issue_num = extract_issue_number(card)
    if issue_num is None:
        return {"card": card["id"], "status": "skipped", "reason": "no issue ref"}
    result = card.get("result") or ""
    if not result.strip():
        return {"card": card["id"], "status": "skipped", "reason": "no verdict in result"}

    # 结构化裁决解析 (fail-closed: 无标记/非法值 → 拒绝, 不猜文本)
    parsed = parse_verdict(result)
    if parsed is None:
        return {"card": card["id"], "status": "failed",
                "reason": "result 缺 VERDICT: PASS|REJECT 结构化标记 "
                          "(auditor-ev-templates 约定)"}
    if parsed == "REJECT":
        verdict_label = "❌ **REJECT**（驳回回卡）"
    else:
        verdict_label = "✅ **PASS**（通过）"

    comment = [
        "## 🔍 EV 裁决（auditor 自动同步）",
        "",
        "**审计对象**: %s" % (card.get("title") or "(untitled)"),
        "**裁决**: %s" % verdict_label,
        "",
        "**裁决详情**:",
        "",
        result,
    ]
    body = "\n".join(comment)
    actions = []
    if dry_run:
        actions.append({"action": "ev_comment", "dry_run": True, "body": body[:100]})
    else:
        # 账号守卫: 审计评论必须以 auditor 账号发布, 防止身份错位
        guard_gh_user(gh_user)
        r = subprocess.run(
            ["gh", "issue", "comment", str(issue_num), "--repo", repo, "--body", body],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {"card": card["id"], "status": "failed",
                    "reason": "ev_comment: %s" % r.stderr.strip()[:160]}
        actions.append({"action": "ev_comment", "ok": True})
    return {"card": card["id"], "status": "synced", "issue": issue_num,
            "verdict": verdict_label, "actions": actions}


def load_synced(state_file: Path) -> set[str]:
    if state_file.exists():
        try:
            return set(json.loads(state_file.read_text()).get("synced", []))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_synced(state_file: Path, synced: set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"synced": sorted(synced)}, indent=2))


def resolve_item_id(repo: str, issue_num: int, project_node: str) -> str:
    """gh api graphql: find the ProjectV2 item id for a linked issue."""
    query = (
        'query($p: ID!) { node(id: $p) { ... on ProjectV2 { items(first: 100) {'
        ' nodes { id content { ... on Issue { number } } } } } } }'
    )
    r = subprocess.run(
        ["gh", "api", "graphql", "-f", "query=%s" % query, "-F", "p=%s" % project_node],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError("item lookup failed: %s" % r.stderr.strip()[:200])
    data = json.loads(r.stdout)
    for node in data["data"]["node"]["items"]["nodes"]:
        if node.get("content") and node["content"].get("number") == issue_num:
            return node["id"]
    return ""


def sync_one_card(card: dict, repo: str, project: dict | None,
                  dry_run: bool = False) -> dict:
    """Post the evidence comment and (optionally) move the issue to Review."""
    issue_num = extract_issue_number(card)
    if issue_num is None:
        return {"card": card["id"], "status": "skipped", "reason": "no issue ref"}
    actions = []

    # 1. Evidence comment
    comment = build_review_comment(card)
    if dry_run:
        actions.append({"action": "comment", "dry_run": True, "body": comment[:80]})
    else:
        r = subprocess.run(
            ["gh", "issue", "comment", str(issue_num), "--repo", repo, "--body", comment],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {"card": card["id"], "status": "failed",
                    "reason": "comment: %s" % r.stderr.strip()[:160]}
        actions.append({"action": "comment", "ok": True})

    # 2. Move to Review on the project board
    if project and issue_num:
        try:
            item_id = resolve_item_id(repo, issue_num, project["node"])
            if not item_id:
                actions.append({"action": "review", "status": "skipped",
                                "reason": "issue not in project"})
            elif dry_run:
                actions.append({"action": "review", "dry_run": True})
            else:
                r = subprocess.run(
                    ["gh", "api", "graphql",
                     "-f", "query=mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) {"
                           " updateProjectV2ItemFieldValue(input: {"
                           " projectId: $p, itemId: $i, fieldId: $f,"
                           " value: {singleSelectOptionId: $o}}) { projectV2Item { id } } }",
                     "-F", "p=%s" % project["node"],
                     "-F", "i=%s" % item_id,
                     "-F", "f=%s" % project["review_field"],
                     "-F", "o=%s" % project["review_option"]],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    return {"card": card["id"], "status": "failed",
                            "reason": "review: %s" % r.stderr.strip()[:160]}
                actions.append({"action": "review", "ok": True})
        except Exception as exc:  # review move is best-effort
            actions.append({"action": "review", "status": "skipped",
                            "reason": str(exc)[:120]})

    return {"card": card["id"], "status": "synced", "issue": issue_num,
            "actions": actions}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--config", type=Path, default=None,
                        help="dispatcher.yaml (for project review mapping)")
    parser.add_argument("--board", default=None, help="kanban board/tenant slug")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_HOME)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--archive", action="store_true",
                        help="archive done cards after successful sync "
                             "(keeps the board clean; default off for "
                             "backward compatibility)")
    parser.add_argument("--sync-ev", action="store_true",
                        help="sync EV verdicts ([EV] cards with a result) to "
                             "GitHub issue comments instead of the done-card "
                             "evidence sync")
    parser.add_argument("--gh-user", default=None,
                        help="(deprecated) 账号由环境变量 GH_USER_<ROLE> "
                             "预传递, 此参数仅作向后兼容")
    args = parser.parse_args()

    project = None
    if args.config:
        raw = yaml.safe_load(Path(args.config).read_text()) or {}
        repos = raw.get("repos") or {}
        # match by GitHub repo (owner/name) OR by local repo key
        repo_cfg = repos.get(args.repo)
        if repo_cfg is None:
            for _key, _cfg in repos.items():
                if isinstance(_cfg, dict) and _cfg.get("repo") == args.repo:
                    repo_cfg = _cfg
                    break
        if repo_cfg:
            projects = repo_cfg.get("projects") or []
            for p in projects:
                if p.get("review_field") and p.get("review_option"):
                    project = p
                    break

    cards = list_done_cards(args.board)
    state_file = args.state_dir / ("synced_%s.json" % args.repo.replace("/", "_"))
    synced = load_synced(state_file)
    results = []

    # EV 同步模式: 读 [EV] 卡 (含 result 裁决), 以 auditor 账号
    # (GH_USER_AUDITOR 环境变量预传递) 把裁决发到 GitHub issue comment
    # —— 账号即物证: 产出=worker 账号, 审计=auditor 账号
    if args.sync_ev:
        ev_cards = [c for c in list_all_cards(args.board)
                    if is_ev_card(c) and c.get("status") == "done"]
        gh_user = role_user("auditor")  # 环境变量预传递, 无默认账号 (fail-closed)
        for card in ev_cards:
            if card["id"] in synced:
                continue
            if not args.dry_run:
                switch_gh_user(gh_user)  # 切换 + 验证 (fail-closed)
            outcome = sync_ev_verdict(card, args.repo, dry_run=args.dry_run,
                                      gh_user=gh_user)
            results.append(outcome)
            if outcome["status"] == "synced" and not args.dry_run:
                synced.add(card["id"])
                if args.archive:
                    subprocess.run(["hermes", "kanban", "archive", card["id"]],
                                   capture_output=True, text=True, timeout=15)
                    outcome["archived"] = True
        if not args.dry_run:
            save_synced(state_file, synced)
        print(json.dumps({"cards": len(ev_cards), "new": len(results),
                          "results": results}, ensure_ascii=False, indent=2))
        return

    for card in cards:
        if card["id"] in synced:
            continue
        outcome = sync_one_card(card, args.repo, project, dry_run=args.dry_run)
        results.append(outcome)
        if outcome["status"] == "synced" and not args.dry_run:
            synced.add(card["id"])
            if args.archive:
                subprocess.run(["hermes", "kanban", "archive", card["id"]],
                               capture_output=True, text=True, timeout=15)
                outcome["archived"] = True
    if not args.dry_run:
        save_synced(state_file, synced)
    print(json.dumps({"cards": len(cards), "new": len(results),
                      "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
