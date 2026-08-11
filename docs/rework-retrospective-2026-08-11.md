# 复盘：beijing-lot #205/#206 返工根因与体系化防范（2026-08-11）

## 背景

统一大模型 E2E 测试（#205 M×3 价值矩阵、#206 固定 N7 票生成器）经历了多轮
返工。本文记录根因分类、三层防线方案、codex 独立审核结论与实施行动项。

## 数据

- #205：2 轮返工（4 工程缺陷 + 1 科学接口歧义）
- #206：6+ 轮返工（EV-B206-01~09 缺陷 + 并发审计冲突 + PI 多次打回）
- 观察：#206 共产生 5 执行卡 + 5 EV 卡（每轮新建，无复用）

## 根因分类

### 类别 1：工程纪律缺陷（占比最高，可体系化根治）
| 缺陷 | 根因 |
|---|---|
| notebook 命令不可执行（#205-①、#206-①）| 开发环境能跑 ≠ 干净环境能跑，文档化命令未在 clean checkout 验证 |
| 输入不 fail-fast（#205-②、#206-②）| malformed 数据被静默归一化/丢弃而非显式报错 |
| 依赖未锁定（#205-④）| 版本漂移，replay 不可复现 |
| notebook 非原子写（#205-③）| 直接写 tracked notebook，崩溃可截断 |

### 类别 2：科学/接口语义（PI 层发现）
| 缺陷 | 根因 |
|---|---|
| #205：残差概率标成 calibrated_true_probability | 对照 vs 实验命名混淆 |
| #206：结论过度外推 | 科学结论边界不清（"MLP 没进步"≠"MARKET_WR 是答案"）|

### 类别 3：系统架构缺陷（控制平面）
| 缺陷 | 根因 |
|---|---|
| 并发 EV 卡（双审计 PASS/REJECT 冲突）| 每轮新建 EV 卡 + idempotency 竞态 |
| 分支冲突（返工卡 spawn 失败）| 每轮新分支，旧 worktree 未清 |
| 卡堆积 | 每轮 done + 新建，无复用 |
| EV REJECT 状态回写断链 | 需人工拨 GitHub Ready，非自动 |
| worker 上下文丢失 | kanban spawn 用 chat -q 全新会话，不 resume |

### 类别 4：流程/通知（已修）
- PI Review 不通知 PI → 已修（跟进 cron + agent-deck）
- 状态切换不进飞书 digest → 已修（USER_FACING_REASONS 扩展）
- PR merged 不通知 → 已修（恢复 merged 检测，v0_3 误删）

## 三层防线方案（含 codex 审核修正）

### ① 前置防线：可执行验收契约（REQ 模板）
- PI 开 issue 时把验收标准编号为 REQ（可执行验证条款）
- worker 交付 PR 带 REQ→evidence 对照表
- EV 按 REQ 编号逐条 verdict（从"发现新问题"变"核对清单"）
- **codex 修正**：升级为 `make preflight` 统一命令 + required CI 门禁
  （失败禁止进入 EV），不只靠 SOUL 软约束

### ② 执行防线：kanban 1 卡多 run（attempt + generation）
- **放弃"双卡 running 不释放"**（codex 否决：进程常驻 = 泄漏风险，
  把 worker 进程当流程连续性替代品是错误方向）
- 定案：每 issue 固定执行卡 + EV 卡（记录），每轮 = 独立 attempt
  （generation 递增，task_runs 原生支持）
- worker 交付 → 退出进程释放资源
- EV 入口原子 claim（issue+HEAD+generation 唯一）防并发
- EV 回写 CAS（stale verdict 拒写）；并发审计允许但仅 1 个 publisher
- REJECT → 新 run（generation+1）
- **上下文续接**：worker 交付记录 session_id → 返工用 `--resume` 恢复
  （已验证：跨进程凭记忆续接 ✅）；resume 失败回退 handoff 文档

### ③ 终局防线：EV 独立审计 + PI 对抗性审查（保留）
- 类别 2 靠它兜底，不能省
- EV 仍保留低级缺陷抽查（不能因 CI 取消兜底）

## 实施行动项（P0）

| # | 行动 | 归属 |
|---|---|---|
| 1 | 补齐 #206 事故事实（EV-B206-01~09 + 3 次 PI 驳回归档）| Hermes |
| 2 | 通用 REQ 模板（从 #205/#206 缺陷提炼 5-8 条可执行条款）| Hermes → templates/ |
| 3 | kanban bridge 改 1 卡多 run（generation + 原子 claim + CAS）| Hermes → kanban_bridge.py |
| 4 | worker 交付规范：session_id + handoff 文档 → --resume 返工 | Hermes → SOUL |
| 5 | `make preflight` 统一自检命令 + required CI | Hermes/Worker |
| 6 | 唯一状态源：GitHub 八态为准，kanban 仅执行态 | 已定，固化文档 |

## 指标（P2，稳定后）

- first-pass EV pass rate；每类缺陷 escape rate；平均返工轮数
- duplicate audit attempt 数；stale verdict 被 fencing 拒绝次数
- orphan process/worktree 数；EV/PI 等待时间；CI 与 EV 重复发现率

---
*审核：codex exec 独立审核（2026-08-11）。核心修正：① 否决双卡 running
不释放（进程常驻错误方向）；② REQ 前置优于 SOUL 软约束；③ 防并发靠
原子租约 + CAS（控制平面），非状态占位。*
