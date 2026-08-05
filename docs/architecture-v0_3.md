# 架构精简定案 v0_3 — 契约层 + 实现层 + 异常管理

> 状态: 定案 (2026-08-05, 用户逐层推敲确认)
> 适用范围: bot-dispatcher 项目本体 (helixatlas 仅为部署实例)
> 分层原则: **GitHub 状态机 = 长期契约 (长久坚持); kanban/sync/投递 = 第二层实现 (可替换)**

---

## 一、契约层: GitHub 八态状态机 (长久坚持)

### 1.1 状态流转矩阵 (每格唯一归属角色, 谁裁决谁操作)

```
Inbox ──PI(路线图分布)──→ Ready ──worker(开工)──→ In Progress ──worker(完成+自查+贴证据)──→ EV Review
EV Review ──auditor PASS──→ PI Review
EV Review ──auditor REJECT──→ Ready (直接返工)
PI Review ──PI 验收──→ Done
PI Review ──PI 驳回──→ Ready
任意执行态 ──worker──→ Blocked (诉求)
Blocked ──PI──→ 回原状态 (解除)
任意 PI 可控状态 ──PI──→ Human (仅 PI 可进入, worker/auditor 不能)
Human ──真人──→ Done / Ready
```

### 1.2 角色-状态归属表

| 状态变化 | 操作者 | 备注 |
|---|---|---|
| Inbox → Ready | PI | 基于路线图分布 (project/milestone) |
| Ready → In Progress | worker | 实际开工时拨 |
| In Progress → EV Review | worker | 完成 + 自查 + 贴证据评论 |
| EV Review → PI Review | auditor | EV PASS, 直接拨 |
| EV Review → Ready | auditor | EV REJECT, 直接拨回 |
| PI Review → Done / Ready | PI | 验收 / 驳回 |
| → Blocked | worker | 诉求唯一入口 |
| Blocked → 回原状态 | PI | 解除 |
| → Human | PI | 仅 PI, 任意 PI 可控状态 |
| Human → Done / Ready | 真人 | 处理完拨回 |

### 1.3 核心原则

1. **谁裁决谁操作** — 每个状态变化有唯一归属角色, agent 直接拨状态、直接贴评论, 无中间人
2. **评论 = 内容载体, 不是路由信号** — 路由 = 工作流 + 状态流转; @ / [TO:] 有漂移风险, 已废弃
3. **GitHub 八态 = 对外唯一完整生命周期契约** — 任何人/agent 看 GitHub 即知全部状态
4. **In Progress 仅可见性 + 超时锚点**, 不驱动投递

### 1.4 评论路由机制 (已删除)

- issue 评论扫描、PR 评论扫描、[TO:] 解析、@ 路由、unknown→PI 兜底链、mention_map — 全部移除
- 诉求表达改为: worker 置 Blocked = 诉求, 评论仅作补充说明

---

## 二、实现层: dispatcher = 纯通知 + 监控

### 2.1 dispatcher 定位 (唯一职责)

```
① 通知: 检测 GitHub 状态变化 → 通知对应角色
   Ready → worker (执行通知, 保留)
   EV Review → auditor (执行通知, 保留)
   Human 出现 → 飞书通知用户
   PI Review / Blocked → 不通知 PI (PI 主动轮询, 见 §3)
② 监控: 滞留 WARN / 活性检测 / 资源监控 / 心跳 (见 §3)
✗ 不投递卡片  ✗ 不改状态  ✗ 不解析评论  ✗ 不评论  ✗ 不写 GitHub 任何内容
```

### 2.2 通知对象

| 通知对象 | 接收方式 | 内容 |
|---|---|---|
| worker | dispatcher 通知 (本机 hermes profile) | Ready → 开工 |
| auditor | dispatcher 通知 (本机 hermes profile) | EV Review → 审计 |
| PI | **不接收通知** (远端机器, 主动轮询 GitHub) | — |
| 用户 | 飞书 (主) + 邮件/短信 (兜底) | Human / WARN / 资源危机 |

### 2.3 删除清单 (已定案)

| 机制 | 状态 | 原因 |
|---|---|---|
| sync_job 全部 | 删 | 契约层全手动, 状态同步/自动评论无存在理由 |
| kanban 投递 (建卡) | 删 | 通知靶子改为角色本人, kanban 降级为可选内部清单 |
| 评论路由 ([TO:]/@/兜底链) | 删 | 评论非路由信号 |
| Review 分支 PR 检查 | 删 | 与 PR 监控重复 |
| agent-deck 投递 (tmux digest) | 删 | legacy, kanban 时代已废弃 |
| Milestone 监控 | 删 | 字段保留 (统计查询用), 提醒逻辑删除 |
| PM/coordinator 角色 | 删 | 状态机无 PM 位置; PM 职责移到对话接口层 (drwho 汇报/传话) |
| Issue Graph 通知 | 删 | 除 PI 外无角色管依赖 |

### 2.4 worker 选择 (选实例)

- 选角色 = 契约层 (Project owner 绑定, 不可变)
- 选实例 = 通知目标解析, 属 dispatcher 通知逻辑: 静态映射 (dispatcher.yaml 配置 owner角色→通知目标)
- 本机走飞书/cron, 服务器走 webhook/SSH — 通道差异, 配置体现

---

## 三、异常管理

### 3.1 检测

| 检测 | 归属 | 内容 |
|---|---|---|
| 状态变化 | dispatcher | GitHub 状态 diff |
| 滞留 WARN | dispatcher | 超时 → token 成本提示 (不介入) |
| 活性检测 | dispatcher | heartbeat 文件 / 文件写入 mtime / 进程存活 |
| 资源监控 | watchdog | CPU / 内存 / 磁盘 >90% / 僵尸进程 / 锁占用 |
| dispatcher 心跳 | watchdog (外层) | 心跳文件新鲜度 |
| PI 主动轮询 | PI (远端 codex) | 发现异常 → 拨 Human |

### 3.2 异常分级

| 等级 | 场景 | 动作 |
|---|---|---|
| WARN | 滞留超时 (任务跑得久) | 飞书提示 token 成本, 不介入 |
| L1 | 活性消失 (heartbeat/写入停) | 通知 PI (PI 轮询亦可见 → 拨 Human) |
| L2 | 评审积压 (EV Review / PI Review) | 通知 PI / 用户 |
| L3 | PI 掉线 / dispatcher 掉线 / 资源危机 (磁盘/僵尸/锁) | 飞书 + 邮件/短信双发 |

### 3.3 通知通道 (双触发)

```
通道 1: 飞书会话 (主) — 完整详情
通道 2: 邮件/短信 (占位) — 摘要告警
触发: 所有异常 → 飞书; 飞书失败 → 邮件/短信; L3 → 直接双发
```

### 3.4 Human 超时兜底 (PI 第二通道)

```
PI 轮询发现 Human 状态持续 8 小时
  → PI 走邮件/短信通知用户 (第 1 条)
  → 每小时重发 1 次
  → 上限 3 次 (8h / 9h / 10h) → 停止
目的: 防 dispatcher 失灵 (Human 事件未通知到用户), PI 作为独立兜底
```

### 3.5 排错留痕 (可观测性)

```
每次 tick 写:
  心跳文件   {ts, tick_ok, 各检测项摘要}     → dispatcher 活着 = 心跳新鲜
  事件日志   append {ts, level, source, detail} → 异常全记录, 可回溯时间线
  状态快照   每 N 分钟: 进程/锁/资源/issue 状态  → 出事时看快照定位
```

---

## 四、工作流链条 (最终形态)

```
PI 拨 Ready → dispatcher 通知 worker
worker 拨 In Progress → 干活 → 自查 → 贴证据评论 → 拨 EV Review
dispatcher 通知 auditor
auditor 审计 (独立重跑证据链) → 拨 PI Review (PASS) / Ready (REJECT)
PI 轮询发现 PI Review → 验收 → 拨 Done / 驳回 Ready
dispatcher 检测 Human / WARN / 资源危机 → 飞书通知用户
PI 检测 Human 超时 8h → 邮件/短信通知用户 (兜底)
```

每一步: 状态变化 → 通知 → 角色直接操作 GitHub。无卡、无中间人、无文本路由。

---

## 五、落地记录

| 项 | 状态 |
|---|---|
| 契约层八态 (EV Review / PI Review 拆分) | 待实施 |
| dispatcher 瘦身 (删评论路由/PR 检查/milestone/agent-deck/PM) | 待实施 |
| 删 sync_job + 重写 tick (只跑 dispatcher) | 待实施 |
| 监控模块 (WARN/活性/资源/心跳/双通道) | 待实施 |
| Human 超时兜底 (8h×3) | 待实施 |
| 测试更新 | 待实施 |
| codex 复核 | 待实施 |


---

## 五、Human 兜底双通道 (2026-08-05 收敛定案)

dispatcher 是 no_agent cron 脚本, 投递发生在脚本进程之外 (cron 调度层),
**脚本内无法感知真实投递结果** — "投递后确认"在脚本内是假命题 (codex r2-r4 教训).

收敛设计:

```
通道 1 (dispatcher 尽力而为): 飞书提醒
  monitor 检测 Human 超时 (8h+) → 锁内自增计数 (上限 3 次, 1h 窗口)
  → 通知输出到 cron 投递管道 (飞书主通道)
  → 外部投递失败由 cron error 告警兜底 (平台机制)

通道 2 (PI 兜底, 真实保障): 邮件/短信
  PI (远端 codex) 独立轮询 GitHub → 发现 Human 持续 8h+
  → 每小时 1 次, 上限 3 次, 邮件/短信直达用户
  → 不依赖 dispatcher (dispatcher 失灵时仍有效)
```

职责边界:
- dispatcher 的 Human 提醒是"限频提醒" (防每 tick 刷屏), 不是关键投递
- 真正防 dispatcher 失灵的兜底 = PI 第二通道 (用户定案)
- 邮件/短信通道占位: notify level=3 已留, 接 SMTP/短信网关时填入
