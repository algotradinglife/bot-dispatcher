# bot-dispatcher 体系级测试计划 v0_1

> 目标：把「GitHub 控制面 + Hermes Kanban 执行面 + PI Session 评审面」三层体系测到可信，
> 支撑后续复用到团队管理。
> 载体：bot-dispatcher 仓库 `feat/kanban-delivery` 分支（双轨 delivery_mode 可对照）。
> 状态：2026-08-03 设计闭合（v0_3）；本计划定义分层测试矩阵与验收标准。

---

## 0. 测试对象（三层体系）

```
┌─ 控制面 GitHub ─────────────────────────────────────────────┐
│ Issue Graph (blockedBy/parent/subIssues) · Project V2 · PR   │
│ pr-status-sync workflow: PR 事件 → 关联 issue 状态自动流转   │
└───────────────┬──────────────────────────────────────────────┘
                │ 轮询 (no_agent 每分钟)
┌─ 粘合层 bot-dispatcher ─────────────────────────────────────┐
│ 路由链: Project→owner→[TO:]→作者兜底→fail-closed             │
│ 投递靶子(改造后): hermes kanban create (双轨: agent-deck)     │
│ 汇报通道(新增): sync job: done 卡 → gh comment + 置 Review   │
└───────────────┬──────────────────────────────────────────────┘
┌─ 执行面 Hermes Kanban ──────────────────────────────────────┐
│ 卡生命周期: todo→ready→running→done · WIP caps · 幂等键       │
│ worker (profile 实例) · EV 卡 (独立 auditor)                  │
└───────────────┬──────────────────────────────────────────────┘
┌─ 评审面 PI Session ─────────────────────────────────────────┐
│ gh CLI 轮询 Review 状态 → 拉取 → 评审 → merge (唯一 merge 权) │
└──────────────────────────────────────────────────────────────┘
```

---

## 1. 分层测试矩阵

### L0 — 单元测试（纯 mock，无外部依赖）
**已有 53 个**（全部通过，7.8s）：路由/失败闭环/去重/digest 保持/baseline/dry-run/TO 指令解析/report URL 提取。

**需新增（改造引入的纯逻辑）：**

| # | 用例 | 验收 |
|---|------|------|
| L0-01 | `delivery_mode` 配置解析：`agent-deck` \| `kanban` \| 缺省 | 缺省=agent-deck 向后兼容 |
| L0-02 | `delivery_mode=kanban` 时 flush_goals 构造 `hermes kanban create` 命令 | title/body/assignee/幂等键正确 |
| L0-03 | 卡体内容：issue URL + 契约摘要 + 验收标准 + 证据要求 | 字段齐全 |
| L0-04 | 幂等键生成：`issue-<repo>-<num>` 确定性 | 同 issue 同键 |
| L0-05 | `--idempotency-key` 传递 | 重复 tick 不建重复卡 |
| L0-06 | agent-deck 路径保留（双轨对照） | 原 53 测试不回归 |
| L0-07 | 未知 delivery_mode 拒绝 | fail-closed |
| L0-08 | sync job 状态映射纯函数：done 卡 → (gh comment 文本, Review 置位) | 单向映射无回写 |

### L1 — 集成测试（mock subprocess/gh/hermes CLI）
| # | 用例 | 验收 |
|---|------|------|
| L1-01 | flush_goals kanban 模式：mock subprocess，断言 CLI 参数 | 命令序列正确 |
| L1-02 | 建卡失败（CLI 非零退出）→ 保留 pending 状态待重试 | at-least-once 不丢事件 |
| L1-03 | 建卡成功后状态提交；部分失败保留旧状态 | 状态文件原子性 |
| L1-04 | sync job：mock gh，done 卡 → issue comment + Project Review | 汇报通道正确 |
| L1-05 | sync job 幂等：重复运行不重复 comment | 幂等 |
| L1-06 | EV 触发：产出卡 done → 建 EV 卡（assignee=auditor 独立 profile） | EV 独立性 |
| L1-07 | `kanban complete` 的 --summary/--metadata 结构化交接 | 下游可消费 |

### L2 — 沙箱端到端（真实 hermes kanban + mock GitHub 控制面）
用真实 `hermes kanban`（本地 board，隔离测试用），GitHub 侧用本地 stub（gh mock 脚本）。

| # | 场景 | 验收 |
|---|------|------|
| L2-01 | 全链路：Ready issue → 建卡 → 卡 running → 完成 → sync 置 Review | 状态机走通 |
| L2-02 | 重复 tick 幂等：同 issue 连续两次扫描 | 只建一张卡 |
| L2-03 | 控制面不可用（GraphQL 失败）→ fail-closed，不建卡不丢状态 | 观察者不制造现实 |
| L2-04 | 投递失败重试：CLI 挂 → pending 保留 → 恢复后补投 | at-least-once |
| L2-05 | WIP 上限：超过 max_in_progress 不再派工 | 队列不膨胀 |
| L2-06 | EV 驳回往返：产出卡 done → EV 卡驳回 → fix 卡复用同一 worktree | 多次交互闭环 |

### L3 — 真实 GitHub 演练（需要授权创建测试 repo）
在 hh1985 名下建隔离测试 repo（如 `bot-dispatcher-e2e`），真实 Project V2 + Issue + PR。

| # | 场景 | 验收 |
|---|------|------|
| L3-01 | 真实 Issue 置 Ready → dispatcher 建卡 → worker 执行 → 产出 → PR | 全链路真实跑通 |
| L3-02 | pr-status-sync workflow：draft→ready_for_review → Review；merge → Done | GitHub 原生自动化验证 |
| L3-03 | 证据 PR 模型：worker 开 PR（body `Closes #N`）→ 状态自动流转 | 物证驱动 |
| L3-04 | EV 独立验证：fresh checkout 重跑校验 | EV 铁律 |
| L3-05 | PI 评审往返：驳回 → fix 卡同分支续写 → 通过 → merge → 归档 + prune | 多次交互 |
| L3-06 | 状态单向映射审计：全程无双向回写 | 防双看板漂移 |

### L4 — 团队复用场景（多角色/并发/韧性）
| # | 场景 | 验收 |
|---|------|------|
| L4-01 | 多角色路由：PI/PM/worker/EV/reviewer 各归其位 | 路由链正确 |
| L4-02 | 并发 children 卡：独立 worktree 互不污染 | 文件隔离 |
| L4-03 | 评审积压反压：Review WIP 上限 → 上游并发按 PI 吞吐调节 | 无 Review 堆积 |
| L4-04 | 观察者纪律：dispatcher 全程只读 GitHub（断言无 gh 写操作） | 永不写状态 |
| L4-05 | 恢复演练：状态文件丢失 → 重建不重放历史 | 幂等 + baseline |
| L4-06 | 报告完整性 watchdog：缺必需段不给 complete | 报告=第一公民物证 |

---

## 2. 验收标准（整体）

1. **L0+L1 全绿**：单元/集成测试 100% 通过，无回归（原 53 + 新增 ≥ 20）。
2. **L2 沙箱全链路走通**：Ready→卡→done→Review 状态机，含故障注入至少 3 场景。
3. **L3 真实演练完成**：一条真实分析任务全生命周期（含至少 1 次 EV/PI 驳回往返）。
4. **L4 团队场景**：多角色 + 并发 + 韧性场景通过，产出复用 SOP。
5. **SOP 落盘**：团队管理复用手册（角色/路由/状态机/评审闸门/故障处理）。

---

## 3. 依赖与授权

- [x] gh 已认证 hh1985（repo scope 足够建私有测试 repo）
- [x] hermes kanban 可用（create/complete/boards switch）
- [x] pytest 环境就绪（.venv，53 passed）
- [ ] **需用户授权**：L3 在 hh1985 名下创建真实测试 repo（建议私有、命名 bot-dispatcher-e2e，用完可删）
- [ ] L2/L3 中 hermes kanban worker 会真实跑任务（消耗少量 token，任务为 trivial 验证型）

---

## 4. 阶段顺序

1. **阶段 A（本分支）**：L0 新增用例 → 实现双轨 delivery_mode → L0+L1 全绿
2. **阶段 B**：L2 沙箱（mock GitHub stub + 真实 kanban）
3. **阶段 C**：L3 真实演练（需授权建 repo）
4. **阶段 D**：L4 团队场景 + SOP

每阶段产出物证（测试输出/日志/状态快照），汇入最终测试报告。
