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
    5. 生成 tick 脚本 (观察者 cron, 只跑 dispatcher)
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

# 七态 (GitHub Project V2 single-select)
#   In Progress = 执行态: kanban 卡 running (执行开始) → 同步置位
#   Blocked     = worker 诉求通道: worker 置 Blocked → dispatcher 升级 PI
#   Human       = PI 判定需真人干预 (超出 AI 循环): 终局性暂停, 等真人处理
STATES = [
    ("Inbox", "GRAY", "planning, not yet dispatched"),
    ("Ready", "GREEN", "contract approved, ready to dispatch"),
    ("In Progress", "BLUE", "executing: kanban card running (synced)"),
    ("EV Review", "YELLOW", "worker submitted evidence, awaiting independent auditor EV"),
    ("PI Review", "ORANGE", "EV passed, awaiting PI acceptance"),
    ("Blocked", "RED", "worker request: needs PI decision or unblock"),
    ("Human", "ORANGE", "PI judged: needs human intervention (beyond AI loop)"),
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
        q = ('mutation($f: ID!, $o: [ProjectV2SingleSelectFieldOptionInput!]!) '
             '{ updateProjectV2Field(input: {fieldId: $f, singleSelectOptions: $o}) '
             '{ projectV2Field { ... on ProjectV2SingleSelectField { id options { id name } } } } }')
        opts_in = [{"name": n, "color": c, "description": d}
                   for n, c, d in STATES]
        # gh -f/-F 无法正确传 [Input!]! list → 用 --input 传完整 JSON body
        body = json.dumps({"query": q,
                           "variables": {"f": field_id, "o": opts_in}})
        env = dict(os.environ)
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        r = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                           input=body, capture_output=True, text=True,
                           timeout=60, env=env)
        if r.returncode != 0:
            raise DeployError("set_states failed: %s" % r.stderr.strip()[:300])
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
        f'                          "ready_for_review": "{opts["EV Review"]}",    # EV Review\n'
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
    body = f"""# {key} dispatcher 配置 — GitHub 控制面 (v0_3: 纯通知+监控)
# 由 deploy.py 生成; 角色纪律见 templates/AGENTS.md
repos:
  {key}:
    repo: {repo}
    projects:
      - number: {project_num}
        node: "{project_id}"
        name: "{key.title()} Board"
        owner: researcher
        review_field: "{field_id}"
        # 八态 option id (v0_3 契约层)
        status_options:
{chr(10).join('          %s: "%s"' % (n, opts[n]) for n in ("Inbox", "Ready", "In Progress", "EV Review", "PI Review", "Blocked", "Human", "Done") if n in opts)}
    # 角色 → 通知目标 (v0_3: PI 不接收通知, 主动轮询)
    # researcher=Dr. Strange(文献/策略/模型/报告), engineer=Adam(全栈),
    # auditor=Alan(独立 EV, Engineering validation)
    session_map:
      pi: pi-profile            # PI (远端 codex, 主动轮询 GitHub, 不接收通知)
      researcher: researcher    # Dr. Strange
      engineer: engineer        # Adam
      auditor: auditor          # Alan (EV)
    assignee_map:
      hh1985: pi
      everything-bot-engineer: engineer
"""
    p = out / "dispatcher.yaml"
    if not dry:
        p.write_text(body)
        import yaml
        yaml.safe_load(body)  # validate
    return p


# ── 5. tick 脚本生成 ───────────────────────────────────────────────────
def gen_tick(key: str, repo: str, board: str, deploy_dir: Path, dry: bool) -> Path:
    script = f"""#!/bin/bash
# {key} dispatcher observer tick — no_agent watchdog.
# v0_3: dispatcher 纯通知+监控 — 读 GitHub 状态, 输出通知事件, 不写 GitHub.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

GH=/opt/homebrew/bin/gh
BASE={deploy_dir.parent}
CFG=$BASE/{deploy_dir.name}/dispatcher.yaml
STATE=$BASE/{deploy_dir.name}/dispatcher-state

# ── 账号守卫: 观察者只读, 不写 GitHub — 用 PI 账号读 (物证: 只读无副作用) ──
ACTUAL_USER=$($GH api user --jq .login 2>/dev/null || echo "unknown")
if [ -z "${{ACTUAL_USER:-}}" ] || [ "$ACTUAL_USER" = "unknown" ]; then
  echo "⚠️ gh 不可用 (账号未登录) — 跳过本轮"
  exit 1
fi

OUT=$(cd $BASE && python3 bot-dispatcher/dispatcher.py \\
  --repo {key} --config $CFG --state-dir $STATE 2>&1)

python3 - "$OUT" <<'PY'
import json, sys

lines = []
try:
    d = json.loads(sys.argv[1])
    # v0_3: 通知事件 (role: worker/auditor/user)
    for n in d.get('notifications', []):
        role = n.get('role', '?')
        icon = {{'worker': '🛠', 'auditor': '🔍', 'user': '⛔'}}.get(role, '•')
        lines.append('%s [%s] %s' % (icon, role, n.get('message', '')[:100]))
    for w in d.get('warnings', [])[:3]:
        lines.append('⚠️ %s' % w[:80])
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
