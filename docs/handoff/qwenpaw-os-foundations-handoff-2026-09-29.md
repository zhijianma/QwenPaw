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

### 2026-10-02 模型资源等待与长程续行

- `ModelCallResult.wait_resource` 已接入独立 `ModelResourceWait` source of truth；
  限流按 timer，额度耗尽按 external event，不复用用户 Queue 或 Interaction。
- 等待成熟或释放后，durable outbox 创建新的 Submission / Invocation，沿用原
  `ChatSpec.id` 与 `correlation_id`；稳定 idempotency key 覆盖 enqueue 后崩溃窗口。
- Chat `wait-conditions` 合并资源等待与 Interaction 等待，Task 页面没有提前开发。
- `ModelOutputBoundary` 已区分 pre-output、完整响应、部分流、终态流与 incomplete
  EOF；未见终态 chunk 不再被记为成功。无内容时允许 transport retry，已有内容时
  禁止整请求重放。边界只留枚举证据，不保存模型输出或隐藏 reasoning；部分流现在
  由 bounded Model Step continuation 创建新的 Invocation 继续。
- “一问一答”仅保留为短 Chat 快速路径；长程工作由 Agent Loop、typed wait、
  continuation 和 checkpoint 推进，不要求用户发送“继续”。
- 设计与未完成边界见
  `docs/design/qwenpaw-model-resource-recovery.md`。真实 retry-after、Provider health
  自动释放和副作用自动对账仍待完成；部分流 continuation boundary 已完成。

- Goal 已恢复，但 Task Workbench 仍按用户要求后置。
- Task 页面不是当前阶段验收目标。仓库中的 `console/src/pages/Tasks/`
  来自此前累计实现，只能视为早期投影/原型，不能视为最终 Workbench。
- 尚未完成浏览器级真实端到端验收：运行失败恢复、Steer 的 reasoning 前与工具
  admission 前边界、Artifact/Evidence 全链路仍需在后续阶段逐项实测；并行审批、
  `AFTER_REASONING`/`AFTER_TOOL_BATCH` Steer 与插件热替换回退已完成。
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

### 2026-09-30 Steer 安全点浏览器验收

- 通过服务端控制 API 提交的 Steer 已真实记录 `after_reasoning` 回执，并作为可见
  用户消息进入 Conversation，随后驱动 Agent 继续 reasoning。
- 为排除 Shell 自动后台化干扰，临时热安装公开 `tool.provider` 慢工具；监控器先
  观察到工具 `running` 且 Submission active，再提交 Steer。
- 工具没有被 Steer 静默取消，先提交 `SLOW_TOOL_COMMITTED`；控制回执随后变为
  `applied_at_safe_point=after_tool_batch`，Chat 最终只回复
  `STEER_AFTER_TOOL_BATCH_OK`。
- 对照路径确认已进入 background offload 的工具不再属于 active Invocation，此时
  Steer 明确失败为 `no active invocation`，不会误伤后台任务。
- 临时插件已卸载，插件列表恢复为空，临时源码未进入 Git。

该验收证明运行中工具批次的不可变事实边界和 `AFTER_TOOL_BATCH` 及时干预；
`BEFORE_REASONING`、`BEFORE_TOOL_BATCH` 以及长模型流协作取消仍需分别保留真实链路
验收，不能由本项外推。

### 本次 handoff 前重新验证

- PawApp 契约测试：`32 passed`。
- PawApp 相关文件 pre-commit：AST、私钥检测、mypy、black、flake8、
  pylint 等全部通过。
- `git diff --check` 与 `git diff --cached --check`：通过。
- Git 提交钩子：pre-commit、prepare-commit-msg、commit-msg、post-commit
  均通过。

### 2026-10-08 模型恢复 Interrupt fencing

- Resource Wait 新增原子 `dispatch_ready` 与 conversation cancel；取消和恢复入队在
  Lite 进程内串行，`waiting/ready` 可稳定终结为 `cancelled`。
- `Interrupt Current` 通过来源 `invocation_id` fencing 迟到恢复；
  `Stop and Clear` 同时取消该 Chat 尚未入队的 Resource Wait。
- dispatcher 先捕获 Queue revision、再读取持久化 Control，恢复 enqueue 使用乐观
  revision；Stop 若插入检查与 enqueue 之间，旧提交会冲突而不是复活。
- 恢复 Submission 先持久化但不唤醒 consumer；Wait 绑定完成后才启动执行。服务重启
  同样先修复 Resource Wait outbox，再恢复通用 Queue consumer。
- 恢复执行入口要求 Wait 为 `dispatched` 且绑定当前 `submission_id`，取消态和伪造
  envelope 均失败关闭。
- 定点验证覆盖 Wait 取消、单次 dispatch、HTTP Stop、crash-after-enqueue 与确定性
  Stop/enqueue 竞态；该模块不涉及 Task 页面。

### 2026-10-08 Provider Retry-After 持久化等待

- 统一 Model Error Policy 解析 `Retry-After` delta-seconds 与 HTTP-date，拒绝负数、
  NaN 和 infinity；Provider 原始 header 不进入 Kernel。
- `ModelRecoveryDecision` / `ModelCallResult` 只保存内容安全
  `retry_after_seconds`，领域校验限制其只能用于 `rate_limited + wait_resource`。
- Resource Wait 优先使用 durable hint 计算 `not_before`，缺失时回退 Lite 默认 60 秒；
  重启后沿用首次记录，不按新配置漂移。
- 共享 WaitCondition 投影公开可选 `not_before`，Chat 可展示可重试时间而无需读取
  Provider 私有异常或前端自行猜测。

### 2026-10-08 Transport 短重试与 durable recovery 预算

- 实际装配顺序已确认是 Provider → TokenRecording → Retry，多个候选模型外层再由
  Fallback 管理；因此每个真实网络 attempt 独立留证，而 durable Wait 只在整个
  logical call 最终失败时创建。
- `transport_unavailable` 与 `provider_overloaded` 在短 retry/fallback 耗尽后进入独立
  timer Wait，释放当前 Invocation 和模型连接，不要求用户发送“继续”。
- Lite 将 transport timer 与 rate-limit timer 分开配置；transport、provider
  overload 和 rate limit 共用跨 Invocation 自动 timer budget，默认最多 3 个 cycle，
  并按原 `correlation_id` 统计。
- cycle budget 耗尽形成持久化 `recovery_exhausted`，通过 WaitCondition 投影为
  `expired`；不静默丢弃，也不会继续定时复活。

### 2026-10-08 部分模型流的 bounded Model Step continuation

- `stream_interrupted + continue_model_step` 且已有输出时，持久化内容安全的
  `ModelStepContinuation`；只保存身份、边界和状态，不保存半截正文或 reasoning。
- continuation 使用新的 Submission / Invocation、原 `ChatSpec.id` 与 correlation；
  稳定幂等键覆盖 crash-after-enqueue，默认最多自动恢复 2 个 cycle。
- 后续模型输入从 durable context 重建；partial stream 不提交为最终 Assistant
  Message，也不作为已提交上下文重放。
- 已修复旧 Runtime 对所有异常统一注入 Envelope partial 的冲突：Model Call 在
  continuation 落库后抛出 typed `ModelStepRecoveryError`，Runtime 保存失败 Turn 时
  跳过 partial 注入；用户主动取消仍保存已展示 partial，不改变现有交互预期。
- Stop / Interrupt、Queue revision 和执行前反向绑定共同阻止迟到恢复；工作目录缺失
  时 action reconciliation 失败关闭。
- dispatcher 扫描来源 Invocation 的 `ActionRequest`；发现任何 Action 即进入
  `action_reconciliation_required`。现在会从真实 `ActionRecord` 生成并持久化内容安全
  assessment，区分 pending result、uncertain side effect 和 durable context
  required；仍不会自动重做或猜测副作用结果。
- terminal/certain Action 现在会进一步校验 Agent snapshot 中每个 executor item 的
  `CommittedActionItem`。同步 ToolResult 与同 Invocation 内进入上下文的后台完成 hint
  共享 Action ID、Invocation、executor item ID、observation digest 四元 binding；随后
  保存权限 `0600` 的不可变私有 snapshot，并发布内容安全
  `ModelStepContextCheckpoint`。dispatcher 可修复 snapshot 已写但 checkpoint 尚未绑定
  的崩溃窗口；该修复路径与 Runtime 首次 checkpoint 共用 `CommittedActionItem`
  验证器，后台 hint/Harness session message 重启后仍可识别。pending/uncertain 继续
  失败关闭。
- checkpoint 绑定来源 Submission；如果之后已有新 Submission 被接受，旧 continuation
  取消而不是覆盖新上下文，enqueue race 继续由 Queue revision 关闭。
- 受控 Codex/Qoder Harness 的 ActionResult 持久化后，Tracker 生成内容安全的
  `CommittedActionItem`；Session Bridge 仅在对应规范化 tool output 原子写入 Chat
  context 时附加 binding。provider completion event、未知终态和 history hydrate 不会
  被误当成 model-visible commit。若 Action 成功但 session 写入失败，Harness
  Invocation 失败关闭并保留成功 Action 证据，后续不得自动重做。
- Harness 断流现在只有在 provider context identity、provider history tool item、Chat
  session `CommittedActionItem` 与 ActionStore digest 四方一致时，才保存权限 `0600`
  的 `HarnessRecoveryContextCheckpoint`。原始 provider thread/session ID、工具输出和
  异常正文均不进入 checkpoint；受控 Harness 自建 Submission 也已回填统一的
  submission/correlation identity。
- 已通过 admission 的 Harness checkpoint 会生成持久化
  `HarnessStepContinuation` outbox，并由共享 Chat Submission dispatcher 创建新的
  Invocation。stable idempotency key 覆盖 enqueue 后、outbox 标记前崩溃；Stop /
  Interrupt、来源后的新输入、Queue revision、执行前反向绑定与原 backend 一致性
  共同 fencing 迟到恢复，默认最多自动恢复 2 个 cycle。
- 状态经现有 Chat Runtime Observation 展示为 pending / accepted / cancelled /
  blocked / failed，不伪造 Queue 项，不依赖 Task 页面或前端私有状态。
- Kernel/SDK 新增只读 `ModelRecoveryHistoryPort`；独立查询 Adapter 从同一恢复事实
  读取 Resource Wait 与 Model Step，Chat snapshot、列表和分页均消费该 Port。
- Resource Wait 的 waiting / ready / dispatched / cancelled / exhausted 现在也进入
  Activity；投影只含失败类别、触发方式、`not_before`、状态和因果 ID，不含 Provider
  payload、Prompt、异常正文或凭据。
- “一问一答”只保留为 Chat 快速路径和 UI 投影；长程执行链只在 Outcome、显式
  Stop / Interrupt、不可自动化的 typed Wait 或预算/恢复终态结束，不依赖用户发送
  “继续”。
- 尚未完成：Provider token 级原地续传，以及真实断流、后台长工具与进程重启的
  浏览器端到端演练。

### 2026-10-08 Harness durable continuation

- Kernel 冻结 `HarnessStepContinuation` 的 ready / dispatched / cancelled /
  recovery-exhausted 生命周期；outbox 只保存内容安全 checkpoint 和因果身份。
- Harness Runtime 完成四方恢复 admission 后立即写入 outbox；服务启动和 worker
  唤醒都会扫描 ready 项，不依赖浏览器连接或用户发送“继续”。
- Workspace 在执行恢复 Submission 前强制校验 `harness_backend` 与当前 Agent 配置；
  backend 已切换或回到原生 qwenpaw 时 fail closed，避免把 Provider 私有上下文交给
  错误执行器。
- 本切片定点验证：38 项通过；该数字仅覆盖恢复 Store、Harness Runtime、Workspace
  路由与 Chat dispatcher，不等价于全仓或浏览器端到端测试。

### 2026-10-08 后台 Action durable continuation

- Action Recorder 在结果 digest 确定后先保存未发布的私有 background context
  snapshot，ActionResult 成功持久化后才发布 outbox 并允许 supervisor 通知；进程在
  两阶段之间退出时，dispatcher 只有在 ActionStore 精确匹配 action、invocation 与
  observation digest 后才从 snapshot 修复 outbox。
- continuation 等待来源 Submission 进入权威终态；只有来源成功、无 Stop /
  Interrupt、无更新的用户输入，且原 Session 没有消费同一 `CommittedActionItem`
  时才进入 ready。
- SessionLoadHook 把已绑定结果合并到最新 Session，而非用旧 snapshot 覆盖；同一来源
  的多个后台 Action 可以形成兄弟 continuation，顺序合并且各自保持幂等。
- 进程内 pending hint 在注入前按 binding 去重；跨 Invocation 恢复不产生重复结果。
- 本切片相邻契约验证：118 项通过；仍需真实浏览器长工具与进程重启演练。

### 2026-10-08 Conversation Execution Chain 投影

- Kernel 新增 `ConversationExecutionChain` 与 `ConversationExecutionState`，Chat
  Runtime 从权威 Submission / Interaction 按 correlation 派生，不建立第二事实源。
- 同一意图可跨多个 Submission / Invocation；blocking Interaction 投影为
  `waiting_user`，后续 continuation 重新进入 `running`。
- Submission `succeeded` 只投影为 `inactive`，不从 assistant message、HTTP response
  或 SSE 结束推断业务完成。Outcome Store、Verification 联动与 Trajectory 已在后续
  2026-10-08 切片实现，详见下文。
- Runtime SSE 仅读取最近 200 个 Submission 并合并当前 Queue，返回
  `execution_window_truncated` 明示截断，不调用全量 Observation 重建接口。
- 本切片相邻定点验证：64 项通过；覆盖领域校验、游标、Runtime API、SQLite/Service
  合同以及同一 correlation 跨两次 Invocation 的状态变化。
- 运行中的固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 已通过真实
  `GET /api/chats/{id}/runtime` 返回 v3 cursor；历史窗口明确标记截断，成功
  Submission 均为 `inactive`，失败 Submission 为 `failed`，没有产生伪
  `completed` 状态。

### 2026-10-08 显式 Conversation Outcome

- Kernel 新增 `ConversationOutcomeStatus`、`ConversationOutcome` 与
  `ConversationOutcomeStore`；Outcome 和 Invocation / Submission completion 分离。
- Lite SQLite Store 要求同一 correlation 的后续 Outcome 显式 supersede 最新记录；
  相同 ID 可幂等重放，冲突内容、跳过 supersession 和时间倒退均 fail closed。
- Outcome 数据库及 WAL/SHM 在 POSIX 上收口为 `0600`；公开链路只返回显式业务摘要和
  Artifact/Evidence/Verification 引用，不保存隐藏推理。
- Chat Runtime 只有在 Outcome 不早于最近 Submission 且当前没有 active / queued /
  blocking Interaction 时投影 achieved、partial、not_achieved 或 abandoned；新输入会
  让旧 Outcome 退出当前状态。
- 固定真实 Chat 的 Runtime API 在迁移后返回 `outcome_count=0`，原历史继续保持
  inactive / failed，没有为旧 assistant message 自动补写 Outcome。
- Host Outcome Broker 已实现 producer admission：system/plugin producer 使用同一入口，
  支持 Host 热注册/注销，且不直接访问 Store；普通 Chat 的 Artifact/Evidence 引用按
  ChatSpec 归属校验。
- Task-owned 声明校验 Agent、ChatSpec、Task、Run 与 correlation；只有既有
  `validate_result_projection()` 同时通过 Execution Contract、Verification Policy 和
  Result Package 时才允许 achieved，并由 Host 写入权威 Artifact/Evidence/Verification
  引用。
- Broker 已装配到 Workspace 单例和真实 Invocation Host 生命周期。Tool/Driver 的
  system/plugin Host 都实现可选 `OutcomeHostAccess`，同时不改变旧 Host Protocol；
  Invocation 固定 Agent、ChatSpec、correlation、Invocation、generation 和 producer。
  system capability 自动注册，plugin 必须由 Host 显式热注册后才对新 Invocation 可见；
  已打开 Invocation 持有准入 lease，卸载不会中断旧 generation 的收尾。
- correlation-scoped Trajectory 已接入只读 Chat API：复用现有 Observation，追加完整
  Outcome supersession 历史，并以 content-free index 固定快照正序分页。Steer 与
  Interrupt 通过 target Invocation 对应的 Submission 恢复 correlation；legacy 缺失
  correlation 的事实不猜测归属。
- 本轮 Trajectory 相邻定点验证：35 项通过；未开放 HTTP 写入口，模型文本和未注册
  插件仍不得声明 Outcome。
- 真实运行服务 `localhost:8004` 已返回
  `qwenpaw.conversation-trajectory-page.v1`：固定 Chat 的 correlation
  `b19d5f7f-2045-4607-b36c-2ba70c45e41a` 得到 10 个正序节点，覆盖 Model、Action、
  Guardrail、HITL 和 Submission，全部归属一致且未虚构不存在的 Outcome。

### 2026-10-08 Lite Budget Lease

- Kernel 冻结 `BudgetAllocation`、`BudgetLeaseSnapshot`、状态枚举与 `BudgetLease`
  Protocol；它是运行授权，不替代 `ExecutionBudget` 或持久 Usage Ledger。
- `TaskExecutionCoordinator` 为每次 Run 打开根 Lease，Contextual Runner、Console
  模型与 Tool 继续通过原 `UsageMeter` 兼容面使用，但实际对象已具备派生和撤销语义。
- 子 Agent 的 opaque Usage Scope 现在派生独立 Lease；兄弟配额不能超额预留，释放
  会归还未用额度，根撤销级联后代，子级超额仍先写入权威 Ledger 再 exhausted。
- Lite 只承诺进程内 Lease identity；重启后以跨 attempt 累计 `UsageSnapshot` 重建根
  准入。跨主机 TTL、fencing 和分布式级联仍属于 Workstation / Hub。
- 本切片定点验证：32 项通过，覆盖 Lease 单元、Usage Scope 和完整 Runner 相邻路径；
  未执行全仓测试。

### 2026-10-08 Communication Contract

- Kernel 冻结 S1/S2/S3/S4 通信能力模型，以及 ordering、idempotency、cursor、
  retention、backpressure 和 disconnect policy；非法恢复能力组合在装配前失败。
- Chat Runtime Projection 的 GET 与 SSE snapshot 均返回同一 Contract。当前 SSE 只
  声明 snapshot-change reconnect，不声明 Event Log replay；durable Submission 才
  声明客户端断线后继续执行。
- Console TypeScript 已同步只读类型，但没有增加前端 Queue 或新的客户端状态机。
- 本切片定点验证覆盖领域约束、Projection、HTTP/SSE 契约和关闭 SSE 后 Invocation
  仍保持 running；未执行全仓测试或真实浏览器断线演练。

### 2026-10-08 Capability Lock Manifest

- Kernel 冻结 `CapabilityRelease`、`CapabilityLockManifest` 与 Store Port；Lock 只含
  selected descriptor 的身份、版本和 hash，不含实现、配置值或 Secret。
- Runtime Assembly 在 pinned generation 上校验 Selection 后、执行前保存 Lock；
  `InvocationScope` 和后续 `ContextManifest` 固定引用同一 ID/hash。system/plugin
  走同一编译路径，热替换不会改写旧 Invocation 证据。
- Lite Store 使用 deterministic lock identity、`0600` append-once 文件和冲突关闭；
  Chat 新增 ownership-checked `/capability-locks` 与 `/context-manifests` 只读查询。
- 固定 Chat `/clear` 后真实返回 `CAPABILITY_LOCK_E2E_OK`。API 证据显示 generation
  11 的 9 个 system release；Lock 与 Context 在 ID/hash、Invocation、generation
  四项全部一致，Context 含 72 个内容安全 fragment。
- 本切片相邻定点验证 71 项通过；尚未实现关键词发现索引、稳定 tag/promotion 和
  Workstation/Hub 远端 Release Registry。

### 2026-10-08 Stable Capability Release 与安全回滚

- Capability Registry 已为每个 system/plugin provider 建立内容寻址的 `stable`
  release tag，展开已晋升 Capability 的 descriptor hash，而非只暴露 generation。
- 热替换可使用 expected release hash fencing 做一次 provider 级回滚；回滚生成新的
  单调 generation、只恢复目标 provider，期间其他 provider 晋升不会被撤销，已固定
  旧 lease 也不会漂移。失败 staging 不改变 stable tag 或当前 generation。
- 回滚点包含 Python 实现对象，因此明确只在当前进程有效；重启继续从安装 manifest
  装配，不能反序列化代码对象。持久 Promotion/Evaluation Journal 与授权晋升门禁仍是
  后续基础设施切片，不应被当前 stable tag 冒充。
- 本切片定点验证 39 项通过，覆盖 Registry、system/plugin 同合同、Task Runtime
  相邻路径与 Lite 插件热激活；未执行全仓测试。
- OS/插件管理面新增 `GET /api/plugins/capability-releases`，只读返回当前 generation
  与按 provider 排序的 stable tags。启动期间优先使用 Plugin Loader Registry，未就绪
  时回退到同一 Workspace Registry，不扩展已暂停的 Task route。
- 真实 8004 服务 ready 后返回 generation 2、`qwenpaw.system.tasks` v1.7.0 的 13 个
  Capability release；响应不含 implementation、Secret 或 credential。该接口不提供
  远程回滚写操作，避免未经授权扩大管理面。

### 2026-10-08 Promotion/Evaluation Journal

- Kernel 已冻结 Candidate、Promotion Check、Evaluation、Event 及 Journal/Gate Port。
  Candidate 只持久化 bundle hash，Event 只引用内容安全 release/evidence identity，
  不保存实现对象、bundle payload、配置值或 Secret。
- Lite Journal 使用 `0600` append-once 文件。activate 与 provider rollback 共用
  prepared/committed WAL；prepare 失败不发布，commit 失败恢复旧 generation、stable
  tag 和 rollback fence，并尽力追加 aborted，因此可安全重试。
- 每次 Registry 启动生成 `registry_epoch_id`，Event 和管理 API 同时返回；跨重启历史中
  多个 generation 2 因 epoch 不同而不会混淆。旧 Event 无 epoch 时仍可读取为 null。
- `GET /api/plugins/capability-promotions` 提供 provider filter 与有界 limit，只读暴露
  WAL。真实 8004 返回相同 epoch 的 prepared/committed，以及跨三个 epoch 的同号
  generation 2 历史；candidate hash 稳定，未发现敏感字段泄漏。
- 当前 Gate 只把既有 schema、Slot implementation 与 health check 结构化为 Evaluation；
  高风险 Scenario Runner、Evidence Bundle、人工授权和 deactivate WAL 尚未完成。

### 2026-10-08 Registry Epoch 贯穿 Invocation 证据链

- Registry Snapshot 与 Lease 现在同时固定 `registry_epoch_id + generation`；Assembly
  将这对身份传入 `InvocationScope`，避免进程重启后同号 generation 被误认为同一
  次能力快照。
- Capability Lock、Context Manifest、Route Decision 与 Model Call Attempt 均记录
  相同 epoch。Lock 与新 Context hash 纳入 epoch；历史 Lock 没有 epoch 时继续按旧
  hash 校验，旧记录仍可读取为 null。
- 固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 真实提交返回
  `EPOCH_OK`。该 Invocation 的 Lock、Context、Route、Attempt 均为 epoch
  `ecb96e7a-c789-4f86-b4d7-b4e1a8505252`、generation 11；Lock hash 与 Context
  引用一致，Route 引用的 Context Manifest ID 也一致。
- 本切片 75 项定点测试与 AST、mypy、flake8、pylint 等文件级门禁通过；未执行
  全仓测试。真实历史投影还观察到一条推理式 assistant 文本，因此消息投影后续必须
  明确 reasoning、可见 assistant message 与 durable evidence 的边界，不能把内部
  过程文本作为“一问一答”消息直接暴露。

### 2026-10-08 Provider Deactivation WAL

- `deactivate_provider` 已使用 Kernel 预留的 `DEACTIVATE` Action 接入同一 Promotion
  Journal，不再绕过稳定发布证据。system/plugin 仍调用同一 Registry 方法。
- prepared 写成功后才发布移除 provider 的新 generation；committed 写失败会恢复旧
  snapshot、stable release 与 rollback fence，并尽力追加 aborted。prepare 失败时
  generation 和 provider 可用性均不变化。
- Deactivation Candidate 只包含 provider identity、previous release hash 与 capability
  IDs；Journal 不保存实现对象、配置内容或 Secret。不存在的 provider 保持幂等且不
  产生虚假事件。
- PluginLoader 在执行 hook、清理模块、工具和 Registry 状态之前先提交 capability
  deactivation；若 WAL prepare 失败，插件记录和 uninstall hook 均保持未触碰，避免
  “Capability 仍可解析但宿主实现已拆除”的撕裂状态。
- 当前 deactivation evaluation 只证明 provider 存在；按 Slot 风险执行 Scenario、
  Evidence 与人工授权仍是下一阶段门禁，不能把 contract check 冒充安全批准。
- 本切片 98 项 Registry、Promotion、Plugin lifecycle 与管理 API 相邻定点测试通过；
  AST、mypy、flake8、pylint 等文件级门禁通过，未执行全仓测试。

### 2026-10-08 Promotion Evidence Bundle

- Kernel 新增 `CapabilityPromotionEvidence`、Bundle、Assessment 与 Evidence Store
  Port；Assessment 强制 Evaluation、evaluator、Candidate、check、outcome 和 evidence
  reference 一致，未引用或跨 Candidate 的 Evidence 会在进入 Registry 前失败。
- 默认 Contract Gate 分别记录 schema、implementation 和 health 证明。Registry 在
  prepared WAL 前先持久化并回读 Bundle；Store 不可用时不发布 generation 或 stable
  release。activate、rollback 与 deactivate 都走同一证据前置门禁。
- Lite 使用 `0600` append-only Evidence Store；Bundle identity/hash 排除运行时时间，
  同一 candidate 重试或重新安装可安全复用，不会因 `created_at` 漂移冲突。
- `GET /api/plugins/capability-promotion-evidence?candidate_id=...` 提供内容安全回查。
  真实 8004 服务返回 system tasks Candidate 的 1 个 Bundle、3 个 checks 和 13 个
  capability identity；不含 implementation、配置或 Secret。
- 模型演进兼容了无 `evidence_bundle_id` 的历史 WAL，以及短暂版本曾把 null 纳入 hash
  的 6 条过渡 WAL；本地 86 条真实历史记录全部验证通过，Promotion API 从 500 恢复
  为 200。
- 本节记录生成时 Bundle 仍只证明 contract/schema/health；后续
  “Artifact Renderer Promotion Scenario”切片已补上首个真实行为场景。其他高风险
  Slot、Evidence Artifact 与人工授权仍未完成，不能宣称完整安全发布门禁。
- 本切片 106 项 Capability、Promotion、Plugin lifecycle、Workspace Registry 与管理
  API 相邻定点测试通过；AST、mypy、flake8、pylint 等文件级门禁通过，未执行全仓
  测试。

### 2026-10-08 Artifact Renderer Promotion Scenario

- `SlotContract` 新增机器可读 `promotion_risk` 与 `promotion_scenarios`；高风险
  engine、runner、tool/driver、memory、sensor、scheduler 和 delivery 已明确标注，
  但没有真实 Scenario 的 Slot 不生成虚假通过证据。
- Lite Scenario Runner 首先覆盖 `artifact.renderer`：对 staged system/plugin
  implementation 运行 `task.summary` Markdown inline/attachment fixture，验证 renderer
  identity、source hash、disposition、filename、64 KiB 输出预算、安全 inline media
  type 和 attachment byte preservation，单次 render 上限 5 秒。
- Scenario 失败会让 Contract Gate deny 并阻止 generation 发布；不支持 fixture 时
  记录 `not_applicable` 而不是 pass。若自定义 Gate 异常或遗漏 Scenario Evidence，
  Registry 同样失败关闭，并在拒绝 Bundle 中保留已完成的 Scenario 结果。
- 真实 `task-insights` 示例插件已经通过同一 PluginLoader + Registry 路径验证；运行中
  8004 服务的 system tasks renderer 也返回 PASSED Scenario Evidence。该门禁执行
  本地插件代码，当前不是安全沙箱；同步 `supports()` 的恶意阻塞、其他高风险 Slot、
  Evidence Artifact 与人工授权仍是后续范围。
- 长程交互方向保持不变：一问一答仅是短 Chat 快速路径和 UI 投影。Agent Loop 由
  durable Submission、Invocation、Interaction、Action、Artifact、Evidence、
  Verification 与 typed wait 持续驱动；只有缺失必要事实、实质偏好、范围授权或高影响
  决策才阻塞询问，禁止用“是否继续”模拟任务调度。
- 本切片 85 项 Promotion、Slot、Renderer、system contribution、真实 plugin hot
  activation、Workspace Registry 与管理 API 定点测试通过；文件级 AST、mypy、
  flake8、pylint 门禁通过。真实 8004 API 返回 generation 2，当前 system tasks
  Bundle 的 contract 三项与 renderer round-trip 均为 passed；未执行全仓测试。

### 2026-10-08 Tool Provider Promotion Scenario

- `tool.provider` 新增 `tool-provider.catalog` Scenario；Registry 将 staged descriptor
  与 implementation 一同交给 Runner，场景使用固定 Invocation、空配置、无 Credential、
  无 Interaction 和无真实 Workspace I/O 的 Host，只发现目录，不执行工具函数。
- 目录门禁限制 128 项和 64 KiB，拒绝非 Sequence、无效对象、重复/超长名称，以及
  `target_param` / `pattern_param` 不存在于 callable 参数的治理声明；目录发现受 5 秒
  timeout 保护。legacy callable 仍作为迁移兼容输入，不因此扩大 Plugin SDK。
- 对 `config_schema` 先验证空对象：schema 合法但需要 Agent Profile 配置时记录
  `not_applicable`，避免误杀配置后可用的插件；schema 本身非法记录 failed 并阻止
  generation 发布。Scenario Bundle 不保存配置、函数、Credential 或 Tool 参数。
- 内置 Workspace Tool Provider 与真实 `chat-tool-provider` 示例均通过相同门禁；重复
  工具名、错误 governance parameter 和非法 config schema 的负向用例均在发布前被
  拒绝。运行期仍由 Tool Guard、Policy、Approval 与 Action Plane 管理真正调用。
- 本切片 134 项 Promotion、Slot、Tool Provider、Assembly、Plugin SDK、system
  contribution、Workspace Registry 和管理 API 定点测试通过；文件级 AST、mypy、
  flake8、pylint 门禁通过，未执行全仓测试。

### 2026-10-08 Memory Provider Promotion Scenario

- `memory.provider` 新增 `memory-provider.session` Scenario。宿主直接打开 staged
  Provider，不新增由插件自证的 Probe 接口；固定 Invocation 使用 `ChatSpec.id`，仅因
  兼容模型仍同时填写同值 `session_id`。
- Promotion `MemoryHost` 只暴露空配置和进程内 revisioned State Store；Agent 与
  Conversation scope 相互隔离，不连接用户 SQLite、Credential、Interaction、真实
  Workspace 或 legacy memory backend。内置 Provider 通过私有兼容方法取得 `None`。
- 场景验证 Session Protocol、32 KiB prompt、共享 Tool catalog 合同，并以 5 秒预算
  约束 async open/close；验证失败后仍尝试关闭 Session。Evidence 仅保存 outcome 和
  capability identity，不保存 prompt、state key/value 或实现对象。
- 真实 `runtime-provider-kit.project-memory` 与内置 Workspace Memory Provider 走同一
  门禁；场景已验证临时 State Store 的 revision/write/delete，超长 prompt 在发布前
  被拒绝且 Session 正常关闭。
- 该边界是 Host capability isolation，不是 OS sandbox。恶意本地 Python 插件仍可绕过
  SDK 直接访问系统资源；sync `get_prompt()` / `list_tools()` 也还没有可强制终止的
  进程隔离。Driver、Runner、Delivery 的副作用方法不能在缺少 sandbox/Action Plane
  时直接试跑。
- 本切片 148 项 Promotion、Slot、Tool/Memory Provider、Memory State、Assembly、
  Plugin SDK、system contribution、Workspace Registry 与管理 API 定点测试通过；
  文件级 AST、mypy、flake8、pylint 门禁通过，未执行全仓测试。

### 2026-10-08 Driver Provider Promotion Scenario

- Driver Session 的纯目录验证从 Runtime 归位到 Kernel；Runtime 保留兼容导出，实际
  Toolkit 装配与 Promotion 使用同一 `validate_driver_session`，避免规则漂移。新增
  64 KiB tool catalog 序列化预算，补足原有 128 tools、16 fragments 和 32 KiB prompt
  限制。
- `driver.provider` 新增 `driver-provider.catalog` Scenario。Plugin Host 只提供空配置、
  无 Credential，并拒绝 open 阶段的 Approval；内置 Provider 使用独立私有 Host，
  `load()` 只返回空兼容目录，第三方看不到该方法。
- 场景打开 staged Session，验证 Session/provider identity、tool ownership、唯一
  capability/name、input schema、effect/risk/reversible 和 Prompt Fragment ownership，
  但绝不调用 `invoke()`。open/close 受 5 秒预算约束，验证失败后仍关闭 Session。
- 真实 `runtime-provider-kit.example-driver` 与内置 Workspace Driver Provider 走同一
  门禁；foreign tool ownership 和 open 阶段 Approval 均在 generation 发布前被拒绝。
  真正 Driver 调用仍必须经过 Tool Guard、Approval、Action Request/Result 与 Evidence。
- 本切片 165 项 Promotion、Kernel Driver、Driver 合同、Tool/Memory/Driver Provider、
  Assembly、Plugin SDK、system contribution、Workspace Registry 与管理 API 定点测试
  通过；文件级 AST、mypy、flake8、pylint 门禁通过，未执行全仓测试。

### 2026-10-08 Delivery Adapter Promotion Scenario

- `delivery.adapter` 新增 `delivery-adapter.routing` Scenario。Contribution 使用
  `metadata.delivery_addresses` 声明最多 8 个、每个不超过 200 bytes 的可验证地址；
  system channel/inbox 与示例 local-jsonl Adapter 已补齐声明。
- Promotion 只调用 deterministic `supports()`：每个正确 adapter/address 的 final text
  fixture 必须返回严格 `True`，foreign adapter identity 必须返回严格 `False`。场景绝不
  调用 `deliver()`，真实示例插件测试同时断言 JSONL 输出文件没有产生。
- 未声明 route hint 记录 `not_applicable`，不冒充 pass；空、重复、超界、错误类型或与
  实现不一致的 hints 均记录 failed 并阻止 generation。Evidence 不保存地址或 payload。
- 场景审查发现并修复了 system channel 的真实前置条件漂移：旧 `supports()` 不验证
  opaque address，可能先接受、到 `deliver()` 才解码失败；现在路由阶段即拒绝畸形
  address，Promotion 使用可真实解码但不含用户信息的固定 fixture。
- Scheduler 同期审查发现尚不能安全加行为场景：当前 Contribution 直接构造绑定真实
  路径的有状态 `SchedulerPort`，只读查询也可能创建 SQLite。后续先迁移到 Host-owned
  storage 的 `scheduler.provider`，不能用真实状态访问换取虚假验收。
- 本切片 178 项 Promotion、Delivery/Driver 合同、Channel、Tool/Memory/Driver Provider、
  system contribution、Plugin SDK、热激活、Workspace Registry 与管理 API 定点测试
  通过；文件级 AST、mypy、flake8、pylint 门禁通过。真实 8004 generation 2 的
  channel/inbox routing 与 renderer round-trip 均为 passed；未执行全仓测试。

### 2026-10-08 Scheduler Provider Migration

- 原 `scheduler` Contribution 已迁移为无状态 `scheduler.provider`。Kernel 与 Plugin
  SDK 暴露最小 `SchedulerHost`/`SchedulerProvider`；Provider 只能取得宿主注入的
  `SchedulerPort`，不再决定 SQLite 路径、读取存储环境变量或拥有用户数据生命周期。
  具体 `SQLiteSchedulerStore` 已从公共 SDK 移除，只保留在 Lite Host Adapter；旧
  `scheduler` Slot 仅保留 Dispatcher 与激活契约兼容，不再属于正式公共 Slot。
- 内置 local durable Scheduler 与示例插件均使用同一 Provider 合同。Cron Runtime
  在应用组合边界创建 `SQLiteSchedulerStore` 并通过 `SchedulerStoreHost` 注入，保持原
  durable path 和 Fire 幂等语义；Plugin 安装/热替换不触碰真实 Store。
- `scheduler-provider.catalog` Promotion Scenario 使用进程内只读 Store，查询固定合成
  Agent 的 definitions，验证 tuple、Agent ownership、唯一 schedule ID、128 项与
  64 KiB 上限。所有 mutation 方法都失败，场景不会创建、读取或修改用户 SQLite。
- system/plugin 行为合同继续验证 generation-pinned Fire、唯一 Task/Run、重放幂等与
  持久化重开；旧直连 Port 测试保留，用于约束迁移期兼容路径。
- 长程交互架构不采用 Handbook 式“一问一答”作为执行模型：Chat 问答只是短路径与
  UI 投影；Scheduler、Submission、Invocation、Interaction、Action、Artifact、
  Evidence 和 typed wait 共同驱动可恢复任务。只有缺失必要事实、授权或高影响决策时
  才进入 ask/approval，普通阶段推进不依赖用户逐轮回复。
- 本切片 83 项 Scheduler Promotion、Slot、system/plugin contract、Dispatcher、
  SQLite、Cron Runtime 与 Plugin SDK 定点测试通过；文件级 AST、mypy、flake8、
  pylint 门禁通过。真实 8004 最新 committed Candidate 发布
  `scheduler.provider`，其 `scheduler-provider.catalog` 为 passed；未执行全仓测试。

### 2026-10-08 Runner Preflight Scenario

- Kernel 与 Plugin SDK 新增 `RunnerPreflightRequest`、`RunnerPreflightResult` 和可选
  `PreflightTaskRunner`。请求固定 `external_io_allowed=False`；结果只描述 capability
  identity、Slot、candidate generation、Contextual 支持和 Cost Accounting，不承载
  Credential、Workspace、Task 内容或执行句柄。
- `runner` 与 `harness.runner` 共用 `runner.preflight` Promotion Scenario。Host 在 5 秒
  内调用预检并与 staged `TaskRunner` / `ContextualTaskRunner` /
  `CostAwareTaskRunner` 的真实属性交叉校验，绝不调用 `execute()`、
  `execute_context()` 或解析 Workspace。畸形、超时、身份或计费声明漂移均阻止发布。
- `LocalAgentRunner` 自动实现该合同；内置 Console/Codex/Qoder 与 task-insights、
  runtime-provider-kit 示例因此共用同一门禁。旧 Runner 缺少预检时明确记录
  `not_applicable`，不能冒充 passed，也不会因迁移期兼容被误杀。
- 预检属于 capability isolation，不是 OS sandbox。任意本地 Python 仍可能绕过 SDK；
  真正执行必须继续经过 Environment Resolution、Sandbox、Policy、Approval、Budget、
  Cancellation、Artifact/Evidence 和 generation lease。
- 长程任务方向保持事件驱动：Runner 在无逐轮用户应答时也可持续执行、Checkpoint、
  typed wait 和恢复；ask/approval 只用于缺失必要事实或授权，不成为任务推进时钟。
- 本切片 124 项 Promotion、Registry、Runner、Harness、system/plugin contract、热激活
  与 Plugin SDK 定点测试通过；文件级 AST、mypy、flake8、pylint 门禁通过。真实
  8004 最新 committed Candidate 的 Console、Codex、Qoder 三个 Runner preflight
  均为 passed；未执行全仓测试。

### 2026-10-08 Exact-release Plugin Deactivation Authorization

- capability-bearing Plugin/PawApp 的永久卸载改为两阶段条件请求。首次 DELETE 返回
  HTTP 428、当前 stable release hash 和 capability IDs；Console、PawApp client 与
  已完成 click.confirm 的 CLI 使用 `X-QwenPaw-Confirm-Release` 精确重试。
- release fence 在同一 per-plugin lifecycle lock 内校验。缺少或过期确认不会执行
  deactivation、uninstall hook、注销或删文件，因此并发升级后的新版本不会被旧确认
  误删。非 capability 插件和内部 force replacement 保持原兼容路径。
- Registry deactivation Evidence 新增 `deactivate.operator-authorized`；显式永久卸载
  记录 passed，内部兼容撤销记录 not_applicable，不伪造人工授权。
- 长程交互方向不变且已进入可执行契约：一问一答只保留为短 Chat 快速路径和 UI
  投影；持续 Agent Loop 由 correlation、Submission、Invocation、Action、Artifact、
  Evidence、Checkpoint、typed wait 和 continuation 驱动。Ask User 仅用于缺失必要
  事实、实质偏好、范围授权或高影响裁决，不能成为逐步骤推进器。

### 2026-10-08 Promotion Evidence Artifact Projection

- Promotion Evidence Store 仍是唯一权威事实源；没有新建 Artifact 数据库或复制 Bundle
  文件。Registry 从持久化 Bundle 确定性派生标准 `ArtifactRef`，因此 system 与 plugin
  发布证据使用同一条 lineage。
- Artifact 内容是去除 `created_at` 的 canonical JSON；内容 hash、size 与下载响应字节
  严格一致，同一 Candidate 重试不会因为时钟变化产生新 Artifact identity。
- Evidence API 现同时返回 Bundle 和 Artifact refs；按 bundle ID 的只读下载端点返回
  `nosniff`、ETag 与 attachment filename。公开 metadata 只有 candidate、bundle 和
  evaluator identity，不含实现、配置、原始日志、主机路径或 Secret。
- 本切片 62 项 Promotion domain、Registry、system API 与真实 plugin hot activation
  定点测试通过，文件级 AST、mypy、flake8、pylint 门禁通过。运行中 8004 的 system
  Candidate 已返回 Artifact ref；下载 7146 bytes 后复算 SHA-256 与 Ref/ETag 完全一致，
  canonical JSON 不含 `created_at`。未运行全仓测试，通用人工授权策略仍是后续模块。

### 2026-10-08 Exact-candidate Plugin Promotion Authorization

- capability-bearing Plugin 的显式安装和更新改为两阶段条件请求；首次请求返回 HTTP
  428、candidate ID/hash 和 capability IDs，Console 与 CLI 以
  `X-QwenPaw-Authorize-Candidate` 精确重试。非 capability 插件保持单次安装。
- Candidate 不只覆盖 manifest：源码树使用相对路径和文件字节做 64 MiB/5000 文件
  有界摘要，并拒绝符号链接。Loader 在 per-plugin lifecycle lock 内、副作用前校验，
  复制后、依赖安装和代码执行前再次验证，来源变化或旧确认失败关闭。
- 显式晋升 Evidence 增加 `promotion.operator-authorized=passed`；系统启动和内部恢复
  记录 `not_applicable`，避免要求无人值守启动时必须在线确认。
- 一问一答不再是 Runtime 边界：短 Chat 仍可投影成单轮消息，长程意图则由同一
  `correlation_id` 下的 Submission、Invocation、Action、typed wait、continuation、
  Artifact/Evidence/Verification 与 Outcome 持续推进。Task 页面继续后置。

### 2026-10-08 Chat Continuous Execution Activity Projection

- Console Chat 已增加只读 Activity 面板，直接复用
  `ConversationRuntimeProjection.execution_chains/activity`，按最新
  `correlation_id` 展示最多 5 条语义事件；折叠状态仅属于展示层，前端没有新增 Queue
  或执行状态机。
- 面板只展示 Action、HITL、控制、恢复、Artifact、Evidence、Verification 与 Outcome
  的标题、状态和内容安全来源引用，不渲染 facts、隐藏 reasoning 或 Provider payload。
  普通 model 路由/结果及 `qwenpaw.control.submission` 接收/终态被过滤，因此短 Chat
  仍保持一问一答的轻量外观。
- 一问一答只保留为短任务快速路径和 UI 投影，不能再作为长程 Runtime 生命周期。
  长程意图由同一 `ChatSpec.id + correlation_id` 下的 Submission、Invocation、Action、
  typed wait、continuation、Artifact/Evidence/Verification 与 Outcome 自主推进；只有
  Human Interaction Policy 允许的必要事实、实质偏好、范围授权或高影响裁决才阻塞
  等待用户。
- 新组件及 Runtime Store/Queue/Chat 页面 97 项定点测试通过；固定真实简单 Chat 在
  浏览器重载后的 Activity 元素计数为 0。历史“这是 Action”会话没有后端语义 Action
  observation，因此也不会仅凭标题或消息文案伪造活动。未运行全仓测试。

### 2026-10-08 Uncertain Action Reconciliation

- partial model stream 遇到 `unknown/partial` 或 `SideEffectStatus.UNCERTAIN`
  Action 时，恢复 worker 现在通过统一 Chat Approval Interaction 请求人工对账，不再
  永久停在 `action_reconciliation_required`，也没有复用 Task 页面私有状态。
- Approval 以稳定 Interaction ID 绑定 `continuation_id`、Invocation、correlation、
  Action 数量和内容安全 evidence digest。只有“已核实安全，重试一次”会写入
  `ModelStepRetryAuthorization`；“停止恢复”、过期或取消均持久取消 continuation。
- 授权后创建新的 Submission / Invocation，原 ActionRequest/ActionResult 和旧 Python
  stack 保持不变。入队前及执行 materialize 时都会重新验证 Interaction revision 与
  Action digest；来源 Invocation 之后出现新 Submission、证据变化、跨 Chat 或身份
  不一致均失败关闭。
- Interaction HTTP response 同时唤醒普通 continuation 与 resource/model-step recovery
  worker，不再依赖最长 60 秒轮询。54 项恢复、Action、dispatcher 与 Interaction API
  定点测试通过；新增用例覆盖审批落库后重建 service 实例的 retry/stop 两条路径，
  以及审批后新用户 Submission 取消旧恢复。
  AST、mypy、flake8、pylint 门禁通过。仓库固定 Black 23.3.0 在当前 Python 3.13
  pre-commit 环境因访问已移除的 `ast.Str` 失败；源码已用项目 Conda 环境按 79 列完成
  定点格式化，未运行全仓测试。

### 2026-10-08 Durable Action Retry Dispatch

- Action retry 的 durable outbox 已连接 Invocation Control：READY entry 经过
  Stop/Interrupt、新用户输入和 Queue revision fence 后，以内容安全 envelope 幂等创建
  Submission；重复调度不会创建第二个队列项。
- consumer 不恢复旧协程，也不调用模型。它重新加载私有 checkpoint 与源 Action，编译
  确定性 Invocation/ToolCall 身份，取得 Runtime lease，在 pinned generation 中解析
  exact tool，并重新经过当前 Permission、ToolCoordinator、action-scoped Sandbox 与
  ActionRecorder。Provider config 或证据漂移均失败关闭。
- Submission/Invocation/Action 是执行状态的权威来源；DISPATCHED outbox 只保存
  dispatch binding 作为审计与崩溃恢复依据，不建立竞争状态机。只有进程中断且没有
  terminal Action evidence 时，才按精确 dispatch ID requeue。
- 长程生命周期正式采用
  `ChatSpec.id -> correlation -> Submission -> Invocation -> Step/Action -> Outcome`。
  “一问一答”仅是单 Invocation 的快速完成与 UI 投影；网络、资源、审批和恢复通过
  typed wait/continuation 自主推进，只有缺失必要事实、授权或高影响裁决才等待用户。
- `submission_dispatcher` 21 项、Action execution/retry、Invocation dispatcher、
  Builder/Workspace 共 44 项定点测试通过，覆盖 repair、唯一 dispatch、内容安全
  envelope、重复调度幂等、Runtime lease、pinned runner、治理上下文、Sandbox 与
  Submission 终态。AST、mypy、flake8、pylint 定点门禁通过；固定 Black 23.3.0 在
  Python 3.13 上仍因访问已移除的 `ast.Str` 失败，且未改动文件。未运行全仓测试。

### 2026-10-08 Provider Resource Availability Release

- Kernel 新增 `ModelResourceRecoveryPort`，将 provider resource defer、partial-step
  continuation 与精确 availability release 冻结为 Edition 可替换边界，不再让 Runtime
  依赖 Lite SQLite 实现。
- 新 `ModelResourceWait` 持久化内容安全的 Provider/Model identity；SQLite 对旧库自动
  加 nullable 列，旧记录继续可读，但不会因缺少身份而被自动释放。
- 同 Workspace 任一真实成功 ModelCall 会释放相同 Provider/Model 的 waiting quota
  wait；现有 `/models/{provider}/models/test` 只有 live success 才向已加载 Workspace
  发布同一 signal。不同模型、timer wait、失败或非 live probe、重复 signal 均无效。
- release 将 wait 原子推进到 READY 并唤醒既有 dispatcher，因此长程执行无需用户再发
  “继续”。probe 不懒加载 Agent；Lite 尚无跨 Workspace/进程 health-event ledger，Hub
  后续应实现 durable Port Adapter，而不是扫描本地 SQLite。
- Resource wait、ModelCall、Runtime Observation、Provider router、Chat dispatcher
  与 Kernel dependency 共 101 项定点测试通过；AST、mypy、flake8、pylint 定点门禁
  通过。测试收集保留 1 条既有 Pydantic `TestProviderRequest` 命名 warning；未运行
  全仓测试。

### 2026-10-08 Goal Outcome 与 Chat 长程身份

- Goal Mode 不再优先使用 transport `session_id`：有 Chat 时以 `ChatSpec.id` 作为
  active goal identity，因此页面重连或 transport session 变化不会创建竞争目标；无
  Chat 的兼容 Channel 才使用旧 session fallback。
- Runtime 为当前 Invocation 绑定 `qwenpaw.system.goal-mode` Outcome producer。
  `update_goal(complete)` 通过统一 Host Broker 声明 `ACHIEVED`，`blocked` 声明
  `NOT_ACHIEVED`；Producer 不接触 Store，也不能伪造 Agent、Chat、correlation、
  Invocation 或 generation。
- Outcome declaration 失败时 Goal 保持 active，并向 Agent 返回持久化失败；不会因
  Tool 返回、Assistant final message 或 SSE 结束而投影业务完成。预算和迭代上限仍仅
  是技术终态，不自动写无证据的 `PARTIAL`。
- Goal lifecycle 与 Outcome Host 共 20 项定点测试通过；新增/变更 Python 文件的
  AST、mypy、Black、flake8、pylint 与凭据扫描均通过。未运行全仓测试。

### 2026-10-08 Durable Goal Execution Chain

- Kernel 新增版本化 `GoalExecution`、`GoalExecutionStore` 与
  `ConversationCorrelationResolver`；Plugin SDK 同步导出稳定类型，但当前未向任意
  插件开放共享内置 Goal 写权限。
- Lite `.qwenpaw/lite/goals.db` 以 Agent + `ChatSpec.id` 保存 objective、correlation、
  预算、进度、终态 intent 和 CAS revision。active Goal 不能被覆盖，同一 Goal 的身份、
  objective、预算和开始时间不能漂移；POSIX 数据库及 WAL/SHM owner-only。
- `AgentMode.on_turn_start()` 在 Stop Gate scope 评估前恢复 Goal snapshot，不恢复旧
  Python 协程。迭代/token 进度逐边界持久化；`/clear` 写 abandoned，预算或迭代上限写
  exhausted，均不伪造业务 Outcome。
- Goal 完成采用 `OUTCOME_PENDING -> Host declare -> COMPLETED/BLOCKED`。确定性
  outcome ID 与 exact lookup 关闭“Outcome 已提交、Goal 尚未 finalize”的崩溃窗口；
  新 Invocation 只能确认业务字段完全相同的结果，变化时失败关闭。
- Workspace 启动会主动扫描 pending Goal，以内部 Submission 创建恢复 Invocation；
  不调用模型、不生成对话消息，也不依赖用户再次输入。重复扫描与 orphan 重试保持幂等。
- 本切片 Goal、Submission Dispatcher、Outcome、Chat API 与 Mode lifecycle 共 64 项
  定点测试通过；相关 Python 文件 AST、mypy、Black、Flake8、Pylint 门禁通过。

### 2026-10-09 Agent Mode namespaced State Host

- Kernel/SDK 新增 `AgentModeState`；Host 为每个 Provider 绑定 Agent、`ChatSpec.id`、
  state key 与 pinned generation，插件不能伪造 owner 或取得 Store。
- `AgentModeHost.read_state/write_state` 使用 revision CAS；新 generation 可恢复旧状态并
  显式升级 schema，旧 Session 的陈旧写入失败。系统与插件 Host 使用同一 API。
- Lite 使用 owner-only SQLite WAL，单值限制 64 KiB；大内容继续走 Artifact，不进入
  Mode State。示例插件已改为通过 Host 持久化 lifecycle counter。
- `clear_state()` 以空值 revision 实现 reset，不物理删除记录；`/clear`、`/new` 后旧
  Invocation 无法利用 revision ABA 恢复已清理状态。示例 Mode reset 已切换到该路径。
- Mode State、Provider contract、Runtime lifecycle 与 `/clear`、`/new` 命令相邻路径
  共 124 项定点测试通过；未执行全仓测试。
- Mode Host、Runtime Assembly、SDK 示例与热替换相邻路径共 64 项定点测试通过；
  AST、mypy、Black、Flake8、Pylint 文件级门禁通过。
- Chat durable submission 在 active/pending Goal 中继承原 correlation，Goal terminal
  后恢复新意图分配；transport session 或 HTTP request 不再切断长程因果链。
- Goal、Outcome、Chat API、Stop Gate、Runtime lifecycle 与 Plugin SDK 共 88 项定点
  测试通过；
  包含真实 SQLite 重启、CAS 冲突、契约漂移、`/clear`、进度恢复、跨 Invocation
  Outcome 对账和 submission correlation 继承。未运行全仓测试。

### 本阶段此前已执行的定点验证

- 后端核心路径定点测试：70 项通过。
- 前端相关定点测试：74 项通过。
- PawApp 扩展测试组：72 项通过。
- 定点 TypeScript 编译（含 `hostExternals.ts`、`vite-env.d.ts` 与 JSX
  配置）：通过。

这些结果是分组定点验证，不等价于全仓测试通过。

### 2026-10-09 长程交互模型正式裁决

- 不采用一问一答作为长程 Agent Runtime 生命周期；Handbook 中请求/响应示例只视为
  传输或短 Chat 快速路径，不作为执行调度模型。
- 3.0 采用 intent-driven continuous execution：同一 `ChatSpec.id + correlation_id`
  下由 Submission、Invocation、Action、typed wait、durable continuation 和 Outcome
  持续推进。Assistant final、HTTP response 与 SSE 断线都不是完成事实。
- Ask User 只允许必要事实、实质偏好、范围授权和高影响裁决；Approval、Suggestion、
  Steer 与 Interrupt 保持独立类型，禁止用“是否继续”驱动每一步。
- 现有 `ConversationExecutionChain`、`UserInputReason`、Interaction outbox、Goal
  Execution Store 与 Outcome Broker 已构成可执行骨架。相关 Kernel、Projection、
  Interaction、Dispatcher、Outcome 与 Goal 定点验证为 `69 passed`。
- Task Workbench 继续后置；当前剩余工作聚焦 Model Recovery Contract、恢复终态和
  非插件来源的通用人工授权策略，不为本裁决新增第二套状态机。

### 2026-10-09 通用 Promotion 人工授权

- Promotion 授权已从 capability Plugin 私有 boolean 提升为 Kernel 稳定事实和
  provider-neutral Host Policy；system/plugin 在同一 Registry 强制点按 Slot Contract
  计算 low/medium/high 风险。
- 显式 low-risk 候选不增加交互；medium/high 必须确认精确 candidate hash。陈旧确认
  在 factory、依赖安装或插件代码执行前失败关闭，内部启动/恢复不伪造人工授权。
- 每次 Promotion Evidence Bundle 增加 `promotion.risk.<level>`；Plugin HTTP 428 继续
  作为 Adapter，响应同时返回 Host 计算的 risk。Plugin SDK 不导出授权器，避免插件
  自行降级风险或签发 grant。
- system 高风险、陈旧候选、low-risk UI 热安装、Promotion/Scenario/Router/CLI 共
  `185 passed`；文件级 mypy、flake8、pylint 已通过。Task 页面没有改动。

### 2026-10-09 Token Usage 多级归属

- 现有 token 文件原先在写盘前压缩为日期、Agent、Provider、Model，无法恢复
  Chat/turn 归属；新记录保留 `agent_id`、`ChatSpec.id` 和 Invocation turn，旧记录
  继续可读且归属为 null。
- `/api/token-usage` 与 `/details` 在日期、Provider、Model 之外支持 `agent_id`、
  `chat_id`、`turn_id` 过滤。同一事实因此可形成 global/agent/chat/turn 四级视图，
  前端无需从消息历史二次汇总。
- Provider 返回 token 标记为 `provider_reported`；没有实际 usage 时，Chat 的字符
  估算标记为 `local_estimate`。日期按 UTC、模型按实际 Provider route 记录。
- turn 当前稳定定义为 OS Invocation；工具循环的多次调用同属一 turn，retry/fallback
  可按实际 Provider/Model 分行。后续若 Kernel 显式冻结 Submission ID 到
  InvocationScope 的关系，可增加 submission 投影，但不以 `session_id` 代替。
- Console Token Usage 已将同一明细投影为全局摘要及 Agent、Chat、Turn、日期、模型
  五个切面。Chat/Turn 使用包含 Agent 的复合键；旧记录即使没有 Chat/Turn，也只在
  各自 Agent 内显示为“未归属”，不会跨 Agent 合并或伪造历史归属。
- `/api/token-usage` 现已正式返回 `by_date_model`、`by_agent`、`by_chat` 和
  `by_turn`，不再只把这些维度留给前端临时计算。Console 直接消费权威 Summary；
  旧 `useDataAggregation` 已删除，`/details` 继续保留给明细查询与兼容调用。
- Chat 的每个完成 ResponseCard 直接显示该 Turn 的总 Token、输入/输出及实际
  Provider/Model；`local_estimate` 明确显示为约数，旧回合无 usage 时不伪造空统计。
  数据来自卡片持久 metadata，刷新及历史恢复后仍可见，不依赖全局最新 Turn Store。
- 本切片后端 token/turn 定点测试 `96 passed`，前端 Chat/API 定点测试
  `60 passed`；统计页新增聚合与页面定点测试 `19 passed`，Turn 卡片及 usage
  定点测试 `51 passed`。真实统计页已验证五个切面；真实 Chat Turn 显示
  `34.6K tok` 及实际模型，刷新后保持一致。Task 页面没有改动。
- 权威 Summary 收口新增后端 `51 passed`、Console 页面/API `16 passed`；Python
  文件级 pre-commit 与前端 ESLint/Prettier 通过。Task 页面仍未改动。

### 2026-10-09 Token Usage 事实源收敛

- 审计发现实现仍有两份事实：Model Call Result 与可丢弃队列写入的 token JSON；
  进程崩溃、队列满或 Result 落盘失败均可能使两者分叉。此前文档把 JSON Summary
  描述为“同一模型调用事实”并不准确，现已修正架构与迁移 Checklist。
- `ModelCallAttempt` 新增可向后兼容的 `agent_id`，新调用由 `InvocationScope` 写入；
  Chat 使用 `conversation_id=ChatSpec.id`，turn 使用 `invocation_id`，实际
  Provider/Model 继续由 Attempt 记录。
- `ModelCallResult` 现同时保存 `provider_reported` measurement、input/output、
  cache read/write/eligible/observed 与 cost。缓存语义未验证时所有 cache counter
  强制为零；历史 Result 缺少新字段时保持可读。
- `TokenRecordingModelWrapper` 只在 Provider 边界归一化一次 usage；先给执行预算记账，
  再持久化不可变 Result，成功后才更新旧 JSON 和 Chat turn projection。Result
  持久化失败不会再产生 phantom token row。
- 新设计文档为 `docs/design/qwenpaw-usage-accounting.md`。下一切片必须实现
  attempt-id 幂等、可重建 projection index、legacy UTC cutover watermark 和 shadow
  reconciliation，之后才能把 `/api/token-usage` 从 JSON 切换到 Model Call facts。
  禁止把旧日期聚合与新 Attempt 事实直接相加。
- 本切片 Model Call/Token Usage 定点测试 `84 passed`，Observation、Recovery 与 Chat
  API 上下游定点测试 `50 passed`。Task 页面没有改动。

### 2026-10-09 Token Usage 可重建投影与安全切换

- 新增 Lite `token_usage_projection.sqlite3`，只保存 Model Call 的内容无关 usage
  派生字段；`attempt_id` 是唯一键，同一事实重复投影保持幂等，不同事实冲突失败关闭。
  SQLite 可整库删除，不取代 Workspace 中的 `attempt.json/result.json` 权威事实。
- 应用启动读取全部已配置 Agent 的 Workspace，通过 `ModelCallStore.scan_all()` 收集
  事实并原子 rebuild；任一 Workspace 扫描失败时保留旧索引，不用部分数据覆盖。
- 首次初始化把下一 UTC 日写成不可变 cutover。水位前 Model Call 同时写事实投影和
  legacy JSON，作为结构 shadow；水位后有 Attempt 的调用停止写 JSON。查询会过滤
  水位后所有带 Chat/Turn 归属的 JSON row，再合并 SQLite 投影，避免重启、重试或旧
  进程残留造成双算；没有 Attempt 的兼容调用仍由 JSON 提供。
- 单次投影失败只记录日志，不写无法按 Attempt 去重的 JSON fallback；不可变 Result
  已经落盘，下一次启动 rebuild 会补回。该取舍允许短暂统计延迟，但不允许永久双算。
- `/api/token-usage/projection` 返回 cutover date、indexed attempts、最后 rebuild 时间
  和数量，不包含消息、Prompt、reasoning 或 Secret。Summary/details 的既有响应保持
  兼容，Console 无需修改。
- 投影、Model Call、Token Usage、Retry/Fallback、Task budget、Observation 定点测试
  `234 passed`；应用启动与 Token Usage API 定点测试 `19 passed`。Python 文件级
  mypy、Black、Flake8、Pylint 通过。Task 页面没有改动。

### 2026-10-09 Context Window 多级统计

- `ModelCallAttempt` 新增向后兼容的 `context_window_tokens` 与
  `compaction_threshold`；Wrapper 从实际 Adapter/Model 在网络请求前固化，不读取
  前端配置猜测。Observation 同步公开这两个内容无关事实。
- Context 有效输入在缓存语义已验证时使用 `cache_eligible_input_tokens`，否则使用
  Provider `input_tokens`。统一 Usage Projection 为 global、Agent、`ChatSpec.id`、
  Invocation turn、UTC 日期与实际 Provider/Model 输出加权占用、单次峰值、可观测
  调用数和达到当次压缩阈值的调用数。
- 旧 `token_usage_projection.sqlite3` 自动增加 nullable columns；历史 Attempt 缺少
  window 时保持不可观测。cutover 前查询把事实投影的 Context 字段覆盖到完全相同的
  legacy scope row，Token/Calls 仍只取 JSON，所以新指标立即可见且不重复计数。
- Console Token Usage 的总览卡片和五类表格已显示加权占用、峰值与临近压缩调用；
  中英日俄葡越印尼七种 locale 已补齐。Chat 当前上下文继续显示 `local_estimate`，不
  混入跨范围 Provider 统计。Task 页面没有改动。
- 定点验证：领域模型、Model Call、Observation、Projection、Usage Manager 与
  Agent 聚合 `124 passed`；启动及 Token Usage/Console Metadata API `19 passed`；
  Console Token Usage 与 i18n `22 passed`。

### 2026-10-09 Model Transport Capability

- Kernel 新增 Provider-neutral `ModelTransportContract`、Host-keyed HMAC Resume Evidence 与
  fail-closed validator。cursor resume 必须验证 response identity 与 prefix；sticky
  route 及 WebSocket → HTTP continuation 均需 Provider 显式声明，不能由 Kernel 猜测。
- `Provider.get_model_transport_contract(model_id)` 在模型装配时解析，实际 Contract 在
  网络请求前固化到 `ModelCallAttempt`。旧 Provider/旧 Attempt 均使用
  HTTP/non-resumable 默认值，保持向后兼容。
- 任一 response identity、prefix 哈希/长度、route 不一致或证据缺失都回退
  `durable_context_rebuild`。新部分流失败把恢复模式和内容安全原因写入
  `ModelCallResult`/Observation；成功、pre-output retry 与用户 Interrupt 不生成伪
  transport decision。
- 当前 Lite 没有宣称任何内置 Provider 支持原地续传；既有 durable model-step
  checkpoint/outbox 仍是权威恢复路径。首个真实 cursor-resume Adapter 与断流故障注入
  保持未完成。详细合同见 `docs/design/qwenpaw-model-transport-contract.md`。
- 定点验证：Kernel/Model Call/Observation/Usage `130 passed`；Provider
  Transport/Retry/Fallback/Factory `157 passed`；Chat API、Provider startup 与 Console Metadata
  `25 passed`。Python 文件级 mypy、Black、Flake8、Pylint 全部通过。

### 2026-10-09 Model Recovery Kernel Contract

- `ModelRecoveryDecision` 已从 Provider 私有 dataclass 提升为 Kernel 不可变领域模型；
  Provider 分类器、Runtime 和持久 `ModelCallResult` 不再各自解释恢复组合。
- Kernel 冻结全部 failure/disposition 允许矩阵。连接失败和 Provider 不可用只能进入
  transport retry，部分流只能 continue/reconcile，限流和 quota 只能等待资源，
  auth/policy/budget/invalid/context/unknown 只能终止，用户 Interrupt 只能停止。
- `Retry-After` 仍只允许绑定 rate-limited resource wait；旧 ModelCall 记录没有恢复
  字段时继续兼容读取，新记录声明恢复事实时必须通过 Kernel 验证。
- Model Error Policy、Model Call、Resource Wait、Runtime Save 与 Observation 相关
  `111 passed`；Task 页面没有改动。

### 2026-10-09 Workspace Driver Catalog Budget

- Kernel 的 128-tool / 64 KiB Driver Catalog 上限保持不变，并导出统一字节计量函数，
  校验与兼容层不再复制预算口径。
- Workspace 兼容 Driver 仅在目录超限时压缩 MCP 展示注解：工具说明保留首个非空
  摘要行，JSON Schema 只移除无执行语义的 `title`；参数 description、type、required、
  enum、default 及约束均保留。压缩后仍超限继续失败关闭。
- 真实启用的 40-tool MCP 目录原模型可见负载约 84.8 KiB，曾使普通 Chat 在模型调用前
  失败；修复后原 Chat 成功返回 `CATALOG_COMPACTION_OK`，并写入带 Agent、ChatSpec.id、
  Invocation turn、日期和实际 Provider/Model 的 Token 明细。
- Driver Adapter 与 Runtime 定点测试 `44 passed`，改动文件 pre-commit 全部通过；
  Task 页面没有改动。后续 Tool Search / 按需加载应作为独立能力，不以放宽目录上限
  代替当前安全边界。

### 2026-10-09 Durable Execution Deadline

- `ExecutionBudget.max_duration_seconds` 不再在每次 resume 时重新获得完整预算。首次
  Run 固化 `execution_deadline_at`，后续 attempt 继承同一截止点；历史 Run 没有该
  字段时，以首次 `started_at` 兼容补算，不需要数据迁移。
- Coordinator 在每次进入执行边界时只读取一次 wall clock，把 deadline 剩余时间与
  attempt/host timeout 取最严格值；实际活动计时交给 `asyncio.timeout` 的事件循环
  monotonic clock。deadline 已过时会先失败化 Run，不调用 Runner。
- service/runner/replay/runtime 共 53 项定点测试通过，覆盖跨 service 实例恢复沿用
  deadline 和过期后不启动 Runner；未运行全仓测试。macOS、Linux、Windows 的
  suspend/sleep、系统时间跳变与进程重启 E2E 仍未验证，迁移计划保留独立未完成项。

### 2026-10-09 Artifact Renderer Behavioral Contract

- 新增 system/plugin 共用的 Artifact Renderer 行为合同，不再用 Slot 激活或
  Protocol `isinstance` 代替运行语义。系统安全 Renderer 与真实 `task-insights`
  插件 Renderer 均通过同一个 `ArtifactRenderService` 执行。
- 合同覆盖 capability identity、generation pin、预览选择、来源 hash、文件名、
  disposition、安全 MIME、输出大小，以及插件不支持附件时回退系统 Renderer 并保持
  原始字节。没有新建插件私有渲染管线，也没有修改 Task 页面。
- 合同、Renderer 单元、系统 Contribution 与真实 Task Artifact API 共 20 项定点测试
  通过；文件级 AST、mypy、Black、flake8、pylint 门禁通过，未运行全仓测试。

### 2026-10-09 Task Runner Behavioral Contract

- `runner` Slot 现在有独立的 system/plugin 行为合同；不再用已经通过的
  `harness.runner` 合同代替。系统 Console Runner 与真实 `task-insights` SDK Runner
  都经过同一 side-effect-free preflight 和 `TaskExecutionCoordinator`。
- 两条路径均验证固定 generation、Run 成功、Task 完成、Ledger 终态，以及
  Artifact/Evidence 的真实持久化和 producer identity。首次测试还发现
  `health_check()` 只是系统实现附加能力，不属于公开 `TaskRunner` Port；合同已改为
  强制双方实际共有的 `PreflightTaskRunner`，没有误扩张插件 API。
- Runner、Planner/Strategy、Harness 与系统 Contribution 相关 35 项定点测试通过；
  文件级 AST、mypy、Black、flake8、pylint 门禁通过，未运行全仓测试，也没有修改
  Task 页面。

### 2026-10-09 QwenPaw Legacy Queue Isolation

- 修复稳定 QwenPaw Chat 偶发闪现旧 Queue 的根因：服务端 Queue 已成为权威后，
  `ChatSenderTabsPanel` 仍订阅并展示 localStorage Queue，挂载/卸载 effect 也可能继续
  启动 background sender，遗留项存在重复提交风险。
- 隔离现在以 backend 类型为边界，而不是等待 `backendChatId` 映射完成。QwenPaw 从
  首轮 Chat 分配开始不展示、不调度、不后台排空 legacy Queue；遗留项只有解析到已知
  外部 backend Agent 才可执行，QwenPaw 与未知 Agent 均 fail closed。后台工具面板
  仍可独立显示，外部 backend compatibility FIFO 保持可用。
- 旧 Chat SDK 集成测试已按事实重分类：服务端 admission 断言属于 QwenPaw，本地
  FIFO 断言只属于 external backend；修复了全模块 mock 隐藏真实 admission 函数的
  测试旁路。Chat admission、组件、lifecycle 共 124 项定点测试通过；改动文件
  Prettier 检查和新增模块 ESLint 通过，Task 页面未改。全量 TypeScript `--noEmit`
  仍被仓库既有错误阻断（旧 target lib、测试 fixture 缺字段和 Task/UI 旧类型），
  本次改动文件没有出现在错误列表中，不能据此宣称全量类型门禁通过。

### 2026-10-09 Capability Conformance Matrix

- 新增 Host-owned `CAPABILITY_CONFORMANCE`，与全部 Slot 精确一一对应。公开 Slot
  必须绑定系统实现、插件 fixture 和 OS 行为测试；新增 Slot 漏证据或用 activation
  冒充行为合同会 fail closed。
- 证据被分为 `behavior_contract`、`system_lifecycle`、`activation_contract` 和
  `migration_boundary`。16 个 public Slot 已完成行为级系统/插件同契约；
  `agent.factory` 保持 Host-only；4 个旧 Slot 只用于迁移；5 个 UI Slot 只证明热
  激活和投影，未宣称 Task UI 行为完成。
- 新增只读 `GET /api/plugins/capability-conformance`。返回值显式声明
  `declared_evidence_not_runtime_health`，不能代替具体候选的 promotion evidence 或
  当前进程健康检查。矩阵相关 46 项单元测试及其引用的 27 项 OS 行为合同通过；
  未运行全仓测试，Task 页面没有改动。

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
