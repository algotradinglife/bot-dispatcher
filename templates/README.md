# {{PROJECT_NAME}} 部署说明

三层管理体系：**GitHub（控制面）→ Kanban（执行面）→ PI Session（评审面）**。
本目录由 `bot-dispatcher/deploy.py` 生成（一键部署，参数化）。

## 部署资产

| 组件 | 位置 |
|---|---|
| 团队 repo | {{REPO}}（私有） |
| Project V2 | "{{PROJECT_TITLE}}" — 四态 Inbox/Ready/Review/Done |
| pr-status-sync | `.github/workflows/pr-status-sync.yaml` |
| dispatcher 配置 | `dispatcher.yaml`（本目录，不入库） |
| kanban board | `{{BOARD}}` |
| 观察 cron | 每 5 分钟（`{{KEY}}_tick.sh` → profile scripts/） |
| 治理模板 | `AGENTS.md` / `docs/ROADMAP.md`（入库） |

## 账号角色（硬纪律）

- **PI = hh1985**: 开契约 Issue、评审、merge、最终验收、**维护路线图**
- **worker = everything-bot-engineer**: 提交证据 PR、完成卡（执行者）
- **约束**: bot 绝不 merge；PI 决策动作必须由 hh1985 执行
- 切换: `gh auth switch --user hh1985|everything-bot-engineer`

## 日常派工流程（闭环）

**PI → worker → EV → PI review+merge → roadmap update**

1. PI 开 Issue（含验收标准）→ 加入 Board → 置 `Ready`
2. dispatcher（cron 5min）检测 → kanban 建卡（幂等）
3. **唯一 owner worker**（researcher/engineer 二选一）执行 → 完成卡 →
   提交证据 PR（body 含 `Closes #N`）
4. draft→ready → workflow 置 issue `Review`
5. **Alan（auditor）EV**：独立 fresh checkout 验证 → EV 裁决（PASS/REJECT）
6. PI 评审 → merge → 自动 `Done` + 关闭 issue
7. **PI 更新路线图**（`docs/ROADMAP.md`，v0_N 递增，追加更新记录）

**一 issue 一 worker 约束**：一个 issue 上 PI 只与一个 worker 交互；
跨 worker 协作必须拆独立 issue（researcher 完成 close 后另开新 issue
给 engineer）；**owner 不可变更**——与 issue 的 Project 归属严格绑定
（创建后不可改）；真需要改变（换 worker/换 Project）→ 重开 issue 落
对应 Project（引用原 issue 记录谱系），绝不修改已有 issue 的
owner/Project；EV 是独立审计环节不计入 PI 交互。

## 故障速查

| 症状 | 处理 |
|---|---|
| 卡没建 | 首次 tick 是 baseline；先 Inbox 再 flip Ready |
| workflow 0s fail | YAML 解析错（run 块缩进） |
| bot 无法 push | collaborator 权限 |
| 状态读不到 | gh auth switch 对账号 |
| 重部署 | 清 state-dir（dispatcher-state/）后重跑 dispatcher |
