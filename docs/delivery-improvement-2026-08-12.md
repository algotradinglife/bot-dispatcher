# 交付体系改进方案 v2 — codex 修正版（2026-08-12）

> v1 提出三机制（契约方法冻结段 / DECISION.md / grill 分级路由），
> codex 审核修正：**机制要硬化，不做软约束文书**。优先级：
> `protocol + holdout 隔离 + execution receipt + required CI`
> **高于** `DECISION.md + grill 文书 + SOUL 提示词`。
>
> 本版按 codex 修正重写。

## 一、问题诊断

### 1.1 返工两层性质
```
R1 类（工程/数据层）：verifier 盲区、fsync、fail-fast — REQ-E06 tamper
   自测已前置（2026-08-12 落地）
R2 类（方法/科学层）：模型选择泄漏（#207 R2-01 CV_2023 选 SIMPLE/NEURAL）、
   指标语义错误（R2-03）、schema 非 machine-readable（R2-05）—
   只能靠 EV 第二轮深审暴露
```

### 1.2 根因
1. **方法决策无协议化**：worker 选模型/指标无冻结协议，泄漏靠 EV 事后抓
2. **无硬门禁**：交付只验"代码能跑 + tamper 过"，不验"方法协议合规"
3. **无 holdout 隔离**：模型选择与 terminal 评估共用数据，泄漏从结构上可能

## 二、借鉴的框架机制（codex 修正后）

| 框架 | 借鉴点 | codex 修正 |
|---|---|---|
| OpenSpec | 验收标准冻结 | **可执行 method.yaml + schema**（非文本段）|
| grill-loop | 自审分级 | **hard-gate 驱动**（非 worker 贴标签）|
| gstack-decision | 决策日志 | **每 issue 协议修订日志**（最小化）|

## 三、改进方案（三个硬化机制）

### 机制 1：可执行方法协议冻结（治 R2-01/03 类）

**改动**：每 issue 契约带 `method.yaml`（机器可读协议）：
```yaml
# method.yaml — 方法协议（PI 写契约时冻结，hash 绑定）
protocol_version: 1
candidate_pool: "55,042 exact-N7 frozen #206"
splits:
  selection_split: CV_2022      # 模型/消融选择冻结处
  terminal_split: CV_2023       # 只执行一次 terminal decision
selection_algorithm: "frozen selector, no CV_2023 peeking"
metrics:
  - {name: hhi, definition: "aggregate by immutable ticket identity"}
  - {name: rolling, definition: "5/10/20 with ¥100k time-zero anchor"}
  - {name: drawdown, definition: "completed recovery vs terminal-open separated"}
seed: 42
budget: {max_runtime: 8h, max_cpu_hours: 64}
stop_rules: [...]
required_artifacts: [...]
schema_ref: "schema.yaml"
```

**硬门禁**（`make preflight` + required CI）：
- method.yaml 存在且 schema 合法
- protocol hash 与执行 receipt 一致（worker 不得改协议不声明）
- 指标 golden tests（HHI/rolling/回撤定义逐条验证）
- 必需工件/schema/materialization 存在

**holdout 隔离**（模型选择泄漏的真正 P0）：
- 能隔离则加 terminal holdout 隔离 runner（selection_split 数据不出现在
  terminal 评估路径）
- 做不到时**明确记录剩余风险**（不得把文档冻结包装为强保证）

### 机制 2：每 issue 协议修订日志（最小化，治决策可追溯）

**改动**：交付时附 `PROTOCOL-LOG.md`（per-issue，非仓库级）：
```
# PROTOCOL-LOG.md — 本 issue 协议修订记录
| 事件 | 协议项 | 变更 | 理由 | 证据 | supersedes |
|------|--------|------|------|------|-----------|
| revise | metrics.hhi | 聚合口径调整 | CV_2021 发现重复票 | golden test | — |
```
- **只记**：协议修订 / 偏离 / 升级 / 重大取舍
- **不建**完整事件溯源系统（gstack 的 decide/supersede/redact 全套不做）
- 追加式，不删改历史行

### 机制 3：hard-gate 驱动的问题处置（替代 grill 分级文书）

**改动**：不自审贴 FAST_PASS/JUDGE 标签，改为 **preflight/CI 硬检查**：
```
make preflight（required CI，失败禁止进 EV）：
  ├─ protocol schema 合法 + hash 匹配
  ├─ 必需工件/schema/materialization 存在
  ├─ 指标 golden tests 过
  ├─ 冻结协议与执行 receipt 一致
  └─ 已有协议修订事件未被修改/删除
```
- worker 交付前跑 preflight → 红 = 不能交 EV
- 疑难问题（模型选择/口径）→ 走契约冻结段 + PROTOCOL-LOG，不靠自审判断
- 需 PI 决策 → 仍置 Blocked（结构化）

## 四、EV 流程更新

```
EV rubric 增加 REQ-M 逐项 verdict：
  REQ-M01 协议冻结：method.yaml 存在 + hash 匹配 + 执行 receipt 一致
  REQ-M02 holdout 隔离：selection/terminal split 隔离验证
  REQ-M03 指标定义：golden tests 逐条
  REQ-M04 协议修订：PROTOCOL-LOG 完整 + 无隐藏修订
顺序：先独立验证（EV 自己重算/重跑），后看 worker 决策解释
     （防止 worker 解释污染 EV 判断）
```

## 五、实施顺序（codex 修正：协议/CI 先行）

```
P0（先做）：
  1. method.yaml schema + 模板（templates/method.yaml.example）
  2. make preflight 检查器（protocol schema/hash/工件/指标/修订完整性）
  3. required CI 接入 preflight
  4. #208 shadow pilot（只收集指标，不设 required gate）

P1（pilot 数据后）：
  5. holdout 隔离 runner（能做则做；不能做记录剩余风险）
  6. EV rubric 加 REQ-M
  7. PROTOCOL-LOG 模板（最小版）
  8. SOUL 提示（最后 — 消费同一套 REQ-M/rubric，不另造清单）
```

## 六、Shadow pilot 指标（#208）

```
worker 自审发现数 / preflight 阻断数 / EV 新发现数 / PI 新发现数
重复发现率 / first-pass EV pass rate / 模板填写成本 / 误报率
```
数据说话再决定推广到所有研究 issue。

## 七、与现有体系关系

| 现有 | 改进后 |
|---|---|
| REQ-E06 tamper 自测 | 保留（工程层）；方法层由 REQ-M + preflight 覆盖 |
| 契约 REQ 编号 | + method.yaml 协议冻结 |
| 交付三要素 | + PROTOCOL-LOG（协议修订） |
| EV 两轮深审 | preflight 前置拦截 + REQ-M rubric |
| 探索类交付 | 板块 verdict + codex subagent 自检（预筛，不进 issue 状态） |

## 八、探索类研究：板块化交付 + codex subagent 自检（2026-08-13 定）

### 8.1 板块拆解（探索类默认形态）

探索类研究（统计/预处理/EDA/特征/模型/对比）默认**单 issue 内按板块交付**，
不拆 sub-issue（sub-issue 只留给需要并行或独立验收的场景；串行 sub-issue 的
每 issue 固定开销吃掉收益，已用期望时长模型验证）：

- 板块固定：统计 → 预处理 → EDA → 特征 → 模型 → 对比
- 每板块三层交付标准：**问题 / 工件 / 约束**，交付前预注册
- 单 notebook 实现；EV 按板块 verdict（板块 pass/fail），不做整轮 REJECT
- 后置板块发现问题 → 同分支修 + EV 重验该板块（无 reopen 开销）

### 8.2 codex subagent 自检（每板块交付前强制）

每个板块实现并交付 EV 前，worker 必须调用 **codex subagent** 做自检：

- **清单对位**：codex 自检必须用 EV 同一套标准——三层交付标准
  （问题/工件/约束）+ preflight 硬门禁 + REQ-E06 tamper 自测；
  泛泛 review 无效（delta≈0 = 白加开销，实测 26.1h > 纯板块 verdict 24.7h）
- **就地修**：自检发现问题 → 交付前就地修（下游未建，无连锁返工）；
  有效自检（delta≥0.5）时 EV 返工概率减半，EV 从"迭代打磨"变"确认性检查"
- **不进 issue 状态**：自检结果不写 issue 状态、不产 EV 裁决、不留 kanban 痕迹
- **不替代 EV**：EV 仍是唯一正式门禁（结构化 verdict + 审计独立性不变）；
  codex 自检是"同模型家族自审"，只做预筛，独立性弱于 EV
- 预期：高返工概率场景（p≥0.3）期望时长最优 + 尾部最短（p90 30.1h
  vs 板块 verdict 32.9h vs sub-issue 34.3h）

### 8.3 形态判定规则

| 场景 | 形态 |
|---|---|
| 探索类（默认） | 单 issue + 板块 verdict + codex subagent 自检 |
| 需要并行 / 独立验收 | 拆 sub-issue（blockedBy 串行，Issue Graph 表达） |
| 工程类原子任务（如 finalize_delivery） | 单 issue 不拆，事务性交付 |

---
*codex 审核（2026-08-12）：核心修正 = 机制硬化（method.yaml/preflight/
holdout 隔离）优先于文书（DECISION/grill/SOUL）；#208 shadow pilot 验证。*
*2026-08-13：探索类板块化交付 + codex subagent 自检写入（期望时长模型验证）。*
