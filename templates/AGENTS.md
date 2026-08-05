# Agent Governance — {{PROJECT_NAME}}

## Repository purpose

{{PROJECT_NAME}} 团队的项目管理仓库。GitHub 是决策态（契约 + 生命周期状态），
kanban 是执行态（工作卡），单向映射、不双写。

## 闭环流程

**PI → worker → EV → PI review + merge → roadmap update**

1. PI 开 Issue（含验收标准）→ 加入 Project → 置 `Ready`
2. dispatcher（cron）检测 → kanban 建卡（幂等于 issue 号）
3. 唯一 owner worker（researcher/engineer 二选一）执行 → 完成卡 →
   提交证据 PR（body 含 `Closes #N`）
4. draft→ready → pr-status-sync workflow 置 issue `Review`
5. EV（auditor=Alan）：独立 fresh checkout 验证 → **含代码质量与数据工程
   检查**（数据 IO 用 pandas/numpy 非原生 csv、写前 dry-run 预检、读入
   fail-fast 校验、原子写）→ EV 裁决（PASS/REJECT）
6. PI 评审 → merge → 自动 `Done` + 关闭 issue
7. PI 更新路线图（`docs/ROADMAP.md`，v0_N 递增，追加更新记录）

## Roles

- **PI** owns routing policy, business and research decisions, final domain
  acceptance, and deployment authorization. PI 决策动作（评审、merge、验收）
  必须由 PI 账号执行。
- **PI 维护路线图**（`docs/ROADMAP.md`）：路线图是 PI 的职责，worker 不做
  方案设计。每次 worker 产出评审 merge 后，PI 必须更新路线图（将已验证的
  产出纳入 §2 当前路线 / §3 已验证资产，追加 §5 更新记录）。版本 v0_N
  递增，更新记录不抹除历史，条目必须可追溯到产出证据（issue/PR/报告）。
- **worker 类型**（通用资产，绑定各自 profile）：
  - `researcher` (Dr. Strange)：文献调研、策略探索、模型建立与优化、
    分析报告撰写。**不做方案设计/路线图**（路线图由 PI 画）。
  - `engineer` (Adam)：全栈工程师——数据库、网站网页、性能优化、运维。
  - `auditor` (Alan)：独立审计员，**Engineering validation**——独立重跑
    验证、证据链审计、边界检查、**代码质量与数据工程检查**（assignee ≠
    产出方）。
- **PM** (drwho) owns operational coordination: lifecycle tracking,
  dependency follow-up, review scheduling, merge/closure follow-up, and
  dispatcher exception handling. PM escalates business decisions to PI and
  does not decide them independently.
- **Dispatcher** is execution-only: reports and routes configured state; it
  does not invent dependency or lifecycle relationships. Never writes state.

## 一 issue 一 worker（PI 交互约束）

- **一个 issue 上，PI 只与一个 worker 交互**：issue 的 owner 唯一
  （researcher 或 engineer 二选一，由 issue 契约/Project 归属决定）。
- **owner 不可变更**：owner 与 issue 的 Project 归属**严格绑定**——
  issue 落在哪个 Project，owner 就是该 Project 的 owner，创建后不可改。
- PI 评审时只面对该 worker 的产出；跨 worker 协作**必须拆成独立 issue**：
  - researcher 探索完成 → merge/close → 另开新 issue（引用前一 issue）
    再派给 engineer 实现。
  - 禁止在同一 issue 上让两个 worker 轮番产出、都直接与 PI 交互。
- **真需要改变（换 worker / 换 Project）→ 重开 issue**：close 原 issue，
  新开 issue 落在对应 Project 下（引用原 issue 记录谱系），走完整闭环。
  **绝不修改已有 issue 的 owner / Project 归属。**
- EV（auditor）是独立审计环节，不计入 PI 交互对象；EV 只与产出交互，
  与 PI 的交互通过 EV 裁决报告完成。

## 输出模板（worker 必带）

- researcher: `researcher-output-templates`（分析/文献/模型三类报告模板）
- engineer: `engineer-output-templates`（PR 描述/数据库/部署运维/性能）
- auditor: `auditor-ev-templates`（EV 裁决报告 + 检查清单）
- **PI 对接块**：每个产出提交评审时必带——验收标准逐条对照表 / 给 PI 的
  决策输入 / 已知问题与风险 / 明确的评审请求。
- **路线图衔接**：researcher 产出标注支持的路线图里程碑，由 PI 更新。

## Control plane

- GitHub Issue Graph fields (`blockedBy`, `blocking`, `parent`, `subIssues`,
  `issueType`) are authoritative for dependencies and decomposition.
- GitHub Project membership determines functional ownership (owner 不可变更).
- Local runtime YAML maps Project owners / mentions to kanban boards.
- If required GitHub state cannot be read, lifecycle routing fails closed.

## Ground rules

- **消息派发主通道 = 工作流 + 状态 + dispatcher 规则**（结构化）：状态机
  （Project Status）、工作流依赖（Issue Graph: parent/blockedBy/subIssues）、
  API 字段（closingIssuesReferences、author）是路由依据。
- **文本只兜底，且兜底是下一个角色的职责**：dispatcher 不做 AI 文本解析
  优化（不猜、不优化正则）。AI 撰写的评论/PR 描述由接手它的角色
  （PI 评审、auditor EV）在检查时理解。dispatcher 判不了 → fail-closed
  或升级 PI，绝不猜文本。
- **worker 诉求 = Blocked 状态**（结构化信号）：worker 要 PI 决策/解除 →
  置 issue Blocked，dispatcher 检测 → 升级 PI（issue_blocked_escalate）。
  不通过评论文本传达诉求。
- **Human 状态 = 需真人干预**：PI 判定问题超出 AI 循环 → 置 Human →
  dispatcher 显著升级通知（issue_human_escalate）且不自动推进；真人
  处理后置 Done（验收）或回 Ready（解除）。Blocked 与 Human 语义分离：
  Blocked 是 AI 循环内诉求（PI 可决策），Human 是终局性暂停（需真人）。
- **账号即物证 + 环境变量预传递**: 每个角色绑定唯一账号（GH_USER_PI /
  GH_USER_WORKER / GH_USER_AUDITOR），调用方注入、无默认值、缺省 fail-closed；
  切换后验证当前账号，不符即拒绝写入。
- 观察者永不写状态: dispatcher 只读 + 投递，绝不改 Project/Graph/不 merge/不关 Issue。
- GitHub 是唯一真相: 状态只从 GitHub 读，单向映射到 kanban，不双写。
- fail-closed: 控制面读不到就拒绝投递，绝不猜。
- at-least-once: 投递失败保留旧状态重试，不丢事件。
- 确定性路由降级链: Project 归属 → 作者身份 → [TO:] 显式覆盖（mention_map
  明确配置才生效）→ 升级 PI / 响铃警告。
- 损坏状态自动恢复: 备份 `.corrupt` + fresh baseline，人工检查备份。
- Dry-run must not send messages or write state.
- 调度 cron 是 `no_agent` 观察者，不启动 reasoning agent。
