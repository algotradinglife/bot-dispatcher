# pr-status-sync — 部署清单

纯原生 GitHub 自动化：PR 生命周期 → 关联 issue 的 Project Status 自动流转。
Hermes ops 拥有并维护；部署到各项目仓库。

## 行为

| PR 事件 | issue 状态 | 说明 |
|---|---|---|
| ready_for_review | → Review | PR 从 draft 转 ready，等 PI review |
| converted_to_draft | → In Progress | 工作恢复 |
| closed (merged) | → Done + **自动关 issue** | 原生 Closes 语义 |

## PR 规范（必须）

PR body 必须用 GitHub 官方关键词引用目标 issue：

```markdown
Closes #137
```

**不能用** `Part of #137` / `for #137`（不触发 closingIssuesReferences）。

语义变化：merge 后 **issue 自动关闭**（不需要手动关）。

## 部署步骤（每个 repo）

### 1. 添加 workflow 文件

```bash
mkdir -p .github/workflows
cp <bot-dispatcher>/workflows/pr-status-sync.yaml .github/workflows/
# 编辑 PROJECTS 映射为本 repo 的 project/field/option ID
```

### 2. 配置 secret

```bash
gh secret set PROJECT_SYNC_TOKEN --repo <repo>   # PAT 需 project scope（org project 写权限）
```

- ⚠️ `GITHUB_TOKEN`（内置）**不够**：org 级 project 写入需 PAT/GitHub App
- token 来源：hh1985 的 PAT（`gh auth token`），有 `project` scope

### 3. 验证

- 建测试 issue → 加进 project → 建 draft PR（body `Closes #N`）→ 转 ready
- 检查：issue 状态 → Review；merge 后 → CLOSED + Done

## 各 repo 配置（Status field/option ID）

### beijing-lot（Project 2: BJ Prediction & Betting）

```yaml
project: PVT_kwDOCnDY5c4Bed79
field:   PVTSSF_lADOCnDY5c4Bed79zhY37FA
options:
  ready_for_review:   7e18371b   # Review
  converted_to_draft: 95cfdf69   # In Progress
  closed:             96923e69   # Done
```

### paired-trading（6 个 project，ID 各异 — 部署时逐个填）

| Project | field | Review | In Progress | Done |
|---|---|---|---|---|
| 6 Data & Market State | PVTSSF_lADOCnDY5c4BefxnzhY5hXE | ae7bf80a | 2febe4fb | e3e9516e |
| 7 Strategy Research | PVTSSF_lADOCnDY5c4BefxpzhY5hZM | 4e2fd163 | 66d24ec1 | 82600318 |
| 8+ | 待查 | | | |

## 已测试

- 测试仓库 `algotradinglife/dispatcher-workflow-test`（project 12）
- ready_for_review → Todo ✅；closed → CLOSED + Done ✅（PR #10，全链路 success）

## 踩坑记录

1. `GITHUB_TOKEN` 无 org project 写权限 → 用 `PROJECT_SYNC_TOKEN`（PAT，project scope）
2. GraphQL 变量：**Int 用 `-F`**（数字推断），**String/ID 用 `-f`**（防止 "96923e69" 被当数字）
3. 纯数字 option ID（如 98236657）用 `-F` 会报 `String! invalid value` — 必须 `-f`
