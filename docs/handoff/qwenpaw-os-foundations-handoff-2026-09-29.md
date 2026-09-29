# QwenPaw OS 基础设施改造 Handoff

> 日期：2026-09-29
> 分支：`feat/lite-agent-os`
> 代码快照：`e2e227f2`（`feat(runtime): introduce qwenpaw os foundations`）
> Goal 状态：已暂停
> 钉钉副本：<https://alidocs.dingtalk.com/i/nodes/Y1OQX0akWmzdBowLFj9006eDVGlDd3mE>

## 1. 本阶段结论

本阶段已将 QwenPaw 从“按页面和旧服务堆叠能力”的结构，推进到以
Kernel、Capability、Runtime、Interaction、Invocation Control、Delivery、
Scheduling、Plugin Generation 和 Edition Profile 为核心的 OS 基础设施。

当前产品推进顺序已经明确：

1. 先保证 Chat 是主入口，并让普通聊天真实经过新基础设施。
2. 先完成领域模型、控制面、交互面、插件槽位和兼容适配。
3. Task 页面暂不作为当前验收目标；基础模块稳定后再重新设计页面。
4. 内置能力和插件能力使用同一套 Contribution / Capability / Slot 契约。
5. 新代码优先使用 `ChatSpec.id`；`session_id` 仅保留在兼容边界。

本次快照包含 507 个文件变更，新增约 8.9 万行。它是阶段性架构快照，
不是“所有 Task Workbench 功能已完成”的声明。

## 2. 已形成的基础设施

### 2.1 Kernel 与领域模型

- `src/qwenpaw/kernel/`：Conversation、Invocation、Interaction、Delivery、
  Inbox、Scheduling、Operational、Proposal、State Machine、Slot 等稳定契约。
- `src/qwenpaw/capabilities/`：能力声明、注册表与系统内置能力包。
- `src/qwenpaw/editions/`：Lite / Workstation / Hub 的能力配置与部署解析。
- `src/qwenpaw/conversations/`：Lite 对话运行边界。

### 2.2 运行控制与及时干预

- `src/qwenpaw/invocation_control/`：Queue / Steer / Interrupt 的持久化控制面。
- Steer 插入点按 reasoning 前后、工具调用前后设计，避免只在前端排队。
- 前端只投影状态并发出意图，不持有控制真相。
- Stop Gate、Hook、Tool、Prompt、Mode、Memory、Driver 等均可作为 Provider
  参与 Runtime Assembly。

### 2.3 统一交互基础设施

- `src/qwenpaw/interactions/`：统一承载审批、`ask_user_*`、建议与确认。
- Approval Bridge / Task Bridge 将旧调用迁移到统一 Interaction。
- Chat 页面已增加 Runtime Interaction Cards 和服务端队列投影。
- Interaction 的持久化、超时、状态迁移和响应入口已有定点测试。

### 2.4 Delivery、Inbox 与 Artifact

- `src/qwenpaw/delivery/`、`src/qwenpaw/inbox/`、
  `src/qwenpaw/operations/`：统一投递、收件箱投影、操作记录和 SQLite 存储。
- Task 领域已包含 Artifact、Evidence、Checkpoint、Ledger、Replay、Usage、
  Redaction、Side Effect 等基础对象和应用服务。
- Chat 输入附件、运行时媒体流和对话 Artifact 具备迁移桥接。

### 2.5 插件与二次开发

- 插件通过 Contribution / Slot 接入，而不是直接侵入核心模块。
- Generation 模型支持热安装后的新请求生效、运行中固定版本和失败隔离。
- 提供 Tool、Driver、Hook、Memory、Mode、Prompt、Stop Gate、Scheduler、
  Delivery、Task Insight 等示例插件。
- PawApp 提供任务、确认、UI Interaction 等二次开发契约，并新增
  `@app.task("/path")` 公开装饰器。

### 2.6 对话分叉

- 已形成“从某条消息创建新 Chat”的 Fork 领域与 API 设计。
- 分叉沿用消息历史快照，后续消息、运行控制和 Artifact 在新 Chat 下独立。
- 归属校验失败明确返回权限错误，避免通过 ID 猜测跨 Chat 访问。

## 3. 明确未完成或不应误判为完成的部分

- Goal 保持暂停，没有在归档或提交过程中恢复。
- Task 页面不是当前阶段验收目标。仓库中的 `console/src/pages/Tasks/`
  来自此前累计实现，只能视为早期投影/原型，不能视为最终 Workbench。
- 尚未完成浏览器级真实端到端验收：并行审批、失败恢复、热替换回退、
  Artifact/Evidence 全链路仍需在后续阶段逐项实测。
- 没有执行被项目规范禁止的全量 `npm run build`、全量
  `npm run test` 或全量 `npm run format`。
- 全项目 TypeScript 检查仍包含既有错误；本阶段只确认与改动相关的定点
  TypeScript 编译路径。
- 旧 API Route 尚未全部移除。策略是由兼容层逐步切换到新领域服务，
  不是一次性大爆炸替换。

## 4. 验证证据

### 本次 handoff 前重新验证

- PawApp 契约测试：`32 passed`。
- PawApp 相关文件 pre-commit：AST、私钥检测、mypy、black、flake8、
  pylint 等全部通过。
- `git diff --check` 与 `git diff --cached --check`：通过。
- Git 提交钩子：pre-commit、prepare-commit-msg、commit-msg、post-commit
  均通过。

### 本阶段此前已执行的定点验证

- 后端核心路径定点测试：70 项通过。
- 前端相关定点测试：74 项通过。
- PawApp 扩展测试组：72 项通过。
- 定点 TypeScript 编译（含 `hostExternals.ts`、`vite-env.d.ts` 与 JSX
  配置）：通过。

这些结果是分组定点验证，不等价于全仓测试通过。

## 5. 钉钉文档归档清单

### 框架分析目录

目录 ID：`oP0MALyR8kzGnoOwFYN2dZjkJ3bzYmDO`

| 本地文档 | 钉钉文档 | 状态 |
|---|---|---|
| `docs/analysis/qwenpaw-detailed-solution-gap-analysis-2026-09-24.md` | [详细方案差距分析](https://alidocs.dingtalk.com/i/nodes/YMyQA2dXW7gYo6Mzc1Lz0NDNWzlwrZgb) | 已同步 |
| `docs/analysis/qwenpaw-main-current-state-and-iteration-directions.md` | [主干现状与迭代方向](https://alidocs.dingtalk.com/i/nodes/Obva6QBXJwxNZoMOCLye5EMa8n4qY5Pr) | 已同步 |
| `docs/design/qwenpaw-agent-os-current-plan-2026-09-23.md` | [当前改造计划](https://alidocs.dingtalk.com/i/nodes/ZX6GRezwJlzeYoPLF0Lx4EnpWdqbropQ) | 已同步 |
| `docs/design/qwenpaw-agent-os-fused-architecture.md` | [融合总体架构](https://alidocs.dingtalk.com/i/nodes/m9bN7RYPWdyrPBREcjY9EMzBVZd1wyK0) | 已同步 |
| `docs/design/qwenpaw-compatibility-matrix.md` | [兼容性矩阵](https://alidocs.dingtalk.com/i/nodes/ZX6GRezwJlzeYoPLF0LxZbn2WdqbropQ) | 已同步 |
| `docs/design/qwenpaw-lite-agent-os.md` | [Lite Agent OS 设计](https://alidocs.dingtalk.com/i/nodes/6LeBq413JA9BOdm2iz1daGnjJDOnGvpb) | 已同步 |
| `docs/design/qwenpaw-os-infrastructure-migration-plan.md` | [基础设施迁移计划](https://alidocs.dingtalk.com/i/nodes/Y1OQX0akWmzdBowLFj9BL3zrVGlDd3mE) | 已同步 |
| `docs/design/qwenpaw-unified-task-runtime.md` | [统一任务运行时](https://alidocs.dingtalk.com/i/nodes/QG53mjyd800agdlKHe0Y9Kwo86zbX04v) | 已同步 |
| `docs/share/qwenpaw-detailed-solution.md` | [总体详细方案](https://alidocs.dingtalk.com/i/nodes/ZgpG2NdyVXRmQ0jgC7an0LpP8MwvDqPk) | 已同步 |

### 功能设计目录

目录 ID：`YMyQA2dXW7gYo6MzcZbmEdagWzlwrZgb`

| 本地文档 | 钉钉文档 | 状态 |
|---|---|---|
| `docs/design/qwenpaw-lite-migration.md` | [Lite 迁移方案](https://alidocs.dingtalk.com/i/nodes/EpGBa2Lm8aZxe5myCzqKnYMvWgN7R35y) | 已同步 |
| `docs/design/qwenpaw-conversation-fork.md` | [对话消息分叉设计](https://alidocs.dingtalk.com/i/nodes/7NkDwLng8Za7QYkeH3w54ByxJKMEvZBY) | 已同步 |
| `docs/design/qwenpaw-delivery-contract.md` | [交付契约](https://alidocs.dingtalk.com/i/nodes/r1R7q3QmWew5lo02f6qDYXpEJxkXOEP2) | 已同步 |
| `docs/design/qwenpaw-inbox-projection.md` | [Inbox 投影视图](https://alidocs.dingtalk.com/i/nodes/G1DKw2zgV2KnvL4kFvZ5a4ymJB5r9YAn) | 已同步 |
| `docs/design/qwenpaw-invocation-control.md` | [Queue Steer Interrupt 调用控制](https://alidocs.dingtalk.com/i/nodes/YMyQA2dXW7gYo6Mzc1LzOLQKWzlwrZgb) | 已同步 |
| `docs/design/qwenpaw-lite-acceptance.md` | [Lite 验收标准](https://alidocs.dingtalk.com/i/nodes/MNDoBb60VLYDGNPytaeGE1gZJlemrZQ3) | 已同步 |
| `docs/design/qwenpaw-scheduler-contract.md` | [调度器契约](https://alidocs.dingtalk.com/i/nodes/R1zknDm0WR6XzZ4Ltzj7p6x7WBQEx5rG) | 已同步 |
| `docs/design/qwenpaw-task-runtime-contract.md` | [任务运行时契约](https://alidocs.dingtalk.com/i/nodes/EpGBa2Lm8aZxe5myCzqK75llWgN7R35y) | 已同步 |
| `docs/development/plugin-quickstart.md` | [插件二次开发快速开始](https://alidocs.dingtalk.com/i/nodes/vy20BglGWOxjGpq0CvnX4LbaVA7depqY) | 已同步 |
| `docs/share/qwenpaw-interaction-delivery-model.md` | [统一交互与交付模型](https://alidocs.dingtalk.com/i/nodes/Qnp9zOoBVBDEydnQUeRvYO7B81DK0g6l) | 已同步 |

### 热点调研目录

目录 ID：`6LeBq413JA9BOdm2i3DPevLKJDOnGvpb`

| 本地文档 | 钉钉文档 | 状态 |
|---|---|---|
| `docs/share/qwenpaw-strategy.md` | [产品差异化战略](https://alidocs.dingtalk.com/i/nodes/a9E05BDRVQRkezKGCPAwXON0J63zgkYA) | 已同步 |
| `docs/strategy/qwenpaw-competitive-evidence-2026.md` | [竞品证据地图](https://alidocs.dingtalk.com/i/nodes/r1R7q3QmWew5lo02f6qDyYdjJxkXOEP2) | 已同步 |
| `docs/strategy/qwenpaw-strategy-2026.md` | [产品路线与发展战略](https://alidocs.dingtalk.com/i/nodes/N7dx2rn0JbxOaqnACNXzrQ7NWMGjLRb3) | 已同步 |

### HTML 可视化说明

`docs/share/` 中 3 个 HTML 是上述 Markdown 的可视化交付版本：

- `qwenpaw-detailed-solution.html`
- `qwenpaw-interaction-delivery-model.html`
- `qwenpaw-strategy.html`

HTML 原文件随代码快照保存在 Git 中；钉钉以对应 Markdown 作为可搜索、
可协作维护的正文，避免同一内容在知识库中形成两套版本源。

## 6. 建议的恢复顺序

1. 先用一个可复用 Chat，通过 `/clear` 清理上下文后验证普通聊天。
2. 验证 Chat 是否真实经过 Runtime Assembly、Interaction Middleware、
   Invocation Control 和 Delivery，而不是仅保留旧路径。
3. 验证审批、`ask_user_*`、suggestion 在同一 Interaction 投影中可见、
   可响应、可恢复。
4. 验证 Steer 在 reasoning 前后和工具调用前后均能及时生效。
5. 验证从指定消息 Fork 后，父子 Chat 的历史、后续消息、Artifact 和控制
   状态互不污染。
6. 验证插件安装无需重启即可对新请求生效，并验证 generation 固定与回退。
7. 完成上述基础设施验收后，再决定 Task Workbench 页面所需的最小投影和
   交互，不让页面反向定义领域模型。

## 7. 恢复工作时的硬约束

- 不恢复 Goal，除非用户明确要求。
- 不优先继续 Task 页面。
- Queue / Steer / Interrupt 的真相不能下放前端。
- 审批、询问、建议不得各自维护独立状态机。
- 内置与插件必须走同契约，不为内置能力保留隐式特权路径。
- 新领域 API 使用 `ChatSpec.id`；兼容层以外避免继续扩散
  `session_id`。
- 每完成一个模块，同时完成调用接入、定点测试和迁移说明，避免只建空目录。
