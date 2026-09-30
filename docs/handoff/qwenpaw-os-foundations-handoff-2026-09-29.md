# QwenPaw OS 基础设施改造 Handoff

> 日期：2026-09-29
> 分支：`feat/lite-agent-os`
> 代码快照：`b190dea9`（`fix(governance): enforce strict approval per invocation`）
> Goal 状态：执行中
> 钉钉副本：<https://alidocs.dingtalk.com/i/nodes/Y1OQX0akWmzdBowLFj9006eDVGlDd3mE>

## 0. 当前 Goal

### 0.1 Goal 元数据

| 字段 | 当前值 |
|---|---|
| Thread ID | `01a0c21e-e72a-75c2-aa72-52508ca3099a` |
| 状态 | `active` |
| 创建时间 | 2026-09-22 |
| 最后更新时间 | 2026-09-30 |
| 当前约束 | Chat-first；Task Workbench 继续后置 |

### 0.2 Goal 原文

> 重构 QwenPaw Lite 的任务运行时，使现有内置能力与插件能力在同一套
> 领域模型、注册机制和执行管线下完整对齐，并以真实可交互 Task
> Workbench 交付。先盘点当前 Chat/Console/Tool Guard/Driver/Harness/
> Plugin/Task/Artifact 等能力与缺口，形成可追踪矩阵；冻结 Task、Run、
> Plan、Conversation、Approval、Artifact、Evidence、Capability、
> Contribution、Checkpoint 的稳定 API 与事件契约；重新设计统一 Runtime
> Orchestrator，使内置模块和插件都通过相同 Slot/Contribution/Capability
> 接口接入，支持热安装即生效、generation 隔离、运行中固定版本及失败
> 回退；打通任务创建、计划、实时对话、工具调用、单个及并行审批、批准/
> 拒绝后的继续执行、Artifacts/Evidence 产出、取消/失败/恢复和审计时间线；
> Task 页面必须直接呈现执行计划、完整消息、实时状态、所有待审批及其风险
> 与参数、决策结果、成果预览和恢复入口，不依赖旧 Console 私有状态或仅靠
> 事件猜测；兼容现有 QwenPaw 能力并提供迁移适配层，明确哪些旧路径保留、
> 弃用或移除。验收以功能矩阵逐项通过、内置与插件同契约测试、严格审批
> 模式真实端到端演示、多个并行审批测试、插件热激活/替换/回退测试、任务
> 产出 Artifact 与 Evidence 测试、失败恢复测试、前后端定点测试和浏览器
> 实测为准；禁止用 mock 页面或“组件存在”代替运行链路验证，并产出架构
> 说明、API/事件规范、二次开发指南和未覆盖边界清单。

### 0.3 Goal 验收口径

- [ ] 现有 Chat、Console、Tool Guard、Driver、Harness、Plugin、Task、
  Artifact 能力形成可追踪矩阵。
- [ ] Task、Run、Plan、Conversation、Approval、Artifact、Evidence、
  Capability、Contribution、Checkpoint 的 API 和事件契约冻结。
- [ ] 内置能力与插件能力通过同一 Slot / Contribution / Capability 接口。
- [ ] 插件安装后无需重启即可对新请求生效。
- [ ] Runtime Generation 支持隔离、运行中固定版本和失败回退。
- [ ] 任务创建、计划、实时对话和工具调用形成真实运行链路。
- [ ] 单个审批、并行审批、批准、拒绝及继续执行完成真实验证。
- [ ] Approval、`ask_user_*` 和 suggestion 归一到 Interaction 基础设施。
- [ ] Queue / Steer / Interrupt 在服务端控制，并覆盖 reasoning 与工具调用
  前后的及时干预点。
- [ ] Artifact、Evidence、Checkpoint、审计时间线可持久化并可恢复。
- [ ] 支持从指定消息 Fork，新旧 Chat 的后续状态互不污染。
- [ ] 旧路径的保留、弃用、移除和兼容适配范围明确。
- [ ] Task Workbench 展示完整计划、消息、实时状态、审批、成果和恢复入口。
- [ ] 严格审批、失败恢复、插件热激活/替换/回退完成浏览器端到端实测。
- [ ] 交付架构说明、API/事件规范、二次开发指南和未覆盖边界清单。

### 0.4 当前阶段与原 Goal 的关系

当前 Goal 已恢复执行，仍处于“OS 基础设施与契约底座”阶段。根据后续讨论
形成的实施顺序，Chat 仍是当前主入口，Task 页面暂缓；这属于执行顺序调整，
不代表删除 Task Workbench 的最终验收要求。当前先按第 6 节验证 Chat 真实
切换到新基础设施，再继续 Task Workbench。

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

- Goal 已恢复，但 Task Workbench 仍按用户要求后置。
- Task 页面不是当前阶段验收目标。仓库中的 `console/src/pages/Tasks/`
  来自此前累计实现，只能视为早期投影/原型，不能视为最终 Workbench。
- 尚未完成浏览器级真实端到端验收：运行失败恢复、Steer 安全点和
  Artifact/Evidence 全链路仍需在后续阶段逐项实测；并行审批与插件热替换回退
  已完成。
- 没有执行被项目规范禁止的全量 `npm run build`、全量
  `npm run test` 或全量 `npm run format`。
- 全项目 TypeScript 检查仍包含既有错误；本阶段只确认与改动相关的定点
  TypeScript 编译路径。
- 旧 API Route 尚未全部移除。策略是由兼容层逐步切换到新领域服务，
  不是一次性大爆炸替换。

## 4. 验证证据

### 2026-09-30 Chat 治理与 Interaction 浏览器验收

- 修复 `GovernancePolicy` 对 internal 工具在 STRICT 判定前直接放行的问题；
  execution level 改为单次 Invocation 参数，不再写入共享 Governor Policy。
- 治理相关定点测试：`107 passed`；OFF、统一注册、请求级审批与 Interaction
  相关补充组：`64 passed`；相关文件 pre-commit 全通过。
- 固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 在 `/clear` 后完成真实
  浏览器验证：
  - STRICT 下 `GetCurrentTime` 先生成 durable Approval Interaction，批准后执行并
    完成消息；
  - 同一 Invocation 的 `GetCurrentTime` 与 `GetTokenUsage` 同时生成两条审批，
    分别批准后并行工具批次完成；
  - 拒绝后工具不执行，Agent 收到终局拒绝且不重试；
  - 审批等待时 Interrupt 会同时结束 Submission 与 blocking Interaction，页面显示
    “已取消”，后端无 active Submission 或 open Interaction；
  - `AskUser` 先经过 STRICT Approval，再显示阻塞选项，选择“绿色”后继续生成
    最终消息；
  - `SuggestUserAction` 在对话完成后仍保留非阻塞建议，用户处理后才关闭。

上述验证使用真实模型、真实工具、真实持久化与浏览器交互，不是 mock 页面或
组件存在性检查。

### 2026-09-30 插件热生命周期浏览器验收

- 验收前 `/api/plugins` 为空；向运行中的后端安装公开示例
  `chat-tool-provider` 1.0.0，未重启服务。
- 固定 Chat `/clear` 后真实调用 `describe_qwenpaw_invocation`，页面返回 1.0.0
  实现结果，证明安装不仅更新管理面，也进入新 Invocation 的 Runtime Assembly。
- 强制热替换为 1.1.0 后，再次 `/clear` 并调用同一工具，页面原样返回
  `PLUGIN_HOT_V2`，证明新 Invocation 切换到新 generation。
- 尝试替换为缺少完整 `tool.provider` 契约的 2.0.0 时，安装 API 返回 HTTP 400；
  Registry 仍报告健康的 1.1.0。随后浏览器再次调用仍返回 `PLUGIN_HOT_V2`，证明
  失败 bundle 在 generation 发布前关闭失败，没有污染稳定实现。
- 卸载返回 HTTP 200，随后 `/api/plugins` 为 `[]`；安装、替换、回退和卸载全程
  使用同一后端进程，没有重启。

该验收覆盖真实 Plugin API、Capability Registry、generation 发布、Runtime
Assembly、Tool Guard、STRICT Approval、模型工具调用及浏览器结果，不以单元测试
或静态 manifest 检查代替运行链路。

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
6. 插件安装即生效、generation 热替换及失败回退已验收；后续只需在新增 Slot 时
   复用同一门禁，不再重复实现私有插件生命周期。
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
