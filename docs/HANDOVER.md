# Dispatcher Handover — 交接文档

> 本文档记录 bot-dispatcher 的设计决策、运行规则与待办事项。
> 由 PI 负责维护更新；执行者（Hermes/Codex sessions）只负责执行。

## 1. 设计原则

1. **GitHub Issue Graph 是控制平面真相源** — 所有路由/依赖/生命周期决策以 `blockedBy` / `blocking` / `parent` / `subIssues` / `issueType` 为准。Issue 正文、评论、labels、本地文件只是说明，不能覆盖 Graph。
2. **Owner 由 Project 决定** — Issue 属于哪个 GitHub Project → 该 Project 配置的 `owner` 角色 → `session_map[role]` 映射到 agent-deck session。assignee 只是占位，不参与路由。
3. **PI 只关注结果** — Issue 级别的状态流转（Inbox/Ready/In Progress/Blocked）不通知 PI；只有 PR 事件（新 PR、Draft→Ready、待合并）和里程碑变化才找 PI。
4. **一个 session 一次一条消息** — 同一 tick 内多个事件指向同一 session 时，只发第一条（`_pending` 队列 + flush）。
5. **Worker 用 bot 账号，PI 用主账号** — 所有 worker session（Engineer/Strategy/Data）的 git/gh 身份是 `everything-bot-engineer`；PI 是 `hh1985`（通过 `gh-identity-pi.sh` wrapper 注入 GH_TOKEN）。

## 2. 通知矩阵

### Issue 状态变化（Project Status 列）

| 变化 | 通知谁 | 说明 |
|------|--------|------|
| Ready | → Owner | Issue 所在 Project 的 owner |
| In Progress | 无 | 静默 |
| Blocked（有 linked PR 且 CHANGES_REQUESTED） | → PR 作者 | 去改 PR |
| Blocked（无 linked PR） | 无 | 仅 warning 日志 |
| Review（PR APPROVED） | → PI | 等待 PI 审/合并 |
| Review（PR CHANGES_REQUESTED） | → PR 作者 | 去改 PR |
| Review（无 linked PR） | 无 | 仅 warning 日志 |
| Done | → Owner | 完成回执 |
| 任意变化 | → Graph 上下游 owner | blockedBy/blocking/parent/subIssues 关联的 Issue owner |

### PR 事件

| 事件 | 通知谁 |
|------|--------|
| 新 PR | → PI |
| Draft → Ready for Review | → PI |
| Review 变化（APPROVED / CHANGES_REQUESTED） | → PR 作者 |
| Merged | → PR 作者回执 |

### 其他

| 事件 | 通知谁 |
|------|--------|
| `[TO: Role]` 评论（Issue 或 PR） | → 对应 session |
| Milestone 进度变化 / 超期 / <7天 | → PI |
| Stale 卡片（>1h 无活动） | 自动 Blocked + warning（不通知） |

## 3. 配置说明

`dispatcher.yaml` 每 repo 一段：

```yaml
repos:
  <repo-name>:
    repo: <owner>/<name>
    projects:
      - number: <project-number>
        node: "<project-node-id>"      # gh project list --format json 获取
        name: "<project-title>"
        owner: <role-key>              # 引用 session_map 的 key
    session_map:
      pi: <pi-session>
      engineer: <engineer-session>
      strategist: <strategy-session>
      data: <data-session>             # 可选
    assignee_map:
      <github-login>: <role-key>
    mention_map:
      <[TO: 关键字]>: <role-key>
```

## 4. 部署 / 运行

```bash
# 本地安装（cron 引用这些路径）
cp dispatcher.py ~/.hermes/scripts/
cp dispatcher.yaml ~/.hermes/config/
cp bj-dispatcher.sh pt-dispatcher.sh ~/.hermes/scripts/
chmod +x ~/.hermes/scripts/dispatcher.py

# 手动测试
python3 ~/.hermes/scripts/dispatcher.py --repo beijing-lot
python3 ~/.hermes/scripts/dispatcher.py --repo paired-trading
```

Cron：no_agent 脚本，每 1 分钟，deliver=local，输出到 `~/.hermes/cron/output/<job_id>/`。

## 5. 已知问题 / 待办

- [ ] **代码与本地运行实例不同步** — 本 repo 是快照，`~/.hermes/scripts/dispatcher.py` 是 cron 实际运行的版本。后续修改必须同步两边，或改为 cron 直接引用 repo 路径。
- [ ] 是否重建 dispatcher cron（当前已暂停，等 PI 决策）
- [ ] PR 的 Project Status 只处理 Review，Draft/Ready 状态来自 PR 自身 `isDraft` 字段
- [ ] Milestone 扫描假设 `gh issue list --json` 输出单行 JSON，已按行解析兼容
- [ ] 首次运行（空 state）会大量补发历史 `[TO: ...]` 评论 — 属于预期行为

## 6. 身份管理

- PI session（beijing-lot-PI / paired-trading-PI）使用 `gh-identity-pi.sh` wrapper：
  ```bash
  agent-deck session set <session> wrapper "bash ~/.hermes/scripts/gh-identity-pi.sh {command}"
  ```
- worker session 无 wrapper，沿用全局 `everything-bot-engineer`

## 7. 变更流程（建议）

1. PI 在 bot-dispatcher repo 开 Issue 描述需求
2. 分配 owner（Engineer/Strategist）实施
3. PR → PI review → merge
4. PI 确认后同步部署到 `~/.hermes/scripts/`
5. 更新本文档
