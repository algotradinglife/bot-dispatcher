# 交付体系改进方案 — 借鉴开源框架的机制落地

> 2026-08-12。背景：#207 第二轮 EV 发现 6 个方法学缺陷（模型选择泄漏、
> 指标语义、schema 契约），第一轮未发现。核心问题：**返工轮次高，方法层
> 缺陷（R2 类）只能靠 EV 递进深审暴露，worker 交付前不自审方法正确性**。
>
> 方案：不引入新框架，借鉴成熟开源框架的机制（OpenSpec / grill-me /
> grill-loop-skill / grill-me-store-decisions / gstack-decision），
> 落地到我们现有的 REQ 契约 + profile SOUL + 交付规范体系。

## 一、问题诊断（为什么返工轮次高）

### 1.1 返工的两层性质
```
R1 类（工程/数据层）：verifier 盲区、fsync、fail-fast、命名 — 靠 REQ-E06
   tamper 自测已前置（2026-08-12 落地）
R2 类（方法/科学层）：模型选择泄漏（CV_2023 选 SIMPLE/NEURAL）、指标语义
   错误、schema 非 machine-readable、decisive path 未 materialize —
   目前只能靠 EV 第二轮深审发现，worker 交付前无自审
```

### 1.2 根因
| 根因 | 表现 |
|---|---|
| **方法决策无记录** | worker 选模型/指标时不记录"在哪定的、为什么"，EV 无法事前审查，只能事后抓泄漏 |
| **自审只有攻击清单，无分级路由** | REQ-E06 tamper 自测是清单式（4 类攻击），无"哪些问题 fast-pass、哪些要 judge"的分级 |
| **验收标准在契约里但方法声明缺失** | 契约有 REQ 编号，但没强制"模型选择冻结声明"（OpenSpec 的验收冻结思想未用足）|

## 二、借鉴的框架机制

| 框架 | 机制 | 借鉴点 |
|---|---|---|
| **OpenSpec** | 变更提案冻结验收标准，spec 先于代码 | 契约里强制"方法选择冻结段"（方向 A）|
| **grill-me** | 交付前连环拷问设计缺陷 | 自审思想（已有 REQ-E06 雏形）|
| **grill-loop-skill** | 状态化自审循环：FAST_PASS/RESEARCH/JUDGE/ESCALATE 分级路由，Judge 按固定 rubric 打分 | 自审分级路由（方向 B）|
| **grill-me-store-decisions** | 访谈式审查 + 持久化决策树（选了什么/拒了什么/理由）| 决策记录（方向 C）|
| **gstack-decision** | 事件溯源决策日志（decide/supersede/redact，append-only，scope=repo/branch/issue）| 决策日志的工程化形态 — DECISION.md 的升级版 |

## 三、改进方案（三项机制落地）

### 机制 1：契约方法冻结段（借鉴 OpenSpec）— 治 R2-01 类

**改动**：REQ 模板新增 `REQ-ISSUE-00 — 方法冻结声明`（每 issue 契约必带）：
```
契约方法冻结段（PI 写契约时声明）：
- 模型/消融选择在哪个 split 冻结？（如 CV_2022）
- 哪个 split 只执行一次 terminal decision？（如 CV_2023）
- 指标定义逐条核对清单（HHI 聚合口径 / rolling 锚点 / 回撤定义）
- 任何"选择决策"必须预注册，禁止实现中发现后回填
```

**效果**：worker 一开始就知道"在哪定模型"，R2-01 类泄漏在契约阶段被堵。

### 机制 2：交付决策记录 DECISION.md（借鉴 gstack-decision + store-decisions）— 治决策不可追溯

**改动**：worker 交付规范新增 `DECISION.md`（worktree 根，随交付提交）：
```
# DECISION.md — 关键决策记录
| id | 决策 | 被拒方案 | 理由 | scope | supersedes |
|----|------|---------|------|-------|-----------|
| D1 | 用 SIMPLE_LOGISTIC 做主 comparator | NEURAL（CV_2023 上更好但泄漏）| CV_2022 冻结选择 | issue | — |
```
- **事件溯源思想**：追加式（append-only），修正用 `supersedes` 引用，不抹历史
- **scope**：repo / issue 两级（简化版 gstack）
- **EV/PI 审查时**：先读 DECISION.md，对照契约方法冻结段检查"选择是否按声明执行"

**效果**：worker 的"为什么这么选"全程可审计，EV 第一轮就能查方法合规性（不用等 R2）。

### 机制 3：自审分级路由（借鉴 grill-loop）— 自审从"清单"到"循环"

**改动**：REQ-E06 tamper 自测升级为分级路由：
```
自审循环（worker 交付前必跑，结果写入手 handoff）：
1. GRILL 角色：列出最高影响未决问题（tamper 攻击 + 方法自问）
2. 路由分级：
   - FAST_PASS：简单可逆问题（命名、格式）→ 直接确认
   - RESEARCH：可从本地证据回答（verifier 覆盖、指标计算）→ 自查+记录
   - JUDGE：需要判断（模型选择、口径）→ 对照契约冻结段 + 记录 DECISION.md
   - ESCALATE：需要 PI 决策 → 置 Blocked（结构化，不猜）
3. 结果随交付提交（handoff 里附 grill 记录）
```

**效果**：自审不再是无差别清单，而是分级路由 — 简单问题不阻塞，疑难问题强制记录，需 PI 的升级 Blocked。

## 四、与现有体系的关系

| 现有 | 改进后 |
|---|---|
| REQ-E06 tamper 清单 | + 分级路由（机制 3）|
| 交付三要素（SESSION/HANDOFF/状态指令）| + DECISION.md（机制 2）|
| 契约 REQ 编号 | + 方法冻结段 REQ-ISSUE-00（机制 1）|
| EV 两轮深审 | 前置到 worker 自审（机制 1+2+3 让 R2 类提前暴露）|

## 五、预期效果

- **R2-01 类（方法泄漏）**：契约冻结段 + DECISION.md → 契约阶段堵住 / 交付时可审计
- **R2-03 类（指标语义）**：契约冻结段逐条核对 → 实现前明确
- **R2-02/04/05/06 类（materialize/schema/边界）**：grill 自审分级 → worker 交付前自查
- **返工轮次**：预计从"R1 工程 + R2 方法"两轮 → 大部分方法问题提前到第一轮或交付前

## 六、实施步骤

1. REQ 模板加 `REQ-ISSUE-00 方法冻结段`（机制 1）
2. 交付规范加 `DECISION.md` 模板（机制 2）
3. analyst/engineer SOUL 更新：自审分级路由（机制 3）
4. EV 流程更新：先读 DECISION.md + 对照契约冻结段
5. PI 契约模板更新：写契约时填方法冻结段
6. 通知 PI（契约模板 + REQ 更新）
7. #208（下一 issue）验证新机制

---
*待 codex 审核：机制设计的完整性、与现有体系的冲突点、实施顺序合理性。*
