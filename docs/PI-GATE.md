# PI-GATE — 机器可执行的 PI 操作门禁（v1）

> 来源：2026-08-11 PI 自提建议（用户问"如何有效约束 PI 按规范操作"）。
> 定位：dispatcher 是**流程验证器**（非科学决策者）— 校验 PI 的关键操作
> 是否满足状态机/REQ/EV-binding/终态对账，不干预科学判断。
> PI 保留路线和科学判断权，但不能跳过这些 gate。

## 设计原则

1. **dispatcher 只验证流程，不裁决科学**：gate 检查"操作是否合法/完整"，
   不判断"结论是否正确"（那是 PI 对抗性审查的职责）。
2. **阻断 vs 提醒分层**：关键 gate 阻断（非法操作不允许执行），次要项
   仅提醒（不阻塞流程）。
3. **一切 bypass 必须带 reason code + 审计记录**：任何 gate 被显式跳过
   都要留痕，可追溯。
4. **操作前给 gate receipt，操作后做 reconciliation**：PI 每次关键操作
   前收到可执行检查单，操作后自动对账。

## PI-GATE 定义（6 道）

| Gate | 检查项 | 证据 | 级别 |
|---|---|---|---|
| PI-G01 Graph | 操作前读 Project / Milestone / blockedBy / blocking / parent，确认唯一 owner | live graph receipt | 阻断 |
| PI-G02 Contract | 适用 REQ（E01-E08 + 特定条款）必须完整，每项有验收标准 | REQ→evidence 对照表 | 阻断 |
| PI-G03 EV binding | 最新有效 EV 必须 PASS，且 **EV 审计 SHA = 当前 PR HEAD** | EV SHA vs PR HEAD | 阻断 |
| PI-G04 Adversarial | 必须查 baseline / 泄漏 / 选择偏差 / 伪增 | PI adversarial receipt | 阻断 |
| PI-G05 Terminal reconciliation | 合并 / Issue Closed / Project Done / Roadmap read-back 全部一致 | 终态对账 receipt | 阻断 |
| PI-G06 Downstream activation | 下游接口重新评估后才能置 Ready（不能因 blocker 关闭自动启动）| 下游激活决定 | 阻断 |

## 阻断清单（dispatcher 校验 PI 时拒绝）

以下任一条件 → dispatcher 阻断该操作（不执行 + 报警）：

- ❌ 非 PI Review 状态尝试合并 PR
- ❌ 当前 PR HEAD 没有独立 EV PASS
- ❌ EV PASS 的审计 SHA ≠ 当前 PR HEAD（stale PASS，如并发审计场景）
- ❌ REQ 存在未裁决项（有 REQ 既非 PASS 也非 N/A）
- ❌ Issue 有未关闭的原生 blocker（blockedBy 未清）
- ❌ Issue 缺 Project / Milestone / 唯一 owner
- ❌ 非法状态跳转（如 In Progress → Done、Ready → PI Review 跳过 EV）
- ❌ 合并后 Issue 长时间未关闭 / Project 未置 Done
- ❌ 下游 issue 在 PI 明确授权前置 Ready（G06）

## 提醒清单（不阻断，仅提示）

- ⚠️ Roadmap 尚未更新（merge 后应更新 docs/research/roadmap.md）
- ⚠️ PI Review 停留过久（超阈值提示）
- ⚠️ 非关键 REQ 证据链接不够清晰

## 实现建议（dispatcher 侧）

1. **gate 检查函数**：`check_pi_gates(action, repo, cfg)` 返回
   `{gate: (PASS/FAIL/REMIND, evidence)}`，对每个 PI 关键操作调用。
2. **阻断执行**：dispatcher 检测到 FAIL gate → 不投递/不推进，输出
   warning（进用户 digest，GitHub warnings 白名单已有）。
3. **reconciliation**：merge/close/Done 后下个 tick 对账（Issue 关闭?
   Project Done? Roadmap 更新?），不一致 → warning。
4. **reason code**：显式 bypass 需 reason code（如 `REASON=BYPASS_G03_STALE_EV_OVERRIDE`）
   记入 state，可审计。
5. **EV SHA binding**：EV PASS 评论里要求含 `AUDITED_SHA=<head>`，dispatcher
   比对 PR 当前 HEAD — 不一致即 stale（根治并发审计）。

## 实施状态

- [ ] 文档固化（本文件）
- [ ] `check_pi_gates` 实现（bot-dispatcher dispatcher.py 或独立模块）
- [ ] 阻断清单接入 dispatcher 主流程
- [ ] reconciliation 对账
- [ ] EV SHA binding（EV 报告规范 + dispatcher 比对）
- [ ] 测试

---
*相关：templates/REQ-template.md（E01-E08）、docs/rework-retrospective-2026-08-11.md*
