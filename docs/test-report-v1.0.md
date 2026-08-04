# bot-dispatcher 体系级测试报告 & 团队复用 SOP

- 版本: v1.0
- 日期: 2026-08-04
- 分支: `feat/kanban-delivery`
- 测试目标: 验证「GitHub 控制面 + Kanban 执行面」三层体系到**可信**程度，复用于团队管理

---

## 一、测试矩阵总览

| 层 | 范围 | 方式 | 结果 |
|---|---|---|---|
| **L0** 单元 | delivery_mode 双轨、幂等键、卡体构造、config 校验 | pytest 纯函数 | ✅ 70 通过 |
| **L1** 集成 | sync_job 汇报通道、at-least-once、状态幂等、EV 触发 | pytest + mock gh | ✅ 86 → 87 通过 |
| **L2** 沙箱 E2E | 真实 kanban + stub GitHub 全链路 | 脚本驱动 | ✅ 闭环 |
| **L3** 真实 GitHub | 全生命周期（Issue/PR/Project/workflow） | 真实环境演练 | ✅ 闭环 |
| **L4** 团队+韧性 | 多角色路由、并发隔离、观察者纪律、fail-closed | 沙箱 + 故障注入 | ✅ 通过 |

**最终测试计数: 87/87 全绿**（53 原有 + 34 新增，零回归）

---

## 二、L0/L1 单元与集成（代码资产）

### 新增代码
- `dispatcher.py` 改造：
  - `delivery_mode` 双轨: `agent-deck`（原路径保留）| `kanban`（`hermes kanban create`）
  - 未知 `delivery_mode` → **fail-closed 拒绝**
  - `kanban_idempotency_key(repo, issue_num)` 确定性幂等键（同一 issue 永不重复建卡）
  - `build_kanban_command()` 卡体构造（标题剥 `/goal`、body 携带契约+证据 stub）
  - `queue_goal(..., issue_num=)` 平行映射 `_pending_issues`（向后兼容，原 53 测试零改动）
  - 状态损坏 → 备份 `.json.corrupt` + 全新 baseline（**不重复派工**）
- `sync_job.py`（272 行）: 执行面 → 控制面**单向上报通道**
  - 读 done 卡 → `gh issue comment` 贴证据摘要 → GraphQL 置 Review
  - 状态文件幂等（重复运行不重复评论）
  - repo 匹配支持 GitHub 全名或本地 key 两种形式

### 关键语义验证
- **at-least-once 不丢事件**: 投递失败保留旧状态重试；baseline 只记录不投递
- **幂等**: 重复 tick 不重复建卡、不重复评论
- **观察者纪律**: dispatcher 全程 0 次 gh 写操作（transcript 审计）

---

## 三、L2 沙箱 E2E（真实 kanban + stub GitHub）

### 环境
- 真实 `hermes kanban` board（`l2-e2e-sandbox`）
- 可编程 `gh` stub（完整记录所有调用，支持故障注入）

### 验证链路（L2-01）
```
Issue Inbox → baseline → flip Ready
→ dispatcher 建卡（[Issue #42] …）
→ 重复 tick 幂等（仍 1 张）
→ worker complete
→ sync_job: 证据评论 + Review 置位
→ 重复 sync 幂等（0 新增）
```

### 过程中修复
1. **board 选择语义**: hermes kanban 的 board 是全局 CLI 切换（`boards switch`），
   不是 `--tenant` 也不是 `--project` —— 已重构 `flush_goals` + `sync_job`
2. stub 需覆盖 dispatcher 全部查询形态（`--jq`、milestone 逐行 JSON、issue graph 关系分页）

---

## 四、L3 真实 GitHub 演练（全生命周期）

### 环境（真实，可复现）
- repo: `everything-bot-engineer/bot-dispatcher-e2e`（私有，用完可删）
- Project V2: "L3 E2E Board"，四态 `Inbox / Ready / Review / Done`
- pr-status-sync workflow 部署（`pull_request` 事件驱动）
- **账号分离**: PI = hh1985（repo scope），Project 操作 = everything-bot-engineer（project scope）

### 验证链路（完整闭环）
```
Issue #1 Inbox → Ready（dispatcher 建卡 t_efdb77c8）
→ worker 完成 → 证据 PR（body "Closes #1"）
→ draft→ready（workflow 触发 ready_for_review → Review）
→ PI merge → workflow 置 Done + issue 自动 CLOSED
```
- 3/3 workflow runs **success**
- 物证: issue #1 **CLOSED + Done**，PR #2/#3 **MERGED**

### 过程中发现并解决
1. **`[Issue #0]` bug**: 评论路由缺 `issue_num` → 12 处 `queue_goal` 调用点全补
   （issue comments、PR comments、review/merge、graph 通知、workflow 转换、milestone）
2. **Project 必须与 repo 同 owner**: 跨账号无法链接 → 测试 repo 重建到 project 所有者名下
3. **workflow YAML 缩进**: 本地替换脚本破坏 `run:` 块缩进（模板本身无 bug）

---

## 五、L4 团队复用场景 + 韧性

### 多角色路由（L4-01）
- 3 个 project（Research/Engineering/Strategy）× 各自 owner 角色
- 验证: 非 workflow_role 的 owner（researcher）直接收卡；配置 `workflow_role: pm` 时
  PM 收到 lifecycle 协调通知（符合 AGENTS.md 角色定义: PM 拥有协调、executor 执行）
- 并发隔离: 重复 tick 仍 3 张卡，零重复

### 观察者纪律（L4-04）
- transcript 全量审计: **dispatcher 0 次 gh 写操作** PASS

### 韧性（L4-R）
| 场景 | 行为 | 结果 |
|---|---|---|
| R1 控制面不可用 | fail-closed（0 投递）+ 状态保留 | ✅ |
| R2 非法 config | argparse 拒绝，不进入投递 | ✅ |
| R3 状态文件损坏 | 备份 `.corrupt` + 全新 baseline，0 重复派工 | ✅（修复后） |

**R3 修复**: `load_state` 原对损坏文件返回 `{}` → 下次 tick 重放全部事件（重复派工）。
现改为备份 + 返回 None → fresh-baseline 语义。新增回归测试。

---

## 六、复用于团队管理的 SOP

### 6.1 部署前置（一次性）
1. **GitHub 侧**（控制面）:
   - 建 repo（决策记录 + 证据文件都进 Git，GitHub = 唯一完整生命周期快照）
   - 建 Project V2，Status 字段四态: `Inbox / Ready / Review / Done`
   - 部署 `workflows/pr-status-sync.yaml`（PR 生命周期 → Issue 状态），
     配置 `PROJECTS` 映射（project/field/option 的 node ID）
   - PR 约定: body 必须含 `Closes #N`（原生关闭 + 状态联动）
2. **凭据分离**（推荐，权限最小化）:
   - PI 账号: repo scope（评审、merge、评论）
   - 独立 bot 账号: project scope（Project V2 读写）+ repo collaborator
   - 两个账号的 `gh` 通过 `gh auth switch` 切换
3. **Hermes 侧**（执行面）:
   - 建 kanban board（如 `team-main`）
   - `dispatcher.yaml` 配置: `delivery_mode: kanban` + `kanban_board` +
     `projects[].node/owner/review_field/review_option` + `session_map` + `mention_map`

### 6.2 日常运行
- **调度**: cron `no_agent` 每 N 分钟跑 dispatcher（只读观察 + 通知运输）
- **派工**: 控制面置 `Ready` → dispatcher 建卡（幂等键锚定 issue，永不重复）
- **执行**: worker 在 kanban 卡上完成（`hermes kanban complete`），产出证据文件 + PR
- **上报**: sync_job 读 done 卡 → 证据评论回帖 + issue 置 `Review`
- **评审**: PI（agent session）在 PR 上评审；merge 后 workflow 自动置 `Done` + 关 issue
- **归档**: GitHub Done → 归档 kanban 卡（`sync_job` 或人工）

### 6.3 纪律清单（团队必须遵守）
1. **观察者永不写状态**: dispatcher 只读 + 投递，绝不改 Project/Graph/不 merge/不关 Issue
2. **GitHub 是唯一真相**: 状态只从 GitHub 读，单向映射到 kanban，不双写
3. **fail-closed**: 控制面读不到就拒绝投递，绝不猜
4. **两阶段提交**: 投递 digest 成功才推进状态；失败保留旧状态重试（at-least-once）
5. **确定性路由降级链**: Project 归属 → owner → [TO:] → 作者兜底 → 响铃警告
6. **损坏状态自动恢复**: 备份 `.corrupt` + fresh baseline，人工检查备份

### 6.4 故障速查
| 症状 | 排查 |
|---|---|
| 卡标题 `[Issue #0]` | 旧版本缺 issue_num → 升级到本分支 |
| workflow 0s failure | YAML 解析错误（检查 `run:` 块缩进、`on:` 行） |
| 控制面不可用警告 | gh auth 切换错账号 / token scope 不足 |
| Project 无法链接 | Project 与 repo 必须同 owner |
| 卡没建 | 首次 tick 是 baseline（不投递）；需先 Inbox 再 flip Ready |
| 重复派工 | 检查 state 文件是否损坏（应已自动备份恢复） |

---

## 七、遗留与建议

1. **EV 驳回往返**（L3 计划内未跑）: PR review 驳回 → 修改 → 再评审的全循环，
   在真实 repo 上可补充验证（逻辑已由 L1 测试覆盖）
2. **WIP 上限**: v0_2 设计暂缓 WIP 闸，仅保留 Review 滞留时长作为外部异常观察指标
3. **清理**: L3 测试 repo（`everything-bot-engineer/bot-dispatcher-e2e`）用完可删
4. **部署授权**: 本分支代码需 PI 单独授权后才部署到正式仓库（AGENTS.md 变更流程）

---

*报告由 Hermes agent 生成，全部结论基于实际执行输出（pytest 87/87、沙箱脚本、真实 GitHub 物证）。*
