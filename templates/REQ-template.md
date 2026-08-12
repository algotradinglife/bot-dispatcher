# REQ 模板 — 通用可执行验收条款（跨 issue 复用）

> 用途：PI 开 issue 时，把验收标准编号为 REQ（每条可执行验证）。
> worker 交付时逐条对照（REQ → evidence），EV 按 REQ 编号 verdict。
> 通用条款（本文）跨 issue 复用；issue 特定条款在契约中补充。
>
> 来源：2026-08-11 复盘（#205/#206 返工缺陷提炼）+ codex 审核。
> 核心原则：REQ 必须"能跑命令验证"，空话不写；类别按失败频次排序。

## REQ-E01 — Clean-run 命令可执行（#205-①、#206-① 教训）

**条款**：README/契约中所有文档化命令（notebook 执行、verify、replay）必须
在**干净 checkout**（无 data/、无 .venv、无 worktree 残留）中逐字执行成功。

**验证**：
```bash
git clean -fdx && git checkout <HEAD>
# 按 README 逐字执行每个文档化命令（含路径、环境、参数）
# 每条命令必须 exit 0
```
**对抗测试**：notebook 执行用 `--kernel python3`（安装的 kernelspec），
不得依赖自定义/未注册内核名；模块导入不得依赖隐式仓库根路径。

## REQ-E02 — 输入 fail-fast（#205-②、#206-② 教训）

**条款**：所有读入（panel/矩阵/ticket 源）必须对 malformed 数据**显式失败**，
不得静默归一化/丢弃/截断。校验项：finite、严格正、simplex 和=1（容差显式）、
唯一键（issue/match/split/model/seed）、最小规模（如 M≥7 或显式 NO_BET）。

**验证**：对抗测试覆盖 NaN/Inf/零概率/非归一化行/重复键/空输入/少场次 —
每条必须显式抛错或走显式 NO_BET 路径，且有 exclusion ledger（若契约允许排除）。

## REQ-E03 — 依赖与环境锁定（#205-④ 教训）

**条款**：`requirements.txt` 必须精确版本（`==`，无 `>=`）；科学依赖实测版本
与 provenance 记录一致；replay 命令绑定 requirements 的 SHA-256。

**验证**：fresh venv 从提交 requirements 安装 → `pip freeze` 关键包版本
== provenance 记录 → replay 产出与提交 manifest byte-identical。

## REQ-E04 — 原子写与异常清理（#205-③ 教训）

**条款**：所有持久化（notebook、artifact、JSON/Markdown）用同目录临时文件 +
`flush/fsync` + `os.replace`；异常时显式递归清理；输出已存在则 fail-closed
（不覆盖 immutable package）。

**验证**：中断写入测试（kill -9 模拟）→ 原内容不变、无半文件；异常路径
清理测试通过。

## REQ-E05 — 证据链三层完整（契约硬性）

**条款**：交付含 ① 可执行 notebook（clean-run）② 可复用代码（package-first）
③ 生成工件（机器可读 + 图 + decision report + schema + trace + provenance
+ SHA256 manifest）。

**验证**：`verify` 命令 18/18 payloads（或契约数量）→ manifest SHA-256
与提交一致 → 独立 replay 到新目录 byte-identical。

## REQ-E06 — 无泄漏与溯源 + 强制 tamper 自测（#206-03~05 + #207-02/03 教训）

**条款**：标签/事后信息不得进入特征/选择/结算（如 final-SP 仅输出通道）；
每 ticket 的 source trace 必须绑定回冻结源矩阵（cell 级）；不确定性/概率/
SP 输入可溯源。semantic verifier 必须覆盖**全部结算字段**（realized_hit/
realized_gross/SP/概率/不确定性等），且能**从冻结源头逐行重算** ledger
（不是只查内部自洽）。

**验证**：
- 极端改写标签值 → 全链路 byte-identical（无泄漏）
- semantic verifier 拒绝伪造/不匹配的 source trace；OOD fallback 显式
- **tamper 自测（强制，worker 交付前必做）**：worker 必须编写并运行
  对抗性 tamper 测试，攻击自己的 verifier — 至少覆盖：
  1. 篡改结算字段（realized_gross/payout 改值）→ verifier 必须 FAIL
  2. 篡改 ledger 某行但保持会计等式成立 → verifier 必须 FAIL
  3. 重写 manifest 伪造 → verifier 必须 FAIL
  4. 伪造 source trace / 改 label / future 信息 → verifier 必须 FAIL
  **任何 tamper 测试通过 verifier = 交付缺陷**（EV-B207-02/03 模式：
  verifier 字段覆盖不全被伪造穿透，已两次发生，必须根治）
- tamper 测试本身必须提交（tests/ 下），EV 会重跑并追加自己的攻击

## REQ-E07 — 科学结论边界（PI 层，防过度外推）

**条款**：结论必须严格限定在契约范围（候选池/特征/方法）；"X 没进步"≠
"Y 是答案"；对照 vs 实验通道命名不混淆（control_/experimental_ 前缀）；
未晋级结果必须标 source grade + promoted=false + confirmation dependency。

**验证**：decision report 的结论与契约 scope 逐句对照；命名约定检查。

## REQ-E08 — 交接块（返工上下文连续性）

**条款**：交付评论含 `[SESSION] <session_id>` + HANDOFF 摘要（HEAD/已完成/
关键决策/已知问题/下一步）+ 状态指令。

**验证**：评论含三要素；RESUME_SESSION 注入后返工 run 可 --resume 续接。

## REQ-E09 — 返工同类问题完整排查（2026-08-12 定，#207 R1→R3 教训）

**条款**：EV REJECT 返工时，worker 不仅修所列 defect，还必须对每个
defect 的**根因类别**做完整排查 — 检查该类别的所有出现点（不只是
EV 指出的那一个），并提交排查证据。

**验证**：
- 返工交付附"同类排查声明"：每个 defect 归类 + 该类别的完整检查范围
  （如泄漏 → 选择函数+comparator+replay+汇总；fsync → 所有写点+
  目录+rename+异常路径；指标 → 所有窗口/聚合的局部 vs 全局语义）
- 排查证据（检查了哪些调用点/文件/路径）随交付提交
- EV 复审时抽查声明覆盖度 — 找声明之外的同类点（找到 = 声明不完整）

## REQ-M — 方法协议冻结（delivery-improvement v2，可选但研究/模型类必带）

> 契约带 `method.yaml`（见 templates/method.yaml.example）时启用。
> 治 R2 类方法缺陷（#207 R2-01 模型选择泄漏教训）。
> **偏简版（风险前置）**：先做声明 + 自检，不强制物理隔离；
> 若 pilot/失败数据显示兜不住，再逐级加码（EV 认可修订 →
> holdout 隔离 runner → 独立复算基准）。

**条款**：模型/消融选择应在 `selection_split` 冻结；`terminal_split`
只执行一次冻结后的 terminal decision。指标（HHI/rolling/回撤）按
method.yaml 的 definition 实现 + golden tests（worker 自写，EV 抽查）。
协议变更记入 PROTOCOL-LOG（追加式，`supersede` 引用旧行）—
**修订无需 EV 预先批准**，但 EV 审计时抽查一致性；涉及 split/选择
算法的重大修订，worker 应在交付评论中显式标注。

**验证**：
```bash
python method_preflight.py --worktree <交付路径>   # exit 0 = PASS
```
- REQ-M01 协议冻结：method.yaml 存在 + schema 合法 + receipt hash 匹配
- REQ-M02 声明一致性：holdout_isolated 声明与实际执行一致
  （物理隔离不强求；false 时 report 显式记录剩余风险）
- REQ-M03 指标定义：golden tests 存在（EV 抽查语义正确性）
- REQ-M04 协议修订：PROTOCOL-LOG 追加式完整 + 无隐藏修订

---

## Worker 交付对照表格式

```markdown
| REQ | 证据 | 状态 |
|-----|------|------|
| E01 | notebooks/research/issue-N/…（clean-run 截图/日志）| PASS |
| E02 | tests/test_…（对抗测试 12 passed）| PASS |
| … | … | … |
```

## EV 报告按 REQ verdict 格式

```markdown
| REQ | EV 验证 | Verdict |
|-----|---------|---------|
| E01 | 独立 clean checkout 执行文档命令 | PASS |
| E02 | 对抗测试重跑 | PASS |
| … | … | … |
```
