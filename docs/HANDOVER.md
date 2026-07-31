# Dispatcher Handover — 交接文档

## 定位

`bot-dispatcher` 是 PI 管理 Codex 团队的 no-agent GitHub 通知层。它每个
cron tick 读取 GitHub Issue Graph、Project、Issue/PR 评论和 PR 状态，把变化
送到对应 Codex session。它不启动 Hermes，也不进行模型推理。

Dispatcher 只传递事实，不决定 roadmap，不修改 Issue Graph 或 Project
Status，不合并 PR，不关闭 Issue，不授权部署。

## 真相源与 Owner

1. 依赖和拆解：GitHub Issue Graph 的
   `blockedBy/blocking/parent/subIssues/issueType`。
2. 生命周期：GitHub Project `Status`。
3. 职能 Owner：Issue 的唯一 Project membership → 配置的 owner role。
4. PR 执行者：优先使用 PR 关联 Issue 的 Project owner；只有没有关联 Issue
   时才使用 GitHub author fallback。共享 bot 账号不能决定 Engineer/Strategist。

GitHub Graph 或 Project 读取失败、分页游标异常、Issue 同时落入多个配置
Project 时，本轮生命周期路由 fail closed。

## 通知策略

- Ready → Issue 的 Project owner。
- Review → 无整改时通知 PI；有 Changes Requested 时通知关联 Issue owner。
- Done → Issue owner 回执。
- Graph 上下游变化 → 相关 Issue owner。
- 新 PR、Draft→Ready、milestone 风险 → PI。
- `[TO: Role]` → 对应 role session。
- merge/review 回执 → 关联 owner 或 author fallback。

同一 tick 内，同一 session 的所有事件组成一条 digest；不能只保留第一条。
只有全部 digest 发送成功才提交 dedup state。发送失败保留旧 state，下次重试。
不提前在 GitHub 写“已派发成功”。

## 运行

配置默认读取仓库根目录的 `dispatcher.yaml`，状态默认写入
`~/.local/state/bot-dispatcher/`。

```bash
python3 -m pip install -r requirements.txt
./bj-dispatcher.sh
./pt-dispatcher.sh
```

cron 应直接调用审核后的仓库 checkout wrapper，不复制
`dispatcher.py` 到其他目录。cron 是 no-agent local notification job。

可选环境变量：

- `BOT_DISPATCHER_CONFIG`
- `BOT_DISPATCHER_STATE_DIR`

## 测试与部署

```bash
python3 -m pip install -r requirements-dev.txt
pytest -q
```

变更必须经过 Issue → PR → PI Review → PI Merge。合并不等于部署；cron
切换或恢复必须由 PI 单独授权。部署前应使用临时 state 目录运行一次，
核对分页、目标 session、digest 数量及失败重试，不向生产 session 发送测试消息。

## 当前边界

- 没有自动 Blocked、自动 Review 或 stale lifecycle mutation。
- 不创建新的服务、数据库或消息中间件。
- 不让 Hermes session 参与维护或运行。
- PI 保持 roadmap 总览，Dispatcher 只降低轮询成本。
