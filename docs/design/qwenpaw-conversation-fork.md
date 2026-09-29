# QwenPaw Conversation Fork 设计

- 状态：Kernel 契约、Lite Adapter、OS Chat 后端、Console API Client、消息操作
  UI 与统一审批投影已实现
- 产品基线：OS / Chat-first；Lite 是首个产品装配
- 非目标：复制正在运行的 Invocation、共享父子 Queue、复制 Artifact 内容

## 1. 用户语义

用户可在一条已经持久化的消息上选择“从这里分叉”。服务端创建新的
`ChatSpec.id`，子 Chat 继承父 Chat 从开头到锚点消息（含锚点）的上下文；父 Chat
保持不变。子 Chat 后续拥有完全独立的 Queue、Invocation、Steer、Approval、
Ask User 和 Checkpoint。

UI 中一条 AgentScope 消息可能被拆成 reasoning、文本、工具调用等多个渲染段。
这些段共享 `Message.source_message_id`。Fork API 只接受该持久化身份，不能使用每次
历史转换时可能变化的渲染 `Message.id`，也不能使用渠道 `session_id`。

产品界面将 Fork 表述为“从这轮对话分叉”，而不是从任意渲染卡片分叉。一轮对话
（Turn）由用户输入、该次运行产生的 reasoning / tool 过程和最终 assistant 回复组成。
当前 Chat Adapter 以这轮最终 assistant `Msg.id` 作为包含式边界，因此创建出的子 Chat
同时继承这组消息对及其完整工具过程。前端只在已完成回复上显示 Fork 入口；进行中、
失败后未形成稳定回复边界以及临时渲染段均不是合法锚点。

这一约定不要求再引入一套 `turn_id`。`ChatSpec.id` 仍是 Conversation 所有权，最终
assistant `source_message_id` 只是父 Chat 内部的不可变历史坐标。若后续需要支持“保留
用户问题、丢弃旧答案后重新生成”，应新增显式的 `before_response` 分叉模式，不应
改变当前 `after_response` 的含义。

## 2. 稳定契约

Kernel 以 `ConversationForkCommand`、`ConversationForkOrigin`、
`ConversationForkResult` 和 `ConversationForkPort` 冻结存储无关契约。命令显式携带
`agent_id + parent_conversation_id`，所以调用方只理解 `ChatSpec.id`，不接触兼容
存储使用的 `session_id`。Lite 由 `LiteConversationForkAdapter` 把 Kernel 命令映射到
现有 JSON Chat registry 与 AgentState snapshot；未来 Workstation/Hub 可替换 Adapter，
不改变 HTTP、Console 或插件 SDK 类型。

Task Runtime 的稳定身份同样使用可选的 `conversation_id`。它必须来自真实
`ChatSpec.id`；没有绑定 Chat 的后台 Task 保持为空，不能用 `task_id` 或临时
`session_id` 冒充 Conversation。旧 `session_id` 只负责定位 Console/AgentScope
状态，由兼容适配层消费。Schedule 也可显式绑定同一 `conversation_id`，因此一次
定时执行进入 Runtime 后，Queue、Interaction、Approval、Delivery 与后续 Fork
能够指向同一个 Conversation 聚合。

Fork 是核心身份与授权基础设施，不是可被普通插件替换的 Contribution Slot。插件 SDK
只公开稳定模型与 Host 提供的 Port；插件不能覆盖锚点校验、lineage 授权或子
`ChatSpec.id` 分配。

`ChatForkRequest`：

- `source_message_id`：包含式 Turn 边界，对应最终 assistant 的持久化 AgentScope
  `Msg.id`；不是前端渲染 ID。
- `idempotency_key`：一次用户操作的幂等键；相同键和相同载荷返回同一子 Chat。
- `name`：可选子 Chat 名称。

子 Chat 的 `ChatSpec.fork_origin`：

- `parent_chat_id`：父 `ChatSpec.id`。
- `source_message_id`：创建分支时采用的包含式边界。

不在 Fork 契约中存储 `session_id`。渠道兼容层可为子 Chat 生成独立存储句柄，但它
不能成为父子关系、运行所有权或查询分支的依据。

### 2.1 两种用户动作不能混用

首期稳定动作是 `after_response`：用户在一组已经完成的“提问 + 工具过程 + 最终
回答”上选择“从这轮对话分叉”，子 Chat 保留这组消息对，然后从下一条输入继续。
它适合探索多个后续方向，也是当前 Lite 已实现的语义。

后续可增加独立的 `before_response` 动作，产品文案应为“从这里重新回答”：保留该轮
用户问题，但不继承旧 reasoning、工具结果和最终回答，再创建新的 Invocation。它必须
使用新的枚举值和独立验收，不能通过把 `source_message_id` 偷换成 user message 来实现，
否则旧客户端无法判断边界语义。

两种动作都以服务端识别的 Turn 边界为准。Console 可以从渲染响应推导锚点，但只有
服务端有权确认锚点是已持久化的最终 assistant 消息。

### 2.2 Conversation Fork 与 Workspace Fork

Conversation Fork 只创建新的 `ChatSpec.id`、历史快照和 lineage，不回滚已经发生的
外部副作用，也不默认复制 Git 工作区、进程、浏览器或远端环境。历史工具调用仅作为
上下文保留，不会在创建分支时重新执行。

- Lite：默认共享当前工作区，只隔离 Conversation、Queue、Invocation、Interaction
  和新产出的 Artifact。界面应明确显示“共享工作区”，避免用户误以为文件也已分支。
- Workstation：可由 `WorkspaceForkPort` 把同一个 Fork origin 绑定到 Git worktree
  或安全 checkpoint；Conversation 契约本身保持不变。
- Hub：可把 Fork origin 绑定到远端 sandbox snapshot，并附带 tenant、权限与保留
  策略；仍不能把 sandbox 标识变成 Conversation identity。

因此，环境隔离是 edition adapter 的显式选项，不是
`ConversationForkCommand` 的隐藏副作用。若环境快照创建失败，要求隔离环境的请求必须
整体失败；不能静默降级成共享工作区。

### 2.3 分支一致性

子 Chat 固定继承锚点之前的 Conversation summary、消息、Artifact/Evidence 引用，
但下一次 Invocation 使用创建时可用的 capability generation。若产品需要可复现分支，
应额外绑定 `checkpoint_id`、模型配置和 capability generation；普通 Lite 分叉只承诺
对话历史一致，不承诺重演得到相同答案。

共享的长期 Memory 也不能伪装成历史快照。默认 Lite 分叉可读取当前用户 Memory，但
界面与审计应将其标记为环境输入；可复现模式必须使用固定 Memory snapshot 或关闭该
类动态注入，防止父分支在锚点之后写入的记忆泄漏到重演结果。

## 3. 创建事务

Chat Adapter 按以下顺序执行：

1. 以 `parent_chat_id + idempotency_key` 查询既有结果，命中则返回。
2. 读取父 Chat 的不可变历史快照，定位 `source_message_id`；不存在返回 404，未完成
   或尚未持久化返回 409。
3. 服务端确认锚点属于已完成 assistant 回复，再截取到该锚点的 Conversation
   消息；用户消息、reasoning、工具调用和最终回复作为同一轮整体保留，不复制
   active Invocation、pending Interaction、Queue、临时流片段或取消令牌。
4. 创建新的 `ChatSpec.id` 并保存 `fork_origin`。
5. 写入子 Conversation 快照及 Fork 审计事件后提交幂等结果。
6. 任一步失败均不得暴露半创建 Chat；孤立快照可由恢复器清理。

当前 `SafeJSONSession` 在同一进程内同时锁住父、子路径，读取完整父快照后只重建
新的 AgentState：保留 summary 与锚点前后的历史边界，重置 reply、permission、
tool、tasks 和 middle context。Chat registry 写入失败时立即删除子快照。

允许父 Chat 在锚点之后继续产生消息；Fork 只依赖不可变锚点，不要求复制时父 Chat
仍停留在该消息。若锚点本身正在流式生成，则拒绝 Fork，避免继承半条回复。

## 4. Artifact、Evidence 与审批

- 继承历史中的 Artifact/Evidence 只复制不可变引用和完整性收据，不复制内容。
- 子 Chat 新产出的 Artifact 创建新版本；不得覆盖父分支引用的对象。
- 父 Chat 的 pending Approval、Ask User、Suggestion 不进入子 Chat。
- Fork 后新建的 Approval 以 `ChatSpec.id + Invocation.id` 归属；响应 API 必须同时
  校验 Interaction 的 `conversation_id`。拿子审批 ID 到父 Chat 响应返回 404，不能
  依赖相同 user、channel 或兼容 `session_id` 获得跨分支权限。
- Fork 后提交的第一条消息进入子 `ChatSpec.id` 的独立 Submission Queue。
- 父子分支可以并行运行，因为单 active Invocation 约束的作用域是各自
  `ChatSpec.id`。

## 5. 实现与验收

- [x] `POST /chats/{parent_chat_id}/fork` 返回子 `ChatSpec`。
- [x] Kernel 冻结 Conversation Fork 命令、来源、结果及 Port；Lite JSON 实现通过
  Adapter 接入，HTTP 不再直接编排 `SafeJSONSession`。
- [x] 原子截取 Conversation snapshot，不复制完整 Session 运行态。
- [x] 相同幂等键的并发请求只生成一个子 Chat；冲突载荷返回 409。
- [x] 目标回复尚未完成时拒绝 Fork；父 Chat 执行更晚一轮时仍可从已完成消息对
  Fork，成功后的幂等重试返回同一子 Chat。
- [x] registry 写入失败时回滚子快照，不留下可见或孤立分支。
- [x] Console API Client 暴露 `forkChat()` 与稳定 TypeScript 契约。
- [x] 在 Chat 消息操作中加入 Fork 入口并导航到子 Chat。
- [x] 浏览器实测从两个不同消息对分叉；较早消息对只继承锚点及之前历史。
- [x] Console 只从 `completed` response 的最后一个持久化
  `source_message_id` 发起 Fork，不使用渲染消息 ID。
- [x] 领域服务定点验证父子 Queue 与 pending Interaction 按 `ChatSpec.id`
  隔离；父 OS Invocation Queue 运行中创建子分支时，子 Queue 仍为空且独立。
- [x] 验证子快照继承相同 Artifact/Evidence 引用和完整性收据，不复制运行态；
  下载通过服务端生成的可信 Fork lineage 只读授权，非 lineage Chat 仍被拒绝。
- [x] 浏览器验证父子并行运行时 Queue 与 Ask User 互不干扰：父、子 Chat
  同时处于 running，各自只展示所属 Interaction；子分支完成后父分支仍保持 open，
  分别作答后均独立回到 idle。
- [x] 领域服务验证父 Steer 只能在父 safe-point 消费；Interrupt 子 Runtime 后父
  submission 保持 running 并可独立完成。
- [x] 领域服务验证父子 strict blocking Approval 同时 open；处理子审批不会释放、
  覆盖或隐藏父审批。
- [x] 原生 `PolicyGuardedTool` 与 Driver/MCP 共用 Interaction 基础设施；strict 工具
  审批在 Chat API 可见，桥接失败时 fail closed，不遗留只在旧服务可见的等待态。
- [x] 真实 Runtime 验证父子 strict Approval 各自 open；跨 Chat 响应返回 404，批准
  父审批后子审批仍 open，分别批准后各自继续并清空 Queue。
- [x] 领域链路验证子分支可通过可信 lineage 预览父附件；子分支产生同名新
  Artifact 时拥有独立 ID 与内容哈希，父分支不能反向读取子产物。
- [x] 浏览器验证继承附件预览及同名新 Artifact 的父子独立展示。
- [x] 后端把“锚点必须是已完成 assistant 回复”升级为显式领域校验，防止非 Console
  客户端绕过 UI 传入 user / 中间 assistant 消息；旧历史按结构化 Turn 边界兼容。
- [x] 稳定 Chat 路由在前端身份映射尚未恢复时，先以当前 Agent 验证路由中的
  `ChatSpec.id` 所有权，再允许提交；避免刷新后误退回 `session_id` 兼容链路，也
  防止把另一个 Agent 的同名路由当成分支身份。
- [x] 父子页面各自按 `agent_id + ChatSpec.id` 创建一个共享 Runtime Projection
  订阅；Queue 与 Interaction 复用该订阅，页面卸载后释放，不共享父子 SSE、cursor
  或刷新状态。
- [x] 父子 Chat 的普通消息 append 不依赖客户端 Queue revision；服务端事务分别在
  各自 Conversation 内分配顺序。Steer、Interrupt、审批响应及 Queue 重排继续使用
  所属 Chat 的 revision，因此不存在从父分支携带旧 revision 覆盖子分支的路径。
- [x] PawApp 长任务启动时绑定真实 `ChatSpec.id + invocation_id`；
  `UIBridge.confirm()` 先写入 durable `USER_INPUT` Interaction，再发送兼容 SSE
  envelope。SDK 响应统一调用 Chat Interaction API；取消 Task 同时取消该
  invocation 的未决交互。Fork 感知的应用以
  `paw.api.task(path, params, {chatId: childChat.id})` 显式绑定子 Chat；Host 会校验
  该 Chat 的 App、Agent 与用户所有权，未传时才回落到应用默认 Chat。
- [ ] 浏览器验证 PawApp 在 Fork 父子 Chat 中各自发起 confirm、断线重连后仍可见，
  且热替换 UI Contribution 不丢失服务端未决 Interaction。

## 6. 真实链路验收记录

2026-09-28 在本地 Console 与真实 Runtime 上完成父子并行验收：

1. 父 Chat 发起阻塞式 Ask User，服务端 Interaction 投影为 `open`。
2. 不结束父 Invocation，切换到 Fork 子 Chat 并发起另一个 Ask User；侧栏同时显示
   两个 Chat 为运行中，子页面只呈现子 Interaction。
3. 子 Interaction 完成后，父 Chat 的 Interaction API 仍返回原始 open 请求，证明
   子分支的 resolution 没有按渠道 session 或全局状态错误释放父分支。
4. 返回父 Chat 选择另一选项，父 Invocation 正常继续；随后复验子 Interaction 在
   8 秒轮询窗口内持续保持 open，明确作答后才恢复。
5. 最终父子 Interaction 投影均为空，Chat 状态均为 idle。

本次验证覆盖 Queue 的单 active Invocation 作用域和统一 Ask User Interaction 的
Conversation 隔离。

同日在真实 Console 完成附件与 Artifact lineage 验收：

1. 父 Chat 上传并发送 `fork-shared.md`，回复完成后从该消息对 Fork。
2. 子 Chat 保留父附件卡片，并通过 Artifact Renderer 成功预览 37,423 字节内容。
3. 子 Chat 再上传同名但内容不同的文件；页面同时展示两张同名附件卡片，新文件
   预览为 7,192 字节，继承文件仍为 37,423 字节。
4. 返回父 Chat 后，子分支回复不存在，附件卡片仍只有一张，证明子产物不会反向
   泄漏或覆盖父分支历史。
5. 对应领域测试进一步确认两个 Artifact 使用不同 ID 与内容哈希，父 Chat 直接读取
   子 Artifact 返回 404。

同日完成“父 Chat 运行中从较早消息对 Fork”的真实 Runtime 验收：父 Queue 的
submission 已进入 `running` 后，以较早 completed assistant `source_message_id` 调用
Fork API 返回 200；父 Queue 保持原 active Invocation，子 Queue revision 为 0 且没有
active submission。父 Invocation 随后正常完成，证明 Fork 只读取带锁的持久化历史
快照，不复制或打断更晚的运行态。

同日完成 Steer / Interrupt 的真实 Runtime 隔离验收：父子分支分别进入阻塞式
Ask User；父分支提交 Steer 后，仅父 safe-point 消费指令并在最终回复包含
`PARENT_STEER_APPLIED`。对子分支执行 Interrupt 后，子 Interaction 被取消且页面显示
已取消，父 Interaction 保持 open、父 Queue 保持 active，作答后父分支正常完成。

同日完成严格工具审批的真实 Runtime 验收，并修复原生工具审批未投影到 Chat 的
基础设施缺口：

1. 以同一个已完成 assistant `source_message_id` 创建子 Chat，父子分别提交
   `approval_level=strict` 的 `grep_search` 调用。
2. 两个 Chat 的 Interaction API 各返回一张 `Approve Grep` 阻塞卡片，且拥有不同的
   `conversation_id`、`invocation_id` 和 `interaction_id`。
3. 使用子 `interaction_id` 调用父 Chat 的 response API 返回 404
   `Interaction does not belong to this chat`。
4. 批准父审批后，子审批仍为 `open`；随后批准子审批，两条工具调用分别继续，最终
   两个 Queue 的 `active_submission_id` 均归零。
5. `get_current_time` 不作为 strict 验收工具：它属于 `internal` 基础设施，按策略在
   Phase 0 直接允许，以避免 Ask User 等系统工具产生递归审批。
