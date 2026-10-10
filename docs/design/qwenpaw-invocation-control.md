# QwenPaw Invocation Control 设计

- 状态：实施中
- 产品基线：OS / Chat-first；Lite 是首个产品装配
- 范围：服务端 Queue、Steer、Interrupt、统一运行时交互基建
- 非目标：Task 页面、浏览器本地执行队列、远程 Hub Scheduler

## 1. 设计原则

1. Queue、当前 Invocation 和控制命令都以服务端持久化状态为事实源。
2. 前端只提交命令、持有未提交草稿并订阅投影，不拥有运行权。
3. Steer 只改变尚未执行的未来动作，不撤销已提交的事实。
4. Interrupt 通过取消树停止当前执行；它不能伪装成 Steer。
5. 每个 Conversation 最多一个 active Invocation；并行工具属于同一个工具批次。
6. 所有控制命令都有 command identity、幂等键、revision 和终态回执；普通
   Submission append 由服务端事务分配 revision，不要求客户端先抢占版本。
7. Approval、Ask User 和 Suggestion 共用交互投递基建，但不丢失各自
   的政策语义。

## 2. 统一运行时交互

Runtime 的双向交互分为两个平面，两者共用 invocation identity、
correlation identity、revision、幂等、超时、取消、审计和订阅：

- **Control Plane**：用户主动改变正在执行的 Runtime，如 Steer、
  Interrupt、Cancel Queue。
- **Interaction Plane**：Runtime 向用户发起交互，如 Approval、
  Ask User 和 Suggestion。

`InteractionRequest` 是 channel-neutral 投递契约。Approval 原有的
`ApprovalRequest/Decision` 仍是政策事实源，由 `source_id` 关联交互记录；
不把安全决策降级成普通文本回复。Ask User 使用同一请求/回复/终态回执
模型。Suggestion 强制为非阻塞，不能暂停 Invocation。

| 交互类型 | 是否阻塞 | 恢复点 | 终态要求 |
|---|---:|---|---|
| Approval | 是 | 工具批次 admission 之前 | 必须映射到 ApprovalDecision |
| Ask User | 是 | 下一次 reasoning 之前 | 回复写入 Conversation |
| Suggestion | 否 | 无 | 已投递/过期/取消 |

Interaction 超时、Runtime 中断或 Invocation 终止时，服务端统一将 blocking
等待者置为 `expired/cancelled` 并解锁；前端不得以隐藏卡片代替状态迁移。已经
投递的 non-blocking Suggestion 不随 Invocation 终态撤回，只能由用户响应、显式
过期或管理操作关闭。

## 3. Steer 生命周期

Steer 首先以 `accepted` 持久化。只有指令已经写入 Agent 可见上下文，或者已经
整体撤销尚未开始的工具批次并写入上下文后，才能提交 `applied`。回执必须记录
实际使用的 `SteerSafePoint`。仅仅把指令放入内存 Queue 不算应用成功。

### 3.1 安全点矩阵

| 运行窗口 | 可否应用 | 服务端动作 | 及时性边界 |
|---|---|---|---|
| `BEFORE_REASONING` | 是 | 在发起模型请求前追加用户 Steer 消息 | 下一次模型调用立即可见 |
| reasoning 进行中 | 条件允许 | 请求协作终止模型流；封存已输出片段，再转到 `AFTER_REASONING` | Provider 可取消时即时，否则保持 accepted |
| `AFTER_REASONING` | 是 | 最终文本尚未 commit 时阻止结束并追加 Steer | 不再等待下一轮用户消息 |
| `BEFORE_TOOL_BATCH` | 是 | 工具尚未 admission 时整体撤销该批次，为每个调用写入 superseded result，再追加 Steer | 不允许只跳过并行批次中的部分工具 |
| 工具批次运行中 | 否 | 保持 accepted；不因 Steer 杀死正在发生的副作用 | 等待整个批次进入确定或 uncertain 状态 |
| `AFTER_TOOL_BATCH` | 是 | 所有 Tool Result 与 Side Effect 状态提交后追加 Steer | 下一次 reasoning 前立即可见 |
| 最终响应已 commit | 否 | 返回 `conflict: invocation_terminal`；调用方可选择重新 enqueue | 不篡改已完成消息 |

### 3.2 reasoning 中断约束

- 只取消当前模型流，不取消整个 Invocation。
- 已经发送给客户端的增量保留，并以 interrupted 标记封存，禁止静默删除。
- Provider 在有界时间内不响应协作取消时，Steer 继续保持 `accepted`；Runtime 在
  模型自然返回后从 `AFTER_REASONING` 应用。
- Steer 指令本身作为用户可见消息写入 Conversation，不能藏在系统 Prompt 或内存
  标志中。

### 3.3 工具调用约束

- 判断边界必须位于工具批次 admission 之前，而不是每个 `_execute_tool_call()`
  独立判断。否则并行批次会产生部分执行竞态。
- 工具已经获得 Side Effect reservation、进入审批或开始执行后，Steer 不做强制
  取消。若用户要求立即停止，必须提交独立 `interrupt_current`。
- 等待审批但工具尚未执行时，可以原子取消该批次及 pending approval，再应用
  Steer；取消原因和 command ID 必须进入审计事件。
- 工具返回后，必须先提交 Tool Result、Side Effect terminal/uncertain 状态和使用
  量，再到 `AFTER_TOOL_BATCH` 应用 Steer。
- `AFTER_TOOL_BATCH` 的注入路径不得再次查询或关闭 unfinished Tool Call；即使底层
  状态清理晚于 Tool Result 提交，也必须把已完成结果视为不可变事实。
- AgentScope 将 `INTERRUPTED` Tool Result 解释为整个 Invocation 终止；
  未 admission 的旧调用因此以 `DENIED + superseded` 关闭，保证 Steer
  后能继续 reasoning。这与用户显式 Interrupt 的终止语义严格分离。

### 3.4 AgentScope 接入层

- `RuntimeInteractionMiddleware.on_reasoning` 处理 reasoning 前/后的 Steer，
  并作为后续 Ask User、Suggestion 和交互恢复的统一扩展面。
- `on_acting` 只包围单个工具且位于权限校验之后，不能实现并发
  批次原子 admission。因此工具安全点由 `_execute_*_tool_calls`
  极薄 bridge 处理，其他交互逻辑不进入 Agent 主类。

## 4. Interrupt 生命周期

`interrupt_current` 的顺序固定如下：

1. 持久化 command=`accepted`。
2. Submission 从 `running` 进入 `interrupting`。
3. 先原子撤销 blocking Approval/Ask User，再向 Tool Coordinator、模型流、Harness
   和 subagent 传播同一个取消原因。
4. 等待有界排空，保存部分 Assistant 输出并结束未完成 Tool Call。
5. Submission 进入 `interrupted`；命令回执进入 `applied`。

如果 Runtime generation 已丢失，Dispatcher 启动恢复会先原子关闭其遗留 active
Submission，再恢复 queued 调度：`admitted` 直接进入 `interrupted`，`running` 经过
`interrupting -> interrupted`，已处于 `interrupting` 的记录完成为 `interrupted`。
遗留 Interrupt / Stop and Clear 回执结算为 `applied`，无法再注入的 Steer 结算为
`rejected`。恢复器不会自动重放运行中的模型或工具，避免重复外部副作用；需要从
Checkpoint 继续时，由上层创建新的 attempt，并先检查 Side Effect 状态。

上述启动恢复只适用于 Lite 的单 workspace-owner 进程模型。Workstation/Hub 多实例
实现必须在同一 Port 下增加带 TTL 的 owner lease/heartbeat，只有 lease 过期或完成
generation handoff 后才能判定 orphan，不能由任意新实例直接终止仍存活的 Runtime。

## 5. Queue 与竞态规则

- `sequence` 由服务端事务分配且不可修改。
- `queue_position` 是可变调度位置；重排只修改它，不重写用于审计和幂等回放的
  `sequence`。投影按 active 优先、随后 `queue_position` 返回。
- `priority` 只影响 admission 选择；不能为同一 Conversation 启动第二个消费者。
- Steer、Interrupt、Cancel、Reorder、Stop and Clear 等会改变既有状态的控制操作
  使用 Queue revision；过期 revision 返回冲突和最新 revision。
- 普通 Submission append 是可交换的追加操作：客户端默认不发送
  `expected_revision`，服务端在 `BEGIN IMMEDIATE` 事务内分配 `sequence`、
  `queue_position` 与新 revision。这样同一 Chat 的多个标签页不会因为读到同一旧
  revision 而互相覆盖或无意义重试；显式携带 revision 的兼容客户端仍可请求 CAS。
- 相同幂等键及相同业务载荷返回原 receipt，即使服务端生成的 command、correlation
  或时间身份不同。
- 页面关闭、跨标签切换和 SSE 断线不改变 Queue 或 Invocation 所有权。
- `cancel_queued` 只把一个尚未 admission 的 Submission 原子迁移到
  `cancelled`；`reorder` 必须携带当前全部 queued Submission，避免遗漏或重复。
- `stop_and_clear` 在同一 SQLite 事务中取消所有 queued Submission，并捕获当时的
  active `invocation_id`。后续 cancellation tree 只允许命中该 Invocation，禁止
  因 Runtime 晚绑定而误伤之后启动的新一轮；无 active Invocation 时命令直接
  `applied`。

## 6. 实现切片

1. 已完成：Kernel 模型、状态机、`InvocationControlPort`。
2. 已完成：Lite SQLite sequence/revision/idempotency、admission、状态转换和回执。
3. 已完成：Runtime Steering Mailbox 和统一 Interaction Kernel 契约。
4. 已完成：AgentScope reasoning middleware、批次级 tool bridge 和
   Agent 可审计上下文注入。
5. 已完成：持久化 Steer dispatcher、Runtime 晚绑定恢复、
   `ChatSpec.id` 唯一 Conversation identity 与 Chat Submission 运行终态；
   控制面领域模型不携带 `session_id`。
6. 已完成：workspace 级持久化 `InteractionService`，统一并行 open、
   revision、响应幂等、超时恢复和 Invocation 级原子取消；Runtime 所有终态
   均会取消该 Invocation 未决 Interaction 并释放等待者。
7. 已完成：Tool Guard 与 Driver Gate 的旧 pending Approval 通过兼容桥先持久化
   为 `InteractionRequest`；任一旧审批入口或统一 Interaction 入口决策后，都会
   同步关闭另一侧并释放同一 Runtime waiter。桥接元数据递归脱敏，运行身份只使用
   `ChatSpec.id + invocation_id`；原有渠道和 Task 审批投影暂时保留。
8. 已完成：invocation-bound `RuntimeInteractionBroker` 为内置模块与插件提供
   同一 `ask_user()` / `suggest()` API；前者先持久化再等待，后者强制非阻塞，
   Runtime 终止会按 `invocation_id` 解除等待。插件通过公开
   `ToolHost.interaction_broker()` 获取能力，不依赖私有 request-context 键。
   `ask_user()` / `defer_user_input()` 必须声明 Kernel `UserInputReason`；统一
   admission gate 在持久化前拒绝未分类请求，历史 JSON 仍保持可读。
9. 已完成：内置 `ask_user` 工具通过系统 Tool Provider 与插件共用 Broker；
   Chat Interaction HTTP Adapter 按 `ChatSpec.id` 查询和响应，并在服务端校验
   agent/conversation ownership、revision 与幂等。Chat 页面只轮询权威 open
   projection 并提交决策，不持有本地交互状态机；旧 Approval 卡片按同一
   interaction identity 去重。真实浏览器已验证选项提交后 Runtime 恢复执行。
10. 已完成：内置 `suggest_user_action` 与 `ask_user` 通过同一个系统 Tool Provider
    和 invocation-bound Broker 发布；Suggestion 持久化后立即返回，不暂停执行，
    且 Runtime 终态只取消 blocking Interaction。Chat 权威投影支持展示和关闭建议。
11. 已完成：Runtime invocation 级 Interrupt binding 与旧 `/stop` 兼容桥。
    `interrupt_current` 先持久化，再将 Submission 迁移到 `interrupting`，按
    blocking Interaction → Tool Coordinator 前台子任务 → Runtime owner 的顺序
    取消；Interaction 按 Invocation 在单一事务中撤销并先释放 Approval/Ask User
    waiter，终态清理再幂等确认。dispatcher 等待 Runtime 保存部分输出并提交
    `interrupted` 后，才把命令回执写为 `applied`。Console 和 Channel `/stop`
    均按 `ChatSpec.id` 优先走该
    控制面，只有 live binding 不可达时才回退 `TaskTracker/session_id`；两条路径的
    `interrupt_current` 都不再清空 Queue。
12. 已完成：服务端 `cancel_queued`、`reorder` 与 `stop_and_clear` 统一服务 API。
    SQLite 将取消/重排与命令回执放在同一事务；`sequence` 和
    `queue_position` 已分离，`stop_and_clear` 通过捕获的 Invocation 复用同一取消树。
13. 已完成：Codex、Qoder Harness 请求在携带 OS identity 时进入与内置 Runtime
    相同的 durable Submission 和 Interrupt 生命周期；适配器公开统一
    `cancel_turn()`，并把取消传播到精确的 Codex turn 或 Qoder session。Harness
    终态同样释放 blocking Interaction，但保留已投递的 Suggestion。缺少 OS identity
    的旧调用仍走兼容路径，不伪造持久化运行事实。
14. 已完成：Chat Control HTTP Adapter 按 `ChatSpec.id` 暴露 Queue 投影及
    Steer、Interrupt、Cancel Queued、Reorder、Stop and Clear。控制操作携带
    `idempotency_key + expected_revision`；普通 Submission append 只要求
    `idempotency_key`，由服务端事务排序。两类写入都返回领域 `ControlReceipt`；
    路由不维护浏览器执行状态，也不接受 `session_id`。
15. 已完成：`ConversationRuntimeProjection` 将 Queue 与 open Interaction 聚合为
    一个 ownership-checked 当前状态；`/runtime/stream` 使用稳定快照 cursor 支持
    `Last-Event-ID` 和查询 cursor 重连，空闲时发送 keepalive。Queue 或 Interaction
    任一变化都会产生新快照，相同状态不会因观察时间变化产生假更新。
    Runtime Projection 同时返回 Kernel `CommunicationContract`：SSE 被准确声明为
    S2 `request_stream + snapshot_change + latest_state + coalesce_latest`，重连只取
    当前权威快照，不冒充 Event Log replay；Submission 被声明为 S3
    `durable_handle + server_queue + continue`，关闭 SSE 不取消 Invocation。
16. 已完成：workspace-owned Submission Dispatcher、版本化
    `SubmissionInputEnvelope` 和 `POST /chats/{chat_id}/submissions`。Dispatcher 从
    durable Queue 恢复扫描，每个 Conversation 串行、不同 Conversation 并行；通过
    明确 `submission_id` 启动 Console Native Runtime，Native 与 Harness 都接管同一
    Submission，不重复入队。执行生命周期不依赖 HTTP/SSE subscriber；缺失或不兼容
    envelope 会失败关闭。
17. 进行中：QwenPaw Chat 已迁移到服务端 admission 和只读 Queue Projection；首轮
    发送先通过 Session singleflight 分配 `ChatSpec.id`，普通 Enter 与空白页
    Ctrl/Command+Enter 随后都提交 durable envelope。运行中及非 owner 标签提交不再
    进入 localStorage、BroadcastChannel 或 Web Lock。旧草稿只兼容读取，外部
    backend 暂时保留兼容队列。页面在状态查询期间切换时，SDK 的不可变 submission
    route 继续把输入提交给原 `ChatSpec.id`，不再借用本地 Queue 中转。
18. 已完成：Console API Client 冻结 Submission、Queue、Control Receipt 与统一 Runtime
    Projection TypeScript 契约；Chat Interaction 消费侧从独立轮询迁移到
    `/runtime/stream`，保存服务端 cursor 并用 `Last-Event-ID` 断线续接。流异常时只读取
    一次权威当前快照，不构造本地 Interaction 状态机。
    Queue 与 Interaction 组件通过按 `agent_id + ChatSpec.id` 索引的共享
    Projection Store 复用同一条 SSE；一个页面不再为两个投影视图占用两条长连接。
19. 已完成：页面卸载后的后台 Queue 消费者在能够解析稳定 `ChatSpec.id` 时，不再持有
    `/console/chat` SSE，而是把旧 SDK 请求经单一适配器转换为 durable Submission；
    `session_id/user_id/channel/stream` 等传输身份不会进入新契约，审批上下文、附件、
    模型覆盖和 `session_project_dirs` 保留。项目目录验证与持久化已从 Console 路由
    下沉为 Chat 输入基础设施，旧路由和 Dispatcher 共用。无 ChatSpec 的初始草稿仍走
    兼容流式分配路径。
20. 已完成：`InvocationControlPort.recover_orphaned_submissions()` 与 Lite SQLite
    Adapter 冻结 generation 丢失恢复语义。Dispatcher 启动前先在单一事务中将旧
    active Submission 置为 `interrupted`、结算其 accepted Control Command，再放行
    同 Conversation 的下一条 queued Submission；重复恢复为空操作。
21. 已完成：durable Chat 请求以受保护的 `request_extensions` 保留插件
    `request.payloadTransforms` 顶层扩展；服务端拒绝覆盖 sender、channel、message、
    meta 等 envelope 权威字段，插件扩展与身份防伪不再二选一。

### 6.1 Chat Control HTTP 契约

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/chats/{chat_id}/queue` | 读取服务端权威 QueueProjection |
| GET | `/chats/{chat_id}/capability-locks` | 查询 Invocation 实际锁定的内容安全 release 证据 |
| GET | `/chats/{chat_id}/context-manifests` | 查询模型输入证据及其 capability lock 引用 |
| POST | `/chats/{chat_id}/submissions` | 原子持久化完整输入并唤醒服务端 Dispatcher |
| POST | `/chats/{chat_id}/control/steer` | 向当前 Invocation 提交安全点指令 |
| POST | `/chats/{chat_id}/control/interrupt` | 只打断当前 Invocation |
| POST | `/chats/{chat_id}/control/stop-and-clear` | 原子清除排队项并打断捕获的 Invocation |
| POST | `/chats/{chat_id}/queue/{submission_id}/cancel` | 只取消指定 queued Submission |
| POST | `/chats/{chat_id}/queue/reorder` | 原子替换完整 queued 顺序 |

Steer、Interrupt、Cancel、Reorder 和 Stop and Clear 基于客户端最近读取的 Queue
`revision` 执行；过期 revision 返回 409 与稳定领域冲突，不允许路由偷偷读取新
revision 后代替用户覆盖并发修改。Submission append 默认省略 `expected_revision`，
由服务端事务原子排序；需要严格 compare-and-set 的兼容调用仍可显式提供。相同幂等键
和相同意图即使携带旧 revision 也回放原 Receipt；相同键但不同意图返回 409。

### 6.2 可恢复运行时快照流

`GET /chats/{chat_id}/runtime` 返回当前 `ConversationRuntimeProjection`；
`GET /chats/{chat_id}/runtime/stream` 发送相同模型的 SSE `snapshot`。Cursor 由 Queue
持久化字段和 open Interaction 完整内容计算，不包含 `observed_at` 等读取时生成的
字段。客户端断线后携带 `Last-Event-ID`：服务端状态未变则等待，状态已变则立即返回
最新完整快照。

这是 current-state recovery stream，不是不可丢审计日志。断线期间经历又恢复的中间
状态允许合并；需要追溯每个控制命令、审批决策或执行步骤时，必须读取 durable
Control Receipt、Interaction Resolution 和 Execution/Audit Ledger，不能从快照流
反推历史。

### 6.3 服务端 Submission Dispatcher 门禁

服务端 Dispatcher 及其 durable Submission 接口已经实现，且单元契约已证明恢复扫描、
同一 Conversation FIFO、跨 Conversation 并行、无 subscriber 执行、失败关闭，以及
Native/Harness 精确接管预入队 Submission。QwenPaw 首轮发送已先分配真实
`ChatSpec.id`，所有 QwenPaw Submission 均不再使用浏览器 localStorage Queue、
BroadcastChannel 与 Web Lock admission；这些设施仅兼容读取历史草稿和服务外部
backend。在以下条件全部通过前，禁止删除剩余兼容队列：

1. `POST /chats/{chat_id}/submissions` 先验证 Chat ownership、附件收据和输入模型，
   再原子写入 Submission 与版本化 `SubmissionInputEnvelope`。
2. Queue 仍是唯一调度事实源；输入 envelope 是同一 Submission 的执行载荷，不建立
   第二套 pending/running 状态机。
3. Workspace Dispatcher 只 claim 当前 Conversation 最早 queued Submission；启动
   Runtime 时携带明确 `submission_id`，Runtime 采用该 Lease，不重复 submit 或按
   文本猜测归属。
4. Dispatcher 生命周期独立于 HTTP/SSE subscriber；客户端断线不停止运行。
5. 进程启动后扫描 queued Submission；缺失/不兼容 envelope 必须失败关闭并形成
   可诊断 Receipt，不能静默丢弃或执行空输入。
6. 启动扫描必须先关闭旧 generation 遗留的 admitted/running/interrupting，禁止
   自动重放可能已经产生副作用的 active turn；随后才能调度同 Chat 的 queued turn。
7. `TaskTracker` 暂时只承担单次运行 SSE buffer/reconnect 兼容，不再决定 Queue 顺序
   或是否允许下一轮执行。

剩余验收必须包含：同一 Chat 两个标签提交各执行一次且 FIFO；发送标签关闭后
继续；真实服务进程重启后 queued turn 恢复；活动 turn 中 Interrupt 不清队列；
Stop and Clear 明确清队列。完成这些验证后才能删除剩余本地兼容状态机。

2026-09-28 已完成并发 append 的后端事务验收：两个不携带
`expected_revision` 的 Submission 通过同一 HTTP API 并发提交，均返回 200；服务端
分配不同 `sequence`，Queue 中顺序为 1、2。显式 revision 的控制操作仍保持 CAS
冲突语义。Console 定点测试同时证明稳定 Chat 的 `direct`、`queue` 与
`host-queue` 三种 SDK 来源都会进入同一 durable Submission Adapter，提交前不再
执行 Queue revision 预读。

2026-09-28 已完成稳定 Chat 的浏览器消费侧验收：Chat
`22642923-1fe3-4749-9b05-4ef587951eb1` 阻塞于 Ask User 时连续接收两条 queued
Submission，UI 从 Runtime Projection 显示 2 条；UI cancel 后只剩 1 条，刷新仍恢复
相同服务端状态。回答 Ask User 后剩余输入自动执行为 `THIRD_DONE`，最终 revision 10、
active/queue/interaction 全空。多标签同时写入的完整 FIFO 与浏览器进程完全关闭仍是
剩余验收项。

### 6.4 真实 Interrupt 验收记录

2026-09-28 在真实 Console Chat 中让内置 `ask_user` 持久化一个 blocking
Interaction，在不作答的情况下点击现有 Stop：

1. Chat 主区从运行中切换为“已取消”，保留已经产生的步骤而非删除整轮消息。
2. Ask User 卡片消失，服务端 open Interaction 投影为空，证明 waiter 已被取消。
3. 服务端 Chat 状态最终为 idle；父 Chat 保持 idle，取消只按目标 `ChatSpec.id`
   作用于当前子分支。
4. 定点契约测试进一步证明 cancellation tree 固定按前台工具子任务 → Runtime
   owner 执行，Submission 经过 `interrupting -> interrupted` 后命令才成为
   `applied`；Runtime 晚绑定能够恢复已持久化的 accepted Interrupt。

本次浏览器路径覆盖阻塞 Interaction 中断。长模型流、长进程工具与真实外部
Harness 进程仍需各自实测，不能由 Ask User 取消或适配器定点测试替代证明。

### 6.5 真实 Suggestion 验收记录

2026-09-28 在同一真实 Console Chat 中要求模型调用内置
`suggest_user_action`：

1. Runtime 工具步骤实际执行，Chat 权威投影出现“补充回归测试”建议卡及
   “创建测试”“查看文件”两个选项。
2. Suggestion 投递后模型继续 reasoning 并提交最终回复“建议已发送”，过程中没有
   进入等待用户输入状态。
3. Chat 从 running 回到 idle 后建议卡仍保持 open，证明 Runtime 终态只释放
   blocking waiter，没有撤回已投递的 non-blocking Interaction。
4. 组件定点测试验证无选项建议可通过 dismissal response 关闭；带选项建议的后续
   action dispatch 语义尚未冻结，当前只提交结构化 InteractionResponse。

### 6.6 真实 Runtime Projection SSE 验收记录

2026-09-28 在当前 Console 服务上验证统一 Runtime Projection 消费链路：

1. `/runtime` 返回 `ChatSpec.id` 对应的 Queue、open Interaction、revision 和稳定
   cursor；`/runtime/stream` 首帧返回同一 cursor 的 `snapshot` 并保持连接。
2. Chat 页面通过真实模型调用产生 blocking `ask_user`；无需刷新，统一投影卡片实时
   出现“SSE 实时投影测试是否通过？”及“通过”“失败”两个选项。
3. 选择“通过”后卡片立即从 open projection 消失，Runtime waiter 被释放，执行继续
   并最终回复“收到，SSE 实时投影测试通过”。
4. 页面刷新后完整消息仍从持久化历史恢复，Chat 状态为 idle；本次新增 SSE 消费侧
   没有出现新的浏览器运行错误。

本次证明 Interaction 的实时统一投影与响应恢复；尚未证明 durable Submission 的
多标签并发、关页继续和真实进程重启恢复，这些仍受 6.3 门禁约束。

### 6.7 真实后台 durable Submission 验收记录

2026-09-28 在真实 Console Chat 中执行以下路径：

1. 第一轮调用 blocking `ask_user` 暂停 Runtime；运行期间用输入框排入第二条消息，
   页面显示本地 Queue 数量为 1。
2. 切换到另一个 Chat，使原 Chat 的前台 SDK 与 SSE subscriber 卸载；再通过统一
   Interaction API 响应第一轮“继续”。
3. 原 Chat 第一轮完成后，后台消费者自动提交第二条；测试期间浏览器始终停留在另一
   Chat，第二轮仍由 workspace Dispatcher 执行并写入持久化消息历史。
4. 返回原 Chat 后可见第二轮用户消息和最终回复“后台执行完成”，Chat 状态为 idle，
   本地 Queue 已清空。历史中的 client message identity 与排队时一致。

本次证明单标签“切换页面后继续”；浏览器进程完全关闭、两个标签并发提交及服务进程
重启仍未验收，不能据此删除全部 localStorage/Lock 兼容层。

### 6.8 真实 Runtime generation 恢复验收记录

2026-09-28 在 strict Approval 阻塞两条真实 Console Invocation 时触发后端热重载，复现
了旧 generation 执行句柄丢失而 SQLite Submission 长期停留 `running` 的缺陷。接入
恢复契约后再次重载：

1. 两个遗留 Chat 的 `active_submission_id` 均自动归零，Queue revision 分别前进；
   原 active Submission 保留为 `interrupted` 审计记录而非自动重放。
2. 旧 accepted Interrupt 被恢复事务结算，不再留下无人消费的控制命令；无法注入旧
   Runtime 的 Steer 使用 `rejected` 终态。
3. 在其中一个原阻塞 Chat 上使用最新 revision 提交新消息，服务端正常完成
   `queued -> admitted -> running -> succeeded`，最终回复 `RECOVERY_OK`，Queue 再次
   归零。
4. 44 个 Kernel、SQLite、Service、Dispatcher 与 Chat Submission 定点测试通过；其中
   覆盖恢复幂等、控制命令结算和同 Conversation 下一条 queued turn 自动继续。

该验收证明“运行中 generation 丢失后安全终止并继续队列”，不等价于从模型流中间
续跑。Checkpoint 恢复仍由 Task/Run attempt 层负责，且必须服从 Side Effect Ledger。

### 6.9 真实首轮 Conversation 分配验收记录

2026-09-29 从 Console 的空白 `/chat` 页面进行真实首轮提交：

1. 在尚未分配 Chat 的输入框输入唯一测试消息，通过
   `Ctrl/Command+Enter` 提交。
2. 页面先导航到服务端分配的 Chat
   `54d10df6-2195-4c89-92e1-bd4d2ca679d8`，不再以 `new` 或
   `session_id` 作为 Conversation 身份。
3. 侧栏显示该 Chat 运行中，最终页面返回
   `FIRST_TURN_DURABLE_OK`；权威 Runtime Projection 的 revision 为 4，
   `active_submission_id` 为空、Submission 队列为空、Interaction 为空。
4. 刷新浏览器后，同一 Chat URL、用户消息、最终回复和消息级
   Fork 入口均从持久化历史恢复。

该验收证明首轮“先分配 `ChatSpec.id`，再提交 durable envelope”的
浏览器路径。旧本地 Queue 不承担 QwenPaw 首轮 admission 的静态门禁
由 `durableSubmission` 和 Chat 页面定点测试覆盖；外部 backend 仍保留显式
兼容队列。

### 6.10 真实 AFTER_TOOL_BATCH Steer 验收记录

2026-09-30 使用公开 `tool.provider` 临时安装一个不会切换到后台的 8 秒慢工具，
并在真实 Console Chat 中完成运行中 Steer：

1. 独立监控器先观察到 `wait_for_steer_probe` 的 Tool Coordinator 状态为
   `running`，同时 Queue 仍有 active Submission，再按当时 revision 提交 Steer。
2. Steer 首次返回 durable `accepted`，没有取消已开始的工具；工具正常提交
   `SLOW_TOOL_COMMITTED` 后，控制回执转为 `applied`，实际安全点精确记录为
   `after_tool_batch`。
3. Chat 持久化了一条可见的 Steer 用户消息，随后 Agent 继续 reasoning，最终只
   回复 `STEER_AFTER_TOOL_BATCH_OK`；工具事实没有被覆盖或伪造。
4. 验收后卸载临时插件，`/api/plugins` 恢复为空；全程没有重启服务。

对照试验还确认：Shell 或工具进入用户配置的 background offload 后，原
Invocation 已结束，Steer 会明确返回 `conversation has no active invocation to
steer`，不会错误命中后台任务。若用户需要停止后台工具，应使用 Tool Coordinator
的取消能力，而不是伪装为 Invocation Steer。

### 6.11 真实 BEFORE_REASONING Steer 验收记录

2026-10-10 在固定 Native Chat
`1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 先执行 `/clear`，再提交一个要求长回答且
禁止工具的普通 Turn。监控器在 Queue 首次出现 active Submission 后立即按当前
revision 提交 Steer：

1. 初始 Control receipt 为 durable `accepted`，目标 Invocation 为
   `3d4b4f15-874f-4102-bc99-ec9976ccce03`，没有由前端预判安全点。
2. 最终权威 receipt 为 `applied`，`applied_at_safe_point=before_reasoning`；CONTROL
   intent/evidence 使用同一 command、Chat、Invocation 和 correlation identity。
3. Chat 持久化的 Steer 用户消息携带同一 command ID 和 `before_reasoning` metadata；
   模型最终只回复 `STEER_SAFE_POINT_OK`，原长回答没有执行。
4. Queue 最终无 active/queued Submission，Interaction 为空；页面刷新后消息仍从
   Session 历史恢复。

该结果证明服务端可以在首次模型请求前应用已接受 Steer。

### 6.12 真实 BEFORE_TOOL_BATCH Steer 验收记录

2026-10-10 在同一固定 Native Chat 先执行 `/clear`，再通过公开
`loop.gate.provider` 临时安装一个可观测的 tool admission Gate。该 Gate 在模型已经
产生工具调用、工具批次尚未 admission 时写入 `ENTER` 标记并等待 12 秒；监控器观察
到标记后提交 Steer，而不是依赖随机竞争窗口：

1. Submission `9aea0160-e353-4e65-8b82-fb5b5bc6cd20` 的 Invocation 为
   `6033e99a-1298-44ad-bff3-9b808925f7c0`；Steer command
   `47acab50-7aed-4d00-9bf1-741bb3fcf54e` 首先返回 durable `accepted`。
2. Gate 正常写入 `EXIT` 后，权威 Control receipt 转为 `applied`，并精确记录
   `applied_at_safe_point=before_tool_batch`。
3. 模型继续两次 reasoning，最终回复包含 `STEER_BEFORE_TOOL_BATCH_OK`；Action
   Observation 为 0，证明原工具批次没有越过 admission，也没有产生部分副作用。
4. 验收使用真实公开插件 SDK 和热安装路径，没有 mock Runtime；完成后已卸载临时
   插件，`/api/plugins` 恢复为空，安装目录与仓库探针文件均已清理。

该验收同时发现并修复一处 Provider 组合缺陷：旧 `StopHandler` 在没有 Gate 命中时
返回无任何载荷的 `TERMINATE`，其旧语义实际是“允许循环停止”。进入多 Provider
Router 后，这种返回现在会在兼容边界归一为 `BYPASS`，避免系统 Provider 隐藏后续
插件 Gate；任何带 reason、continuation、final message 或 tool-call 注入的真实终止
决策仍保持 `TERMINATE`。回归测试同时覆盖系统 Gate 未命中、插件 Gate 随后命中的
组合路径。

## 7. 验收

- 在 reasoning 前、reasoning 流中、reasoning 后、工具批次前和工具批次运行中分别
  提交 Steer，结果符合安全点矩阵。
- 并行工具批次不会发生部分 admission；已开始副作用不会被 Steer 静默取消。
- 每个 applied Steer 都能在 Conversation 和 receipt 中找到同一 command identity
  与实际 safe point。
- Interrupt 能终止长模型流和长工具，并保留部分输出、取消审批、提交唯一终态。
- 清空浏览器存储、关闭页面或重启服务后，Queue 与 accepted command 仍可恢复。
- Approval、Ask User 和 Suggestion 可通过同一订阅通道投递；不同
  界面的响应经同一 revision/幂等校验后才能恢复 Runtime。
