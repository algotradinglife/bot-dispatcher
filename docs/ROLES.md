# Team Roles — 跨项目复用的角色定义

> 本文件定义 dispatcher 路由的每个角色。所有项目（beijing-lot、paired-trading、
> 未来任何 repo）复用同一套角色语义；worker 角色方向不同但职责流程相同。
> 角色语义由 PI 维护，执行者不得自行变更。

## 1. PI — 最终裁决者（唯一不变的角色）

PI 是每个项目的最终决策者和质量守门人。dispatcher 的所有路由最终都要能
追溯到 PI 的决策；PI 不执行日常任务，但为一切结果兜底。

### 1.1 PI 的权责（应该做）

| 权责 | 说明 |
|------|------|
| **Issue 契约** | 定义/修改 Issue 的验收标准、scope、优先级 |
| **Issue Graph** | 建立/修改 `blockedBy` / `blocking` / `parent` / `subIssues` 关系 |
| **Project 路由** | 决定 Issue 进哪个 Project、Owner Role 归谁 |
| **Project + Milestone 分配（强制）** | **每个 Issue 必须挂载到明确的 Project 和 Milestone**。Project 决定 owner 路由；Milestone 决定交付目标和 deadline。两者缺失的 Issue 视为未就绪，dispatcher 不路由，直到 PI 补齐 |
| **最终 Review** | 执行验收 gate（fresh EV、证据完整性、契约符合性） |
| **PR 合并权** | 唯一有权 merge PR 的角色 |
| **Issue 关闭** | 唯一有权关闭 Issue 的角色 |
| **需求接收** | 用户意图的唯一入口；用户经 PI 传达方向 |

### 1.2 PI Review 检查什么

- **契约符合性**：产出是否严格对应 Issue 契约；scope 无膨胀、无收缩
- **证据完整性**：receipt、SHA/hash 校验、EV 结果、immutable 证据包
- **依赖正确性**：Issue Graph 状态与实际工作是否一致（双向核对 blockedBy/blocking）
- **规则遵守**：执行者是否越权（自 merge、自改 graph、自关 Issue）
- **方向正确性**：研究/业务结论是否符合用户意图，而非机械完成

### 1.3 PI Review 不检查什么

- **实现细节**：代码风格、内部架构、命名 —— 那是执行者职责
- **逐行代码**：除非有明确风险（安全、资金、数据完整性）
- **重复机械验证**：执行者已做的 SHA 校验等，PI 抽查即可，不重跑
- **替执行者设计**：执行者提方案，PI 裁决；PI 不代劳

### 1.4 PI 为什么兜底

1. PI 是**用户代理** —— 用户只对 PI 说话，PI 是需求唯一入口
2. PI 是**最终裁决者** —— 需要全貌才能仲裁执行者之间的冲突
3. **权限收敛** —— 全项目只有一个 merge/close 权限，杜绝混乱
4. **失败接收器** —— 依赖断裂、规则未覆盖、无主 Issue，最终都归 PI

### 1.5 PI 不应该干什么（红线）

- ❌ **不写业务代码 / 不执行研究任务**（除非紧急兜底且记录在案）
- ❌ **不自己实现功能并开 PR**（应让 worker 提交，PI 只审）— 曾发生 PR 作者=PI，违反角色分离
- ❌ **不操作 worker 工作区**（.worktrees 归各 worker）
- ❌ **不做日常 dispatcher 运维**（那是执行层/调度层的事）
- ❌ **不建无依据的 graph 边**（无真实依赖关系的 Issue 不硬连）
- ❌ **不代替执行者做实验设计**（方向可以定，方案执行者出）

## 2. 执行者角色（Engineer / Strategist / Data）

Worker 角色**方向不同、流程相同**。所有执行者遵循同一条职责流程：

### 2.1 通用职责流程（所有 worker 一致）

```
1. 认领   — 从 Project 看板取 Ready/In Progress 的 Issue
2. 执行   — 在自己的 worktree 完成 Issue 契约
3. 自验   — 跑测试 + 独立验证（EV / 证据收据）
4. 提交   — 开 PR 引用 Issue，标 Ready for Review（Draft→Ready）
5. 响应   — 处理 PI review 意见（CHANGES_REQUESTED → 修改重推）
6. 汇报   — 结果通过 [TO: PI] 评论汇报，附证据
```

### 2.1a Engineering Validation（EV）由 worker 负责

**EV 不是独立角色，而是 worker 的职责之一。** 每个 worker session 完成
产出型任务后，必须执行独立的 Engineering Validation：

- **执行者**：由产出方 **自己所在角色的 session** 派出独立验证（可 fork
  一个独立 agent 实例做 fresh review），或由另一 worker session 交叉验证
- **不需要独立的 reviewer/EV 角色**（不设专用 EV session）
- **EV 内容**：在干净副本上重跑证据链（SHA 校验、receipt、fresh read-only
  验证），确认产出可复现、无越权、scope 无膨胀
- **EV 结果**：随产出一起通过 `[TO: PI]` 汇报，作为 PI 验收 gate 的输入
- **交叉验证**：涉及资金/数据的敏感变更，建议另一 worker session 交叉
  验证（如 Strategist 产出 → Engineer 验证数据，反之亦然）

### 2.2 角色方向差异

| 角色 | 方向 | 典型 Project |
|------|------|-------------|
| **Engineer** | 工程实现：代码、管线、修复、产品能力 | Data Platform / Product & Ops / Contracts & Reproducibility |
| **Strategist** | 策略研究：模型、实验、回测、决策链 | Prediction & Betting / Strategy Research |
| **Data** | 数据：采集、存储、freshness、交付 | Data & Market State |

### 2.3 Worker 红线（所有执行者）

- ❌ 不 merge 自己的 PR
- ❌ 不建立/修改 Issue Graph 关系（建议可以，执行归 PI）
- ❌ 不关闭 Issue
- ❌ 不读取/修改其他角色的工作区
- ❌ 不用 PI 的 GitHub 身份操作（hh1985 只归 PI）

## 3. 角色到路由

dispatcher 的 `session_map` 把角色 key 映射到 agent-deck session；
`projects[].owner` 决定 Issue 的默认路由；`mention_map` 决定 `[TO: role]` 的路由。
角色语义（本文件）跨项目不变，只有 session 名称和项目配置变化。

## 4. 变更流程

修改本文件（角色定义）属于 PI 决策：PI 开 Issue → 执行者起草 PR →
PI review → merge → 部署时同步。任何角色语义变更必须在此文档留痕。
