#!/usr/bin/env python3
"""bot-dispatcher 一键部署工具 — 三层项目管理体系参数化初始化。

把 helixatlas 部署中沉淀的治理认知固化为可复用工具：给定项目参数，
自动完成 GitHub 控制面 + kanban 执行面 + 模板资产的全部初始化。

闭环 (治理约束, 见 templates/AGENTS.md):
    PI -> worker(researcher/engineer) -> EV(Alan) -> PI review+merge
    -> roadmap update
    一 issue 一 worker; owner 与 Project 归属严格绑定(不可变更);
    跨 worker 协作拆 issue; 改变 = 重开 issue 落对应 Project。

用法:
    python3 deploy.py --repo owner/name --key project-key \
        --board kanban-board --out /path/to/deploy-dir \
        [--pi-user hh1985] [--worker-user bot] \
        [--projects researcher:Researcher,engineer:Engineering] \
        [--dry-run]

步骤:
    1. 校验 gh 登录 + 必要 scope (repo, project, workflow)
    2. 创建/复用 GitHub Project V2 (四态 Inbox/Ready/Review/Done)
    3. 生成 pr-status-sync workflow (参数化 PROJECTS 块)
    4. 生成 dispatcher.yaml (session_map / assignee_map / mention_map)
    5. 生成 tick 脚本 (观察者 cron, 含 sync_job --archive)
    6. 初始化团队模板资产 (AGENTS.md / ROADMAP.md / README.md)

--dry-run: 只打印将执行的动作, 不产生任何外部副作用 (不调 gh、不写文件)。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TEMPLATES = REPO_ROOT / "templates"
WORKFLOW_SRC = REPO_ROOT / "workflows" / "pr-status-sync.yaml"

# 四态 (GitHub Project V2 single-select)
STATES = [
    ("Inbox", "GRAY", "planning, not yet dispatched"),
    ("Ready", "GREEN", "contract approved, ready to dispatch"),
    ("Review", "YELLOW", "evidence submitted, awaiting PI review"),
    ("Done", "PURPLE", "accepted and merged"),
]


class DeployError(RuntimeError):
    pass


def sh(args: list[str], *, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a subprocess with a clean env (never leak local stub PATH)."""
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    r = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    if check and r.returncode != 0:
        raise DeployError(f"command failed ({r.returncode}): {' '.join(args)}\n"
                          f"stderr: {r.stderr[:500]}")
    return r


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh(["gh", "api", *args], check=check)


# ── 1. 前置检查 ────────────────────────────────────────────────────────
def check_auth() -> dict:
    r = gh("user", "--jq", "{login: .login, id: .id}")
    user = json.loads(r.stdout)
    # scopes 检查: gh auth status 输出的 scopes 行, 用宽松匹配
    tok = sh(["gh", "auth", "status", "--show-token"]).stdout
    scopes = set()
    for ln in tok.splitlines():
        if "scopes:" in ln or "Scopes:" in ln:
            for s in ln.split(":", 1)[1].replace(",", " ").split():
                scopes.add(s.strip().strip("'\"").lower())
    if "repo" in scopes:
        scopes.add("repo")  # 兜底: 某些 gh 版本缩写
    missing = [s for s in ("repo", "project", "workflow") if s not in scopes]
    if missing:
        raise DeployError(
            f"gh 缺少 scope: {missing} (当前: {sorted(scopes)}). 运行: "
            f"gh auth refresh -h github.com -s {' -s '.join(missing)}")
    return user


# ── 2. Project V2 创建 + 四态 ──────────────────────────────────────────
def project_exists(owner: str, name: str, title: str) -> str | None:
    """Find an existing project by title (works for user or org owners)."""
    # Try user first, then org — projectV2s live on both, but the query shape
    # differs; use owner type probing instead.
    for typ in ("user", "organization"):
        q2 = (f'query($login: String!) {{ {typ}(login: $login) '
              f'{{ projectsV2(first: 50) {{ nodes {{ id title }} }} }} }}')
        r = gh("graphql", "-f", f"query={q2}", "-F", f"login={owner}")
        try:
            data = json.loads(r.stdout)
            nodes = (data.get("data", {}).get(typ, {})
                     .get("projectsV2", {}).get("nodes", []))
            for n in nodes:
                if n.get("title") == title:
                    return n["id"]
            return None  # owner type found; project not present
        except (json.JSONDecodeError, AttributeError, KeyError):
            continue  # wrong owner type, try next
    return None


def create_project(owner: str, name: str, title: str, dry: bool) -> str:
    if dry:
        return "PVT_DRY_RUN"
    q = ('mutation($login: String!, $title: String!) { '
         'createProjectV2(input: {ownerId: $login, title: $title}) { projectV2 { id } } }')
    r = gh("graphql", "-f", f"query={q}", "-F", f"login={owner}", "-f", f"title={title}")
    return json.loads(r.stdout)["data"]["createProjectV2"]["projectV2"]["id"]


def get_status_field(project_id: str, dry: bool) -> tuple[str, dict]:
    """Return (field_id, {option_name: option_id})."""
    if dry:
        return "PVTSSF_DRY", {s[0]: f"{s[0].upper()}_ID" for s in STATES}
    q = ('query($p: ID!) { node(id: $p) { ... on ProjectV2 { fields(first: 50) '
         '{ nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } } } } }')
    r = gh("graphql", "-f", f"query={q}", "-F", f"p={project_id}")
    nodes = json.loads(r.stdout)["data"]["node"]["fields"]["nodes"]
    singles = [n for n in nodes if isinstance(n, dict) and n.get("name")
               and isinstance(n.get("options"), list)]
    status = next((n for n in singles if n["name"] == "Status"), None)
    if status is None:
        raise DeployError("Project 没有 Status 字段")
    opts = {o["name"]: o["id"] for o in status["options"]}
    return status["id"], opts


def set_states(project_id: str, field_id: str, dry: bool) -> dict:
    """Ensure the four states exist; return {name: id}."""
    _, opts = get_status_field(project_id, dry)
    missing = [s for s, *_ in STATES if s not in opts]
    if missing and not dry:
        q = ('mutation($p: ID!, $f: ID!, $o: [ProjectV2SingleSelectFieldOptionInput!]!) '
             '{ updateProjectV2Field(input: {fieldId: $f, singleSelectOptions: $o}) '
             '{ projectV2Field { ... on ProjectV2SingleSelectField { id options { id name } } } } }')
        opts_in = [{"name": n, "color": c, "description": d}
                   for n, c, d in STATES]
        gh("graphql", "-f", f"query={q}", "-F", f"p={project_id}",
           "-F", f"f={field_id}", "-f", f"o={json.dumps(opts_in)}")
        _, opts = get_status_field(project_id, dry=False)
    return {s: opts[s] for s, *_ in STATES if s in opts}


def link_project_to_repo(project_id: str, repo_id: str, dry: bool) -> None:
    if dry:
        return
    q = ('mutation($p: ID!, $r: ID!) { linkProjectV2ToRepository(input: '
         '{projectId: $p, repositoryId: $r}) { repository { id } } }')
    gh("graphql", "-f", f"query={q}", "-F", f"p={project_id}", "-F", f"r={repo_id}")


# ── 3. workflow 生成 ───────────────────────────────────────────────────
def gen_workflow(project_id: str, field_id: str, opts: dict, repo: str,
                 out: Path, dry: bool) -> Path:
    """Fill the PROJECTS block in the workflow template."""
    src = WORKFLOW_SRC.read_text()
    block = (
        '          PROJECTS = {\n'
        f'              "{repo}": [\n'
        '                  {\n'
        f'                      "project": "{project_id}",\n'
        f'                      "field": "{field_id}",\n'
        '                      "options": {\n'
        f'                          "ready_for_review": "{opts["Review"]}",    # Review\n'
        f'                          "converted_to_draft": "{opts["Ready"]}",   # Ready\n'
        f'                          "closed": "{opts["Done"]}",                # Done\n'
        '                      },\n'
        '                  },\n'
        '              ],\n'
        '          }'
    )
    lines = src.split("\n")
    start = next(i for i, ln in enumerate(lines) if "PROJECTS = {" in ln)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "}" and i > start)
    out_lines = lines[:start] + block.split("\n") + lines[end + 1:]
    out_path = out / ".github" / "workflows" / "pr-status-sync.yaml"
    if not dry:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(out_lines))
        import yaml  # local import: only needed for real generation
        yaml.safe_load("\n".join(out_lines))  # validate
    return out_path


# ── 4. dispatcher.yaml 生成 ────────────────────────────────────────────
def gen_dispatcher_yaml(key: str, repo: str, board: str, project_id: str,
                        project_num: int, field_id: str, opts: dict,
                        out: Path, dry: bool) -> Path:
    body = f"""# {key} dispatcher 配置 — GitHub 控制面 → kanban 执行面
# 由 deploy.py 生成; 角色纪律见 templates/AGENTS.md
repos:
  {key}:
    repo: {repo}
    delivery_mode: kanban
    kanban_bin: hermes
    kanban_board: {board}
    projects:
      - number: {project_num}
        node: "{project_id}"
        name: "{key.title()} Board"
        owner: researcher
        review_field: "{field_id}"
        review_option: "{opts['Review']}"   # Review
    # worker 角色 → (board, assignee_profile)
    # researcher=Dr. Strange(文献/策略/模型/报告), engineer=Adam(全栈),
    # auditor=Alan(独立 EV, Engineering validation)
    session_map:
      pi: pi-profile            # PI 评审队列 (常驻 codex session 经原生通道拉取)
      pm: pm-profile            # 协调 (drwho)
      researcher: researcher    # Dr. Strange
      engineer: engineer        # Adam
      auditor: auditor          # Alan (EV)
    workflow_role: pm
    assignee_map:
      hh1985: pi
      everything-bot-engineer: engineer
    mention_map:
      pi: pi
      pm: pm
      research: researcher
      researcher: researcher
      engineering: engineer
      engineer: engineer
      audit: auditor
      auditor: auditor
"""
    p = out / "dispatcher.yaml"
    if not dry:
        p.write_text(body)
        import yaml
        yaml.safe_load(body)  # validate
    return p


# ── 5. tick 脚本生成 ───────────────────────────────────────────────────
def gen_tick(key: str, repo: str, board: str, deploy_dir: Path, dry: bool) -> Path:
    repo_name = repo.split("/")[-1]  # e.g. helixatlas (用于目录归属校验)
    script = f"""#!/bin/bash
# {key} dispatcher + sync loop — no_agent observer.
# 1) dispatcher: GitHub read -> kanban cards (never writes GitHub)
# 2) sync_job --archive: done cards -> evidence comment + Review -> archive
# 3) sync_job --sync-ev: EV 裁决以独立账号 (hh1985) 发到 GitHub (账号即物证)
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

GH=/opt/homebrew/bin/gh
BASE={deploy_dir.parent}
CFG=$BASE/{deploy_dir.name}/dispatcher.yaml
STATE=$BASE/{deploy_dir.name}/dispatcher-state
REPO={repo}
BOARD={board}
EXPECTED_REPO="{repo}"

# ── 角色账号 (环境变量预传递, 调用方注入, 无默认值) ──
GH_USER_PI="${{GH_USER_PI:-}}"
GH_USER_AUDITOR="${{GH_USER_AUDITOR:-}}"
if [ -z "$GH_USER_PI" ] || [ -z "$GH_USER_AUDITOR" ]; then
  echo "⚠️ 账号缺失: 必须预传递 GH_USER_PI 和 GH_USER_AUDITOR 环境变量"
  exit 1
fi

# ── 守卫 1: 当前账号必须是 PI 账号 — dispatcher/sync 是观察+汇报通道 ──
ACTUAL_USER=$($GH api user --jq .login 2>/dev/null || echo "unknown")
if [ "$ACTUAL_USER" != "$GH_USER_PI" ]; then
  echo "⚠️ 账号守卫失败: 期望 $GH_USER_PI, 实际 $ACTUAL_USER — 拒绝运行"
  exit 1
fi

# ── 守卫 2: 工作目录归属校验 — 防止在错误 repo 执行 git 操作 ──
if [ -d "$BASE" ] && [ -d "$BASE/.git" ]; then
  ACTUAL_REPO=$(git -C "$BASE" remote get-url origin 2>/dev/null || echo "no-remote")
  case "$ACTUAL_REPO" in
    *"{repo}"*|*"{repo_name}"*) : ;;
    *)
      echo "⚠️ 目录守卫失败: $BASE remote ($ACTUAL_REPO) ≠ $EXPECTED_REPO — 拒绝运行"
      exit 1
      ;;
  esac
fi

$GH auth switch --user hh1985 >/dev/null 2>&1

OUT=$(cd $BASE && python3 bot-dispatcher/dispatcher.py \\
  --repo {key} --config $CFG --state-dir $STATE 2>&1)

SYNC_OUT=$(cd $BASE && python3 bot-dispatcher/sync_job.py \\
  --repo $REPO --config $CFG --board $BOARD --state-dir $STATE --archive 2>&1)

EV_OUT=$(cd $BASE && GH_USER_AUDITOR="$GH_USER_AUDITOR" python3 bot-dispatcher/sync_job.py \\
  --repo $REPO --config $CFG --board $BOARD --state-dir $STATE \\
  --sync-ev --archive 2>&1)

python3 - "$OUT" "$SYNC_OUT" "$EV_OUT" <<'PY'
import json, sys

lines = []
disp_raw, sync_raw, ev_raw = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    d = json.loads(disp_raw)
    sent = [a for a in d.get('actions', []) if a.get('state') == 'sent']
    warns = d.get('warnings', [])
    if sent:
        lines.append('🤖 {{}}: %d 张卡已投递'.format(len(sent)))
        for a in sent:
            lines.append('  - %s → %s' % (a.get('session'), a.get('result', '')[:40]))
    if warns:
        lines.append('⚠️ %d 条警告'.format(len(warns)))
        for w in warns[:3]:
            lines.append('  ! %s' % w[:80])
except Exception:
    pass

try:
    s = json.loads(sync_raw)
    if s.get('new'):
        lines.append('🔄 %d 张完成卡已同步 + 归档'.format(len(s['results'])))
        for r in s['results']:
            if r.get('status') == 'synced':
                lines.append('  - issue #%s → %s %s' % (
                    r.get('issue'), [a.get('action') for a in r.get('actions', [])],
                    '🗂' if r.get('archived') else ''))
except Exception:
    pass

try:
    e = json.loads(ev_raw)
    if e.get('new'):
        lines.append('🔍 %d 条 EV 裁决已同步 (hh1985)'.format(len(e['results'])))
        for r in e['results']:
            if r.get('status') == 'synced':
                lines.append('  - issue #%s: %s %s' % (
                    r.get('issue'), r.get('verdict', ''),
                    '🗂' if r.get('archived') else ''))
except Exception:
    pass

if lines:
    print('\\n'.join(lines))
PY
"""
    p = deploy_dir / f"{key}_tick.sh"
    if not dry:
        p.write_text(script)
        p.chmod(0o755)
    return p


# ── main ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--key", required=True, help="dispatcher repo key (e.g. helixatlas)")
    ap.add_argument("--board", required=True, help="kanban board slug")
    ap.add_argument("--out", required=True, help="deploy dir (e.g. ~/workspace/xxx-deploy)")
    ap.add_argument("--project-title", default=None,
                    help="Project V2 title (default: '<key> Board')")
    ap.add_argument("--dry-run", action="store_true", help="no external side effects")
    args = ap.parse_args()

    owner, _, name = args.repo.partition("/")
    if not owner or not name:
        print("--repo 必须是 owner/name", file=sys.stderr)
        return 2
    title = args.project_title or f"{args.key.title()} Board"
    out = Path(args.out).expanduser().resolve()
    dry = args.dry_run

    print(f"═══ deploy: {args.repo} (key={args.key}, board={args.board}) "
          f"{'(DRY RUN)' if dry else ''} ═══")

    # 1. auth (dry-run 跳过 gh 调用, 纯预览)
    if dry:
        print("[1] (dry-run) gh 登录检查跳过")
    else:
        user = check_auth()
        print(f"[1] gh 登录: {user['login']} (repo/project/workflow scope OK)")

    # 2. repo id
    if dry:
        repo_id = "R_DRY_RUN"
    else:
        repo_id = gh(f"repos/{args.repo}", "--jq", ".node_id").stdout.strip()
    print(f"[2] repo node: {repo_id}")

    # 3. project
    pid = project_exists(owner, name, title) if not dry else None
    if pid:
        print(f"[3] 复用 Project: {title} ({pid})")
    else:
        pid = create_project(owner, name, title, dry)
        print(f"[3] 创建 Project: {title} ({pid})")
    link_project_to_repo(pid, repo_id, dry)
    print(f"    linked to repo: {args.repo}")

    # 4. status field 四态
    field_id, opts = get_status_field(pid, dry)
    opts = set_states(pid, field_id, dry)
    print(f"[4] Status 四态: {list(opts.keys())}")

    # 5. workflow
    wf = gen_workflow(pid, field_id, opts, args.repo, out, dry)
    print(f"[5] workflow -> {wf}")

    # 6. dispatcher.yaml
    project_num = 2  # deploy 用独立 project; number 由用户实际确认
    cfg = gen_dispatcher_yaml(args.key, args.repo, args.board, pid, project_num,
                              field_id, opts, out, dry)
    print(f"[6] dispatcher.yaml -> {cfg}")

    # 7. tick
    tk = gen_tick(args.key, args.repo, args.board, out, dry)
    print(f"[7] tick -> {tk}")

    # 8. 模板资产 (AGENTS/ROADMAP/README) 拷贝
    for tpl in ("AGENTS.md", "README.md"):
        src = TEMPLATES / tpl
        if src.exists():
            dst = out / tpl
            if not dry:
                dst.write_text(src.read_text())
            print(f"[8] template {tpl} -> {dst}")
    rm = TEMPLATES / "ROADMAP.md"
    if rm.exists():
        dst = out / "ROADMAP.md"
        if not dry:
            dst.write_text(rm.read_text())
        print(f"[8] template ROADMAP.md -> {dst}")

    print("═══ deploy done ═══" if not dry else "═══ dry-run preview (no side effects) ═══")
    if dry:
        print("后续手动步骤: gh secret set PROJECT_SYNC_TOKEN; "
              "hermes kanban boards create; cron 注册 tick")
    return 0


if __name__ == "__main__":
    sys.exit(main())
