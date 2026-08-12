#!/usr/bin/env python3
"""Kanban bridge v2 — map GitHub eight-state transitions to kanban cards.

Called by the tick post-processor after the dispatcher emits actions.
Design (2026-08-11 retrospective, codex-reviewed):
- GitHub = decision layer / SSOT (unchanged); kanban = execution layer.
- 1 issue = 1 EXECUTION card (owner) + 1 EV card (auditor), BOTH FIXED.
- Rework = new run on the SAME cards (generation++), NOT new cards.
  This eliminates: concurrent EV audits, branch/worktree conflicts,
  card pileup, and manual status flips.
- Worker writes GitHub status itself (SOUL contract); kanban manages the
  execution lifecycle (heartbeat / retry / reclaim via gateway dispatcher).
- Context continuity: worker records session_id at delivery; rework run
  resumes via `hermes --resume <session_id>`; falls back to handoff doc.

Card lifecycle (status moves, no done until final):
  exec card:  ready → running (worker claim) → blocked「waiting EV」
              → ready (EV REJECT rework) → ... → done (final PASS + PI done)
  ev card:    ready → running (auditor claim) → blocked「waiting rework」
              → ready (rework done) → ... → done (PASS)
"""
import json
import os
import re
import subprocess
import sys
import time

BOARD = os.environ.get("KANBAN_BOARD", "beijing-lot")
EXEC_TAG = "EXEC_CARD"
EV_TAG = "EV_CARD"
GEN_RE = re.compile(r"GENERATION=(\d+)")


def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r


def list_tasks():
    r = run(["hermes", "kanban", "list", "--json"])
    if r.returncode != 0:
        return []
    try:
        d = json.loads(r.stdout)
    except Exception:
        return []
    return d if isinstance(d, list) else d.get("tasks", d.get("items", []))


def find_cards(issue_num, tag=None):
    """Find cards bound to a GitHub issue. tag filters EXEC_CARD / EV_CARD."""
    out = []
    for t in list_tasks():
        body = (t.get("body") or "") + " " + (t.get("title") or "")
        if "#%d" % issue_num not in body and "issue %d" % issue_num not in body.lower():
            continue
        if tag and tag not in body:
            continue
        out.append(t)
    return out


SESSION_RE = re.compile(r"\[SESSION\]\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)


def get_session_id(issue_num):
    """Read the worker's LAST [SESSION] marker from issue comments (newest wins).

    Only trusted worker-auditor comments count (same account as EV verdicts —
    everything-bot-engineer); an untrusted comment (e.g. PI's) cannot override
    the worker's real session. Comments are read in reverse (newest first).
    Raises ControlPlaneUnavailable on read failure (fail-closed).
    """
    from pi_gates import TRUSTED_AUDITORS

    r = run(["gh", "issue", "view", str(issue_num),
             "--repo", "algotradinglife/beijing-lot",
             "--json", "comments"])
    if r.returncode != 0:
        raise ControlPlaneUnavailable(
            "gh issue view failed: %s" % r.stderr.strip()[:120])
    try:
        comments = json.loads(r.stdout).get("comments", [])
    except Exception as e:
        raise ControlPlaneUnavailable("comments parse error: %s" % e)
    for c in reversed(comments):
        if (c.get("author") or {}).get("login") not in TRUSTED_AUDITORS:
            continue
        m = SESSION_RE.search(c.get("body") or "")
        if m:
            return m.group(1)
    return None


class ControlPlaneUnavailable(Exception):
    """GitHub 读取失败 — 调用方必须 fail-closed, 不得按"无裁决"处理."""


def get_rework_type(issue_num):
    """Read the latest EV verdict's REWORK_TYPE from issue comments.

    Returns ('method'|'minor', verdict_sha) / None. Uses the same structured
    [EV-VERDICT] block parsing + trusted-auditor + global-timestamp ordering
    as pi_gates (single source of truth for EV verdicts).

    - latest verdict is REJECT → its REWORK_TYPE (missing → 'method' fresh)
    - latest verdict is PASS / no verdicts → None (not a rework round)
    - GitHub read failure → raises ControlPlaneUnavailable (fail-closed)
    """
    from pi_gates import parse_ev_verdicts, TRUSTED_AUDITORS

    r = run(["gh", "issue", "view", str(issue_num),
             "--repo", "algotradinglife/beijing-lot",
             "--json", "comments"])
    if r.returncode != 0:
        raise ControlPlaneUnavailable(
            "gh issue view failed: %s" % r.stderr.strip()[:120])
    try:
        comments = json.loads(r.stdout).get("comments", [])
    except Exception as e:
        raise ControlPlaneUnavailable("comments parse error: %s" % e)

    # 收集可信 auditor 评论里的 EV-VERDICT 块
    verdicts = []
    for c in comments:
        if (c.get("author") or {}).get("login") not in TRUSTED_AUDITORS:
            continue
        for d in parse_ev_verdicts(c.get("body") or ""):
            d["created_at"] = c.get("createdAt", "")
            verdicts.append(d)
    if not verdicts:
        return None
    # 全局时间序取最新裁决: 只按真实评论 createdAt 排序 (与 pi_gates 一致;
    # 正文 timestamp 不可信, 且同一评论多块时 createdAt 相同)
    verdicts.sort(key=lambda v: v.get("created_at", ""))
    latest = verdicts[-1]
    if latest.get("verdict", "").upper() != "REJECT":
        return None
    # REWORK_TYPE 是块内字段 (EV_FIELD_RE 解析成 rework_type)
    rw = (latest.get("rework_type") or "").lower()
    if rw not in ("method", "minor"):
        rw = "method"  # REJECT 无标记 → 保守 fresh
    return rw, latest.get("sha", "")


def exec_card_prompt(issue_num, title, url, owner):
    """Build the exec-card task prompt.

    RESUME_SESSION injection respects REWORK_TYPE (2026-08-12 rule):
    - method REJECT → NO injection (fresh rework, only contract + EV list)
    - minor REJECT → inject (resume saves time)
    - no REJECT (first round) → no SESSION exists, nothing to inject
    - GitHub read failure → fail-closed: no injection (never resume on
      unknown state; a wrong resume violates method-fresh)
    """
    try:
        rw_ret = get_rework_type(issue_num)
    except ControlPlaneUnavailable as e:
        print("⚠️ kanban bridge: 控制面读取失败, 按 FRESH 处理: %s" % e,
              file=sys.stderr)
        rw_ret = ("method", "")  # fail-closed: 未知状态 → fresh
    rw = rw_ret[0] if rw_ret else None
    prompt = ("/goal Issue #%d is READY — %s\n%s\n"
              "GitHub workflow: 认领置 In Progress → 执行 → 交付 PR → 置 EV Review. "
              "交付时评论附 [SESSION] <session_id> + HANDOFF 摘要." % (issue_num, title, url))
    if rw is None:
        # 首次 Ready (无裁决): 无 SESSION 可注入, 直接返回 (不读 session)
        return prompt
    if rw == "method":
        # fresh: 不 resume, 只带契约 + EV REJECT 清单 + 旧代码 (2026-08-12 规则)
        prompt = ("REWORK_TYPE=method — FRESH 返工: 不 resume 旧会话. "
                  "只带: ① 完整 issue 契约 (REQ + method.yaml) ② EV REJECT 清单 "
                  "(缺陷 + 同类排查要求) ③ 上一轮代码 (参考物, 非'我的作品').\n") + prompt
        return prompt
    try:
        sid = get_session_id(issue_num)
    except ControlPlaneUnavailable as e:
        # minor 需要 resume 但读不到 session → 降级 FRESH (fail-closed)
        print("⚠️ kanban bridge: 读取 [SESSION] 失败, 降级 FRESH: %s" % e,
              file=sys.stderr)
        prompt = ("REWORK_TYPE=method — FRESH 返工: 不 resume 旧会话. "
                  "只带: ① 完整 issue 契约 (REQ + method.yaml) ② EV REJECT 清单 "
                  "(缺陷 + 同类排查要求) ③ 上一轮代码 (参考物, 非'我的作品').\n") + prompt
        return prompt
    if sid and rw == "minor":
        prompt = ("RESUME_SESSION=%s\n" % sid) + prompt
    return prompt


def generation_of(card):
    body = (card.get("body") or "") + " " + (card.get("title") or "")
    m = GEN_RE.search(body)
    return int(m.group(1)) if m else 0


def get_or_create_card(issue_num, title, url, kind, owner):
    """Return the fixed card for (issue, kind); create if absent.

    kind: "exec" (owner worker) or "ev" (auditor). Idempotent — one card
    per issue per kind, reused across rework rounds (generation bumps).
    """
    tag = EXEC_TAG if kind == "exec" else EV_TAG
    cards = find_cards(issue_num, tag)
    if cards:
        return cards[0]

    assignee = owner if kind == "exec" else "auditor"
    workspace = "worktree" if kind == "exec" else "scratch"
    branch = ("%s/bj%d-issue" % (owner, issue_num)) if kind == "exec" else None

    if kind == "exec":
        # 卡 body = worker 的任务 prompt; 注入 RESUME_SESSION 恢复上下文
        card_body = exec_card_prompt(issue_num, title, url, owner)
        card_body += "\n%s GENERATION=1" % tag
    else:
        card_body = ("EV audit for GitHub issue #%d (%s) — %s\n%s GENERATION=1"
                     % (issue_num, title, url, tag))

    cmd = ["hermes", "kanban", "create",
           "Issue #%d: %s [%s GENERATION=1]" % (issue_num, title, tag),
           "--body", card_body,
           "--assignee", assignee,
           "--workspace", workspace,
           "--idempotency-key", "gh-issue-%d-%s" % (issue_num, tag.lower())]
    if branch:
        cmd += ["--branch", branch]
    r = run(cmd + ["--json"])
    if r.returncode != 0:
        print("⚠️ kanban create %s card failed: %s" % (kind, r.stderr[:150]),
              file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def bump_generation(card, reason):
    """Increment GENERATION in the card title/body (rework round marker)."""
    card_id = card.get("id")
    cur = generation_of(card)
    new = cur + 1
    tag = EXEC_TAG if EXEC_TAG in (card.get("body") or "") else EV_TAG
    title = re.sub(r"GENERATION=\d+", "GENERATION=%d" % new,
                   card.get("title") or "")
    body = re.sub(r"GENERATION=\d+", "GENERATION=%d" % new,
                  card.get("body") or "")
    r = run(["hermes", "kanban", "edit", card_id,
             "--result", "%s (generation %d->%d)" % (reason, cur, new),
             "--metadata", json.dumps({"generation": new})])
    if r.returncode != 0:
        print("⚠️ kanban bump generation %s failed: %s"
              % (card_id, r.stderr[:120]), file=sys.stderr)


def set_status(card_id, action, reason=""):
    """Move a card's status: unblock (→ready) / block (→blocked)."""
    if action == "unblock":
        r = run(["hermes", "kanban", "unblock", card_id, "--reason", reason])
    elif action == "block":
        # block 的 reason 是位置参数 (usage: block task_id [reason ...])
        args = ["hermes", "kanban", "block", card_id]
        if reason:
            args.append(reason)
        r = run(args)
    else:
        return
    if r.returncode != 0:
        print("⚠️ kanban %s %s failed: %s"
              % (action, card_id, r.stderr[:120]), file=sys.stderr)


def complete_card(card_id):
    r = run(["hermes", "kanban", "complete", card_id])
    if r.returncode != 0:
        print("⚠️ kanban complete %s failed: %s" % (card_id, r.stderr[:120]),
              file=sys.stderr)


REWORK_ID_RE = re.compile(r"REWORK_ID=([0-9a-f]{40})(?![0-9a-f])")


def comment_card(card_id, text, idem_key):
    """Append a comment to a kanban card (visible to worker when it opens card).

    Idempotent per REWORK_ID: if the card already has a comment carrying the
    same idem_key (verdict sha), skip — a later rework round (new sha) still
    appends its own directive. Returns True when the directive is present
    (either pre-existing or newly posted).
    """
    # 幂等检查: kanban show --json 读卡评论, 解析 REWORK_ID 等值比较 (非子串)
    s = run(["hermes", "kanban", "show", "--json", card_id])
    if s.returncode == 0:
        try:
            card = json.loads(s.stdout)
        except Exception:
            card = {}
        for c in (card.get("comments") or []):
            m = REWORK_ID_RE.search(c.get("body") or "")
            if idem_key and m and m.group(1) == idem_key:
                return True  # 本裁决轮次指令已存在 → 不重复
    r = run(["hermes", "kanban", "comment", card_id, text])
    if r.returncode != 0:
        print("⚠️ kanban comment %s failed: %s" % (card_id, r.stderr[:120]),
              file=sys.stderr)
        return False
    return True


def rework_directive(issue_num):
    """Build the rework directive text for an existing exec card, or None.

    Card bodies are immutable after creation (kanban edit is done-only), so a
    rework round on an EXISTING card must carry the REWORK_TYPE decision via
    a card comment instead. Returns None when not a rework round.

    The directive embeds REWORK_ID=<verdict sha> as the idempotency key, so
    a NEW rework round (new verdict sha) is not suppressed by an old one.

    Raises ControlPlaneUnavailable on GitHub read failure (caller decides
    whether to fail closed; a directive must never be guessed).
    """
    rw_ret = get_rework_type(issue_num)
    if rw_ret is None:
        return None
    rw, sha = rw_ret
    idem = "REWORK_ID=%s\n" % sha if sha else ""
    if rw == "method":
        return (idem + "REWORK_TYPE=method — FRESH 返工: 不 resume 旧会话. "
                "只带: ① 完整 issue 契约 (REQ + method.yaml) ② EV REJECT 清单 "
                "(缺陷 + 同类排查要求) ③ 上一轮代码 (参考物, 非'我的作品').")
    sid = get_session_id(issue_num)
    if not sid:
        # minor 需要 resume 但读不到 session → fail-closed 降级为 method
        print("⚠️ kanban bridge: minor 返工但读不到 [SESSION], 降级 FRESH",
              file=sys.stderr)
        return (idem + "REWORK_TYPE=method — FRESH 返工: 不 resume 旧会话. "
                "只带: ① 完整 issue 契约 (REQ + method.yaml) ② EV REJECT 清单 "
                "(缺陷 + 同类排查要求) ③ 上一轮代码 (参考物, 非'我的作品').")
    return (idem + "REWORK_TYPE=minor — 可 resume 上一轮会话续接: "
            "RESUME_SESSION=%s (省时)." % sid)


def _issue_status_allows_rework(issue_num):
    """重读 GitHub issue 当前状态, 判断是否仍允许返工放行.

    CAS 式确认 (防跨 tick 竞态): 仅 Ready / In Progress 才允许 unblock。
    EV Review / PI Review / Done / Blocked / Human → 不放行。
    读取失败 → False (fail-closed, 不 unblock)。
    """
    from finalize_delivery import get_project_item
    try:
        _, _, status, err = get_project_item("beijing-lot", issue_num)
    except Exception as e:
        print("⚠️ kanban bridge: 状态重读失败, 不 unblock: %s" % e,
              file=sys.stderr)
        return False
    if err:
        print("⚠️ kanban bridge: 状态重读失败, 不 unblock: %s" % err,
              file=sys.stderr)
        return False
    return status in ("Ready", "In Progress")


def handle(action):
    reason = action.get("reason", "")
    node = action.get("node", "")
    state = action.get("state", "")
    role = action.get("role") or "analyst"
    sent = action.get("sent", "")

    parts = node.split(":")
    if len(parts) < 3 or parts[0] != "project":
        return
    try:
        issue_num = int(parts[2])
    except ValueError:
        return

    url = "https://github.com/algotradinglife/beijing-lot/issues/%d" % issue_num
    title = state

    # ── Ready: ensure exec card exists, EV card exists (both fixed) ──
    if reason == "issue_ready":
        exec_cards_before = find_cards(issue_num, EXEC_TAG)
        get_or_create_card(issue_num, title, url, "exec", role)
        get_or_create_card(issue_num, title, url, "ev", "auditor")
        # 卡已存在 (返工轮) → 卡 body 不可改, 用卡评论传递 REWORK_TYPE 决策
        if exec_cards_before:
            try:
                directive = rework_directive(issue_num)
            except ControlPlaneUnavailable as e:
                print("⚠️ kanban bridge: 读取裁决失败, 不追加返工指令: %s" % e,
                      file=sys.stderr)
                directive = None
            if directive:
                # 幂等键 = 裁决 sha (REWORK_ID=...), 第二轮返工不受旧评论抑制
                m = re.search(r"REWORK_ID=([0-9a-f]{40})", directive)
                idem_key = m.group(1) if m else ""
                for c in find_cards(issue_num, EXEC_TAG):
                    posted = comment_card(c.get("id"), directive, idem_key)
                    # REJECT 回 Ready: exec 卡此前被 block「waiting EV」,
                    # 指令就位后才 unblock (评论失败 → 保持 blocked, worker 不启动)
                    if posted and c.get("status") == "blocked":
                        # CAS 式确认: 重读 GitHub issue 当前状态, 仅 Ready/In Progress
                        # 才 unblock — 防旧 tick 与 ev_review_ready tick 重叠时,
                        # 在 EV Review 后误放行 worker (跨 tick 竞态)
                        if _issue_status_allows_rework(issue_num):
                            set_status(c.get("id"), "unblock",
                                       "rework round (issue #%d back to Ready)" % issue_num)

    # ── EV Review: worker delivered → exec card blocked「waiting EV」,
    #    EV card unblocked (auditor re-audits if rework round). ──
    elif reason in ("ev_review_ready", "issue_ev_review"):
        for c in find_cards(issue_num, EXEC_TAG):
            set_status(c.get("id"), "block", "worker delivered, waiting EV")
        for c in find_cards(issue_num, EV_TAG):
            set_status(c.get("id"), "unblock", "EV audit round")

    # ── PI Review: EV PASS → EV card blocked「PASS, waiting PI」 ──
    elif reason == "issue_pi_review":
        for c in find_cards(issue_num, EV_TAG):
            set_status(c.get("id"), "block", "EV PASS, waiting PI terminal review")

    # ── Done: final — complete both cards ──
    elif reason == "issue_done":
        for c in find_cards(issue_num):
            complete_card(c.get("id"))

    # ── Blocked: worker asks PI → exec card blocked ──
    elif reason == "issue_blocked":
        for c in find_cards(issue_num, EXEC_TAG):
            set_status(c.get("id"), "block", "worker blocked on GitHub (issue #%d)" % issue_num)

    # ── Human: needs real person → both cards blocked ──
    elif reason == "issue_human_escalate":
        for c in find_cards(issue_num):
            set_status(c.get("id"), "block", "human intervention needed (issue #%d)" % issue_num)

    # ── In Progress: worker claimed (GitHub In Progress) → ensure exec
    #    card is unblocked so dispatcher can spawn/continue it. ──
    elif reason == "issue_in_progress":
        for c in find_cards(issue_num, EXEC_TAG):
            if c.get("status") == "blocked":
                set_status(c.get("id"), "unblock", "worker resumed (GitHub In Progress)")


def main():
    raw = sys.stdin.read().strip()
    if not raw.startswith("{"):
        return
    try:
        d = json.loads(raw)
    except Exception:
        return
    for a in d.get("actions", []):
        try:
            handle(a)
        except Exception as exc:
            print("⚠️ kanban bridge: %s" % exc, file=sys.stderr)


if __name__ == "__main__":
    main()
