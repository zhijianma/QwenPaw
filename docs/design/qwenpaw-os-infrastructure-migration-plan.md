# QwenPaw OS 基建迁移执行计划

- 日期：2026-09-24
- 状态：实施中
- 当前产品基线：Lite / Chat-first
- UI 冻结：暂停 Task Workbench 功能开发，仅保留既有回归验证
- 长程交互基线：Runtime 使用 intent-driven continuous execution；一问一答仅是
  短 Chat 快速路径和 Conversation 投影

## 1. 本阶段目标

先完成 OS R0 的基础设施契约、运行装配和兼容迁移，再恢复 Task 页面开发。
Chat 是当前唯一真实交互验收入口。Lite 提供本地实现；Workstation 与 Hub 在
本阶段只冻结公共 Port 和 Profile 组合边界，不提前实现分布式能力。

当前执行目标以本计划为准：先完成 Chat-first Kernel、Harness 与 OS Adapter，
Task Workbench 后置。一个长程意图由 `ChatSpec.id + correlation_id` 聚合，可跨多个
Submission、Invocation、Action、typed wait 和 continuation 自主推进。Assistant
Message、HTTP response、SSE 断线或一次 Invocation 成功都不能单独终止该意图；只有
显式 Outcome、Stop / Interrupt、不可自动化的 typed wait，或预算与恢复终态可以
结束执行链。Ask User 只用于必要事实、实质偏好、范围授权和高影响裁决，禁止用
“是否继续”充当调度器。

本阶段完成后，业务代码不再直接依赖旧 Workspace Registry、具体存储、具体
AgentBuilder、DriverManager、Harness 或 Scheduler；这些实现只能作为系统
Contribution 或 Port Adapter 存在。

`docs/share/qwenpaw-detailed-solution.md` 已完成差异审计。其 Reliable Work、
Causal Event、Side Effect、Verification、Inbox Delivery 与 Artifact Registry
语义纳入本计划；代码继续以 `Task` 作为唯一持久目标聚合，不新增平行 `Work`
模型。详细裁决见
`docs/analysis/qwenpaw-detailed-solution-gap-analysis-2026-09-24.md`。

## 2. 强制边界

1. `kernel` 不依赖 FastAPI、AgentScope、SQLite、文件系统、插件加载器或 UI。
2. 内置能力与插件能力使用相同 Descriptor、Slot、Generation 和 Lease。
3. 每个系统能力域使用独立 provider namespace，禁止不同 bundle 相互覆盖。
4. 每次 Invocation 固定 generation；热安装只影响后续 Invocation。
5. 高风险动作统一经过 Approval Broker、Policy 与 Sandbox，失败关闭。
6. Artifact、Evidence、Conversation 和 Approval 只有一个事实源。
7. 旧 API 只能通过兼容适配器进入新契约，并标明弃用与删除门槛。
8. 每个模块同时交付契约、系统适配器、插件路径、兼容测试和真实 Chat 验收。
9. 执行成功、Artifact 生成与 Acceptance 验证通过是三个独立事实。
10. 外部副作用使用独立 Side Effect 幂等记录，不能只依赖 API 幂等键。
11. Inbox 是领域事件的 Delivery Projection，不复制或改写事实源。

## 3. 模块分类

| 层级 | 模块 | 定位 | 本阶段结果 |
|---|---|---|---|
| K0 | Domain Models / Ports / State Machines | 核心基建 | 冻结稳定 API 与依赖纯度 |
| K1 | Capability Catalog / Generation / Lease | 核心基建 | provider 隔离、热替换、回滚、排空 |
| K2 | Invocation Scope / Runtime Assembly | 核心基建 | 所有运行能力从固定 generation 装配 |
| K3 | Approval / Policy / Sandbox / Audit | 核心基建 | 单一治理入口和可追溯决策 |
| K4 | Execution Contract / Invocation Control | 核心基建 | 可靠运行约束、统一因果身份与服务端运行控制 |
| K5 | Ledger / Artifact / Evidence / Verification | 核心基建 | Port、Lite adapter、完整性与验收 |
| R1 | Agent / Mode / Prompt / Command / Hook / Gate | 运行基建 | 完成旧 Runtime 的 Contribution 迁移 |
| R2 | Tool / MCP / Memory / Driver | 运行外设 | 类型化 Provider、治理身份和生命周期 |
| R3 | Harness / Strategy / Scheduler | 执行外设 | Native/Codex/Qoder/策略/调度同一协议 |
| E1 | Plugin SDK / Manifest / Migration Tooling | 扩展基建 | 安装即生效、开发者只依赖公开 SDK |
| A1 | Chat / Channel / Cron Compatibility | 应用适配 | 旧入口复用 Kernel，不产生第二事实源 |
| X1 | Task Workbench | Experience | 暂停新增，基建完成后恢复 |

## 4. 分阶段 Checklist

### I0：Catalog 所有权与命名空间收口

- [x] Task 系统 bundle 使用独立 `qwenpaw.system.tasks` namespace。
- [x] 修复 Chat `qwenpaw.system` bundle 覆盖 Task bundle 的根因。
- [x] 每次取得 Task resolver 时使用幂等 `ensure_bundle()` 自愈。
- [x] 保留旧 Task capability ID 到新 ID 的启动兼容映射。
- [x] 为所有系统 bundle 增加 provider namespace 唯一性测试。
- [x] 增加热替换失败、旧 lease 重入、排空和 cleanup 顺序综合测试。

验收：连续执行 Chat Assembly、Task Assembly、插件安装与卸载后，各 Slot 的系统
能力仍完整；运行中的 lease 不漂移。

### I1：Kernel 与 Runtime Assembly 冻结

- [x] 审计 Kernel 依赖纯度并补自动门禁；递归禁止产品、框架、数据库和文件系统
  实现反向进入 Kernel。
- [x] 支持 Task Run 固定 generation 进入嵌套 Chat Invocation，外部请求不能
  伪造 generation；旧 generation 仅在 lease 保留期间可重入。
- [x] 冻结 InvocationScope、CapabilitySelection 与 Provider Session 基础生命周期：
  Invocation 建立时完整校验选择，FINALLY 在 Session 存活时运行，资源逆序关闭，
  单个 close 失败不泄漏后续 Session 或 generation lease。
- [x] 保持 Task Strategy 与 Chat Agent Mode 分离；Task Strategy 固定在 Task
  generation，参数只通过类型化 RuntimeContext 传入 Chat Invocation。
- [x] 统一 Session close、异常清理、取消与 timeout 顺序；FINALLY 保持 Session
  存活，二次取消不能打断排空，部分 Provider 装配失败会逆序回滚已打开 Session。
- [x] 以机器可读 SlotContract 明确所有现有 Slot 的输入、输出、生命周期、失败
  语义和稳定性；未知 Slot 失败关闭。

验收：Chat 的 Agent、Mode、Prompt、Command、Hook、Gate 来自同一 generation；
Task Strategy 来自同一 Task Run 固定的 generation，并以有界参数进入 Chat。
任一缺失或 Slot 不匹配时准确 fail closed。

### I2：治理基建收口

- [x] Approval Broker 成为 Tool、Driver、Harness、Proposal 的唯一 durable 审批
  入口；旧 ApprovalService 仅保留交互投影和 Future 唤醒职责。
- [x] Policy 决策、Sandbox escalation、durable Approval 与 Audit Record 使用
  Runtime 所有的 Invocation/Correlation 身份；外部请求不能伪造内部身份。
- [x] Side Effect Record 使用独立幂等键并关联 Policy、Approval 与执行事件；
  `ToolEffect` 由内置工具、Plugin API 与 Tool Provider 显式声明，durable Task
  在执行前后向同一 Ledger 写入 prepared/terminal 事件，普通 Chat 保持兼容。
- [x] 并行审批、拒绝、超时、取消和恢复具有确定状态机；审批请求同步生成
  安全 Checkpoint，Runtime 丢失时旧 Run 失败化、pending approval 取消、未决
  Side Effect 转 uncertain，用户明确授权重试前禁止创建新 attempt。
- [x] 内置与插件 Runner 通过同一 `RuntimeContext.checkpoint_broker` 保存运行中安全
  边界；宿主绑定 Task/Run/sequence、限制 32 KiB、提供幂等写入，并以
  `checkpoint.created` 与 `run.suspended` 区分“可恢复”与“已暂停”。
- [x] Resume 的领域提交与 Runner 启动具有统一幂等边界；并发重试或完成后的响应
  重放只返回同一持久化 Run，不再启动第二个执行协程。恢复命令按 Task 串行化，
  新 attempt 仍从当前 generation 解析原 runner ID，热替换不会复用旧实现。
- [x] 清除绕过 Broker 的遗留直接审批调用；源码中仅 TaskApprovalBroker adapter
  可以调用 `TaskService.request_approval()`。

验收：严格模式真实 Chat 中，所有高风险操作先持久化审批，再执行或拒绝；取消
不会遗留等待者或孤立审批。

### I3：Execution Contract 与因果事件

- [x] 冻结自治级别、预算、重试、超时、退出条件、必需产物和验证策略的
  类型化契约；L3 缺少 Acceptance、Permission、四类资源上限、退出条件或验证
  策略时 fail closed，副作用自动重试策略不一致时拒绝创建。
  - [x] `max_duration_seconds` 在首次 Run 固化为 durable wall-clock deadline，恢复
    attempt 沿用同一截止点，不重置总预算；进入执行边界时换算为 monotonic timeout，
    并与 attempt/host timeout 取最严格边界，耗尽时以
    `TaskExecutionBudgetExceededError` 失败化 Run。
  - [x] Token、工具调用和重试次数进入统一 Usage Meter；实际用量先入 Ledger，
    越界后以 `TaskExecutionBudgetExceededError` 失败化 Run，恢复 attempt 延续累计。
  - [x] Cost delta 与 `max_cost_micros` 已冻结并可执行；Runner 明确报告费用时
    统一累计和阻断。
  - [x] `max_concurrency` 已进入 Tool Coordinator 的真实 handler
    acquire/release 边界；前台、后台 offload、内置工具和插件工具使用同一槽位，
    计量失败时工具 handler 不会启动。
  - [x] Runner 通过公开 `CostAccountingMode` 声明 `reported/zero/unknown`；Console
    usage 缺少价格字段时持久化 `cost_unknown`，配置成本上限的 Run 在任何 Provider
    调用前写入证据并 fail closed，不再把未知费用当作零费用。
  - [x] Lite 跨 HTTP 的 subagent/fork 通过宿主签发的 opaque Usage Scope 继承
    根 Task 的同一个 Usage Meter；HTTP 不序列化进程对象，scope 按 `agent_id`
    绑定并以引用计数租约覆盖后台子孙生命周期。子模型 Token/未知费用和工具调用
    写回根 Ledger，根 Run 完成前再次校验累计预算，不能把子任务越界降级为普通
    工具错误。
  - [ ] Workstation/Hub 跨进程部署需将 Lite 进程内 Usage Scope Registry 替换为
    具备签名或服务端持久化租约的分布式适配器；公开 HTTP 契约仍只传 opaque scope。
- [x] Task、Scheduler、Proposal 与 Chat 升级 Task 共用同一 Execution Contract。
  - [x] Task API、TaskService、TaskOrder、RuntimeContext 使用同一领域对象。
  - [x] Proposal/Sensor 审批转换和旧 `/console/chat/task` 升级路径保留契约。
  - [x] Kernel `ScheduleDefinition` 直接引用同一 `ExecutionContract`，Dispatcher
    原样写入 Task；Schedule 自身的 Fire 并发与重试只约束触发租约，不复制 Task
    执行预算。尚未无损迁移的旧 Cron 类型继续留在 I6 兼容清单。
  - [x] required `acceptance_met`、`artifact_emitted` 与 `explicit_signal` 进入
    completion gate；`max_iterations` 下推 Console ReAct，并由
    `runner.iteration` 为插件/Harness 提供宿主兜底。未满足时 Run fail closed。
  - [x] optional Result-bound Exit Condition 在持久化信号后由宿主投影判定；仅当
    完整 completion gate 同时通过时才关闭 Runner，并写入带直接 cause 的
    `exit_condition.triggered`。`max_iterations` 仍是失败关闭的硬边界。
- [x] Execution Event 增加兼容的 invocation、step、cause、correlation 与
  source identity；
  旧事件缺少新增字段时仍可 replay。
- [x] Invocation、Policy、Approval、Sandbox、Side Effect、Artifact 与 Verification
  共享 invocation/correlation identity。
  - [x] Runner Signal、Approval 与 Side Effect 写入顶层
    invocation/correlation/source。
  - [x] Artifact/Evidence/Verification Registry 由宿主事件信封派生 event、step、
    cause、source 与 correlation identity；插件 metadata 不作为审计来源。
- [x] 旧事件可无损 replay；每个新 Run 建立根 correlation，恢复 attempt 延续
  同一根 identity；Ledger 拒绝不存在、跨 Task 或指向未来的 cause_event_id。

验收：任一受治理动作可从 Invocation 追溯到 Policy、Approval、执行、副作用、
Artifact 与 Verification；预算或退出条件触发可解释且 fail closed。

### I3A：API 与 Application 边界迁移

- [x] Task HTTP 输入模型移出路由，使用严格、可版本演进的公开契约。
- [x] Create、List、Detail 进入与 FastAPI 无关的 `TaskApplicationService`。
- [x] Start、Cancel、Resume 进入独立执行应用服务；Runner 选择、孤儿恢复和
  执行结果聚合不再由 route 实现。
- [x] 执行应用服务只依赖最小 Runtime Port；FastAPI 入口负责装配具体
  Orchestrator、Capability Resolver 与 Supervisor。
- [x] Projection、Approval、Artifact、Sensor 和 Event Stream 逐批迁入对应
  Application Query/Command，route 最终只做协议解析、序列化和错误映射。
  - [x] Projection 与 Artifact 已切换到 `TaskWorkbenchReadModel` 和完整
    Result Projection；route 不再以 1000 条事件上限拼装产物事实。
  - [x] Conversation 消息合并、Tool activity 筛选、pending Approval 与最近决策
    已下沉为强类型 Workbench 投影；HTTP route 只做公开结构序列化，不再解释
    Execution Event 或审批状态。
  - [x] Approval 决策、并行取消、运行时续接和 Proposal 调度已进入
    `TaskApprovalApplicationService`。
  - [x] Event Page 与 SSE follow 生命周期已进入
    `TaskEventApplicationService`，route 不再直接轮询 Ledger。
  - [x] Sensor Poll 已进入 `TaskSensorApplicationService`，Agent identity
    和 approval-gated host 调用不再由 route 编排。
  - [x] Sensor 轮询上下文已冻结为 `SensorContext`：Host 注入可信
    `agent_id + registry_generation` 与 32 KiB JSON trigger，限制单次 25 个
    Proposal，并强制 `Proposal.source` 等于 capability ID。旧无上下文
    `propose()` 通过兼容路径保留；新插件使用 `propose_context()`。
  - [x] 内置 proactive memory 已迁入
    `qwenpaw.system.tasks.proactive-memory-sensor`，不再直接调用 Proposal
    persistence；system/plugin Sensor 均写入同一 Task、Run、Approval Ledger，
    并记录 `sensor_registry_generation`。共同合同验证 pending Approval、请求者
    身份、事件顺序和 generation 固定。
  - [x] Registry 准备、Edition 选择、Supervisor、Task Application Services 与
    Proposal 后台生命周期已收口到进程级 `TaskApplicationHost`；同一 HTTP request
    只组合一次绑定，route 不再持有 Runtime 或后台 Task 集合。
  - [x] Artifact ownership、Edition Store 选择、兼容读取、预览预算和 generation
    固定 Renderer 已进入 `TaskArtifactApplicationService`；HTTP 只保留状态码和
    安全响应头映射。
  - [x] Capability Catalog 的 generation lease 生命周期，以及 Side Effect 查询和
    人工重试授权已进入独立 Application Service；route 不再穿透
    `EditionRuntimeBindings` 或 `TaskService`。
- [x] 冻结错误码、分页、幂等 Header 与响应投影兼容矩阵，并补契约测试。
- [x] 删除 route 中完成迁移后的私有业务 helper 与重复 runtime 装配；当前仅保留
  HTTP problem 映射、协议解析、序列化和安全响应头。

迁移期间继续使用当前 `/api/tasks` 作为唯一外部 HTTP 契约，不建立长期并行的
`/v2` 路由，也不复制 Task/Run 状态。所谓“重构版本”是内部模块边界：
`HTTP Contract -> Route Adapter -> Application Command/Query -> Kernel Port ->
Domain Service`。旧实现按 endpoint 逐批切换，已迁移接口不得再直接访问存储或
拼装运行策略。

验收：Application 层可以脱离 FastAPI 定点测试；HTTP 集成测试证明迁移前后
状态码、错误码和响应字段兼容；同一命令只有一个 Task 事实源和一个执行入口。

### I3B：Queue / Steer / Interrupt 运行控制面

定位：这是 Chat-first Runtime 核心基建，不属于前端状态管理，也不依赖 Task
Workbench。前端只能提交命令、订阅事件和展示服务端投影；不得使用
`localStorage`、Web Locks、组件生命周期或浏览器 `AbortController` 充当队列、
运行所有权或取消事实源。

当前审计：

- [x] 已确认旧 Channel 具有进程内 `UnifiedQueueManager`，但队列按
  `(channel, session, priority)` 拆分，不同优先级可并行消费；它不持久化、没有
  command identity、revision、重连投影或 Steer 语义，不能作为新控制面的最终实现。
- [x] 已确认 Console Chat 具有 `TaskTracker.request_stop()`、Tool Coordinator
  cancellation 与 Runtime cancel-save，可停止当前生成、传播到工具并保存部分输出；
  但它们尚未通过统一 Kernel Port 暴露，`/stop` 还会隐式清空普通消息队列。
- [x] 已确认浏览器 `messageQueueStore` 当前承担持久化、排序、暂停、重试、
  跨标签所有权和后台发送；这是需要迁出的旧兼容实现，不是 OS 架构目标。
- [x] 已确认 Steer 尚无一等领域命令；Stop Gate 的
  `INTERRUPT_AND_CONTINUE` 是 Agent 内部循环决策，不能冒充用户 Steer。

实施 Checklist：

- [x] 冻结 `InvocationControlPort` 与服务端领域模型：`TurnSubmission`、
  `ControlCommand`、`QueueProjection`、`ControlReceipt`；所有对象携带 agent、
  conversation、invocation、command、correlation、idempotency 和 revision 身份。
- [x] 冻结单会话调度不变量：同一 Conversation 最多一个 active Invocation；
  不可变 `sequence` 只负责审计顺序，可变 `queue_position` 负责用户重排；priority
  只影响尚未开始的 admission，不能启动第二个并行消费者绕过当前 Invocation。
- [x] Lite 使用可恢复的本地持久化 Adapter 保存 queued/admitted/running/terminal
  状态与控制命令；`asyncio.Queue` 只作唤醒器，不作事实源。Workstation/Hub 只冻结
  等价 Port、lease 与 revision 语义，本阶段不实现远程队列。
  - [x] SQLite WAL Adapter 已建立独立 schema，原子分配 sequence/revision，持久化
    Submission 与 accepted Control Command，并支持进程重建后的 Queue 读取。
  - [x] Admission、运行态转换和命令应用回执已接入 Runtime Dispatcher；启动恢复会
    原子终止旧 generation 的 active Submission、结算 accepted command，再继续同
    Conversation 的 queued turn，且不会自动重放可能已产生副作用的运行中输入。
- [x] `enqueue` 支持幂等提交、查询、取消未运行项和受 revision 保护的重排；服务
  重启或页面关闭后仍可恢复，重复请求不得重复执行。
  - [x] 提交、查询、乐观 revision 和重建请求幂等回放已由 SQLite Adapter 实现。
  - [x] `cancel_queued` 与完整集合 `reorder` 已由 Application Service 在 SQLite
    命令事务中原子应用并返回 applied receipt；审计 `sequence` 不随重排改变。
- [x] `steer` 只针对 active Invocation，在明确的模型/工具迭代安全点应用，并写入
  有序事件；已完成的输出和工具结果不可改写。高风险副作用处于 prepared/running/
  uncertain 时必须等待安全点或返回冲突，不能用取消伪装为 Steer。
  - [x] 已冻结 `BEFORE/AFTER_REASONING` 与 `BEFORE/AFTER_TOOL_BATCH` 安全点、
    applied receipt 证据和 reasoning/tool/approval 竞态规则；详细设计见
    `docs/design/qwenpaw-invocation-control.md`。
  - [x] workspace 级 `InvocationControlService` 已绑定 live Invocation；
    AgentScope `RuntimeInteractionMiddleware` 承担 reasoning 前/后安全点。
    AgentScope 未暴露整批 tool admission middleware，因此仅保留一层极薄
    batch bridge 承担工具批次前/后原子失效。
  - [x] SQLite accepted Steer 到 live mailbox 的恢复型 Dispatcher 已接入；
    只有写入 Agent 上下文后才持久化 applied receipt 和实际
    safe point，Runtime 晚于命令绑定时可恢复投递。
  - [x] `BEFORE_TOOL_BATCH` 仅撤销尚未 admission 的完整工具批次；
    `AFTER_TOOL_BATCH` 只追加 Steer，不读取或重写已完成 Tool Result。审批暂停、
    运行中 Side Effect 与 uncertain 状态不会被 Steer 当作取消处理。
  - [x] Console 以服务端 `ChatSpec.id` 作为 Conversation identity，
    以 Message identity 作为 submission 幂等键；控制面领域模型不再包含
    `session_id`，该字段仅由渠道和旧历史模块自行兼容。
  - [x] `InvocationScope.conversation_id` 已冻结为可空的一等 `ChatSpec.id`；Chat
    Adapter 在装配时只写入一次，Memory Host、Interaction 和控制面从固定 Scope
    传播。没有 Conversation 的 transport 保持 `None`，禁止回退到 `session_id`。
    固定 Chat 执行 `/clear` 后通过真实 durable Submission 返回
    `CONVERSATION_SCOPE_OK`，Queue 回到 idle，消息级 Fork 入口正常呈现。
  - [x] invocation-bound `RuntimeInteractionBroker` 已冻结 Ask User 与
    Suggestion 的生产者 API；插件通过公开 `ToolHost` 获取，阻塞等待、非阻塞
    建议、超时与 Runtime 终止取消共用 workspace `InteractionService`。
  - [x] 内置 `suggest_user_action` 已与插件共用 Broker 和系统 Tool Provider；
    Runtime 终态只释放 blocking waiter，不撤回已持久化的 non-blocking Suggestion，
    Chat 权威投影可展示并关闭建议。
  - [x] 内置 `ask_user` 工具和 Chat Interaction Adapter 已贯通：服务端按
    `ChatSpec.id` 投递和校验响应，Console 只呈现 open projection；真实 Chat
    已验证选项回答后工具返回、reasoning 继续并形成最终消息。
  - [x] Kernel 已冻结内容最小化的 `WaitCondition` / `ContinuationRef` 契约；
    Lite 从 Interaction 权威表投影 Approval 与 Ask User 的等待、解决、过期和取消，
    重启后可查询且不复制 prompt、选项或回答；waiter / hook 的 attached 状态区分
    活跃与孤立 continuation。
  - [x] Task Approval Interaction 已关联真实 Ledger Checkpoint；丢失进程内 waiter
    后，决定先提交，并行 blocker 全部解除后以原始 run_id fencing 旧 Run，再通过
    稳定幂等键创建恢复 Run。恢复失败保留决定与 Checkpoint，并返回
    `task_continuation_failed`。
  - [x] 普通 Chat Ask User 已改为可序列化 Conversation Turn continuation：工具只
    保存 Interaction 并结束当前 Invocation；回答与 outbox 在同一事务提交，workspace
    worker 按稳定幂等键创建新 Submission，执行时再从 Interaction Store 装配回答。
    服务重启以及“入队成功、outbox 标记前崩溃”均不会重复创建 Submission；并行
    Ask User 会全部保留。固定 Chat 已验证原 Invocation 结束后交互卡仍保留，选择
    HTML 后自动创建 continuation 并只输出 `HTML`。
  - [x] Chat Runtime 已进入 queued -> admitted -> running -> terminal
    Submission 生命周期；如果存在更早 queued turn，当前 HTTP 输入
    不会被错配执行。
  - [x] reasoning 流使用 provider cooperative cancel；Interrupt 在取消 Runtime
    owner 前，先通过同一 cancellation root 事务撤销该 Invocation 的 blocking
    Approval/Ask User，再取消前台工具。终态清理保持幂等重试，non-blocking
    Suggestion 不随运行结束消失。
- [x] `interrupt` 已通过 invocation cancellation root 传播到 Runtime owner、前台
  Tool Coordinator（含 root-owned child-agent 工具）与 Interaction waiter，保存
  部分输出并提交唯一 `interrupted` 终态；显式 offload 后台任务不属于当前前台
  Invocation。
- [x] Provider 模型流具备有界 cooperative cancel；Codex/Qoder Harness 公开统一
  `cancel_turn()`，并在 OS Interrupt cancellation root 中精确中止活动 turn/session。
  外部 Harness 请求已进入 durable Submission 终态与 Interaction 收尾，真实外部
  进程的浏览器链路仍需验收。
- [x] 分离三个用户意图：`interrupt_current` 只打断当前 Invocation；
  `cancel_queued` 只撤销指定待执行项；`stop_and_clear` 必须显式组合，禁止沿用旧
  `/stop` 的隐式清队列行为。
  `stop_and_clear` 在同一事务中取消 queued Submission，并捕获当时的
  `invocation_id` 后复用 Runtime cancellation tree，晚绑定不会误伤后续运行。
- [x] Chat HTTP/SSE 仅作为 Control Plane Adapter，按 `ChatSpec.id` 返回权威 Queue
  projection、open Interaction 与 receipt/revision；过期 revision、跨 Chat target
  和幂等冲突由服务端关闭失败。SSE 使用完整快照 cursor 做 current-state recovery，
  不冒充不可丢审计日志。
- [ ] 前端迁移为薄投影：移除本地发送者、跨标签锁和本地权威 runState；允许保留
  未提交输入草稿，但提交后的 Queue、Steer、Interrupt 状态只读服务端。
  - [x] 前置门禁：workspace-owned Submission Dispatcher 原子持久化版本化输入
    envelope，并能在无浏览器 subscriber 和服务重启后消费 queued Submission；
    generation 丢失的 active Submission 会先安全进入 interrupted，`TaskTracker` 只
    保留单次 SSE 回放兼容，不再拥有排队顺序。
  - [x] 已分配 `ChatSpec.id` 的 QwenPaw Chat 在运行中或多标签场景直接提交完整
    durable envelope，不再经过浏览器 Web Lock/localStorage admission；Queue 面板
    订阅服务端 Runtime Projection，并以权威 revision 执行 cancel/reorder。
  - [x] QwenPaw 首轮发送先通过现有 Session singleflight 分配真实
    `ChatSpec.id`，再提交 durable envelope；普通 Enter 和空白页 Ctrl/Command+Enter
    均不再创建 `agent:new` 本地 Queue。页面切换后的旧草稿失败关闭，遗留本地草稿
    Queue 只保留兼容读取，不再承担新 admission；已有 Chat 在提交期间切换页面时
    继续按不可变 route 进入原 Conversation 的服务端 Queue。
  - [x] QwenPaw backend 从首轮分配开始不再展示、调度或后台排空遗留
    localStorage Queue；旧记录按 `agentId` 解析 backend，QwenPaw 与未知 Agent 均
    fail closed。`runState`、Web Lock 和 background sender 仅保留给外部 backend
    兼容队列，不能因 Chat ID 映射尚未完成而短暂接管 QwenPaw admission。
  - [ ] 外部 backend 仍使用本地兼容队列；待其公开 Conversation/Queue capability
    明确后删除跨标签发送状态机。
- [x] Console 与 Channel `/stop` 已按 `ChatSpec.id` 优先使用统一 Interrupt，旧路径
  仅作无 binding 回退，且不再隐式清 Queue；现有前端发送队列仍需逐步切到同一
  Port。迁移完成前保持旧路径可用，但不得让新旧消费者同时执行同一 submission。

验收：固定 Chat 在两个浏览器标签中连续排队仍只执行一次且顺序一致；关闭页面和
重启服务后待执行项可恢复；运行中 Steer 在下一个安全点生效且不丢失既有输出；
Interrupt 能终止模型、工具与子运行，保存部分消息、解除审批等待并产生唯一终态；
三个控制命令均通过幂等、竞态、重连、崩溃恢复和跨平台定点测试。浏览器存储全部
清空后，服务端 Queue 与运行控制状态仍保持正确。

### I3C：Conversation Execution Chain 与分层恢复

定位：用持续执行链替代内核中的隐含“一问一答”假设，同时保留 Chat 的简单交互
体验。该模块属于 Chat-first Runtime 基建，不要求提前开发 Task 页面。

- [x] 冻结持续执行身份：`ChatSpec.id` 是 Conversation，`Submission.id` 是一次
  输入，`Invocation.id` 是一次运行尝试，`correlation_id` 贯穿同一长程意图；
  禁止用 assistant message 或 `session_id` 推断生命周期。
  - [x] Chat Runtime 由权威 Submission 与 Interaction 按 correlation 派生有界
    `ConversationExecutionChain`；同一意图跨 Invocation 保留完整因果身份。一次
    Submission `succeeded` 后仅进入 `inactive`，绝不伪装业务 `completed`；阻塞
    Interaction 显式投影为 `waiting_user`，续行后恢复 running。SSE 使用有界历史窗口，
    `execution_window_truncated` 明示窗口是否截断，不在每次轮询中全表重建。
- [x] 完成 Conversation Activity 只读投影；它从 Submission、Invocation、
  ModelCall、Action、Interaction、Artifact、Evidence 与 Verification 权威事实
  派生，不复制第二套状态，也不把恢复状态伪装成 Queue 项。
  - [x] Chat Runtime snapshot/SSE 已嵌入有界 `ObservationPage`，复用既有
    ModelCall、Action、Interaction、Control、Compaction 与 Verification 派生器；
    Activity 内容参与 v2 cursor，来源仍是权威 Store，未新增事实表。真实固定 Chat
    返回 50 条最近活动，覆盖 model/action/guardrail/hitl/control 五类事实。
  - [x] Invocation Control 已公开只读 `SubmissionHistoryPort`；Submission accepted
    派生不可变 intent，成功、失败、中断或取消后派生不可变 terminal evidence，保留
    `submission_id + invocation_id + correlation_id`。活动中的 queued/running 仍由
    Queue snapshot 表达；旧 Store 没有逐跳事实，因此不伪造状态转换历史。
  - [x] Chat 上传与内置/插件工具产物共用只读
    `ConversationArtifactHistoryPort`；Artifact/Evidence 分别投影独立 category，来源
    回指同一 ownership record。工具产物新记录保留 invocation、correlation 和固定
    generation；Activity 不暴露 URI、文件名、metadata 或 Evidence claim。固定真实
    Chat 的完整 Observation 分页恢复出 6 个 Artifact 和 6 个 Evidence 权威投影。
  - [x] Task-owned `ArtifactRecord/EvidenceRecord` 已通过统一
    `TaskResultHistoryPort` 接入 Observation；一次 Ledger 回放同时返回 Artifact、
    Evidence 与 Verification，并保留 Task/Run/Event/Step/Correlation 因果。若同一
    结果已有 Chat ownership receipt，Activity 以 Task 因果记录为准并显式去重；
    URI、文件名、metadata 与 Evidence claim 均不进入投影。在源 Store 提供
    revision/notifier 前，不提高全量重建频率，也不以轮询冒充日志。
  - [x] Lite Observation index 会按 owner 与当前权威派生集做 reconciliation；版本
    演进后遗留的陈旧指针被删除，旧 cursor 明确失效，不再让 Activity GET/SSE 500。
  - [x] Model Recovery 通过只读 `ModelRecoveryHistoryPort` 接入 Activity；Resource
    Wait 与部分流 Model Step 分别从权威恢复事实派生状态，不复制 Provider payload、
    Prompt、异常正文或 partial output，也不把等待伪装为 Queue 项。
  - [x] Console 已增加薄 Activity 投影；它订阅现有 Chat Runtime snapshot/SSE，按
    最新 `correlation_id` 展示 Action、Interaction、控制、恢复、Artifact、Evidence、
    Verification 与 Outcome 的语义状态和来源引用，不暴露隐藏推理或事实 payload，
    也不复制 Task Workbench 状态机。普通 model 路由/结果与
    `qwenpaw.control.submission` 生命周期被过滤，短问答仍只显示消息；展开/收起仅是
    本地展示状态，不参与 Runtime 判定。固定真实 Chat 浏览器验收已确认普通问答的
    Activity 计数为 0。
- [x] 完成 Chat Ask User durable continuation：响应决定与 continuation outbox
  可原子提交或幂等恢复；新 Submission 继承原 correlation 并创建新 Invocation，
  不恢复旧协程。Lite worker 启动即扫描未派发项，HTTP 只负责唤醒；Queue envelope
  仅保存 Interaction 引用，模型输入在执行时从权威 Store 装配。
- [x] Human Interaction Policy 已冻结为 Kernel `UserInputReason` 与统一 admission
  gate：所有新 blocking Ask User 必须声明 `missing_required_fact`、
  `material_preference`、`scope_authorization` 或 `high_impact_decision` 之一，未分类
  请求在持久化前拒绝；历史记录仍可兼容读取。内置系统 Tool、插件 SDK 与 PawApp
  兼容桥共用这一契约，原因进入内容安全 Observation。默认仍由目标、计划、事件、
  Action、Artifact、Evidence 和 Verification 持续推进；Suggestion、Steer、
  Interrupt 与 Approval 保持独立语义，禁止用逐步追问或“是否继续”充当执行调度器。
  UI 中的一问一答只是上述权威事件的 Conversation 投影；Kernel 不以 assistant
  message 结束作为运行终态，也不要求用户逐轮发送消息才能推进长程意图。
- [x] 冻结 Model Recovery Contract：
  - [x] `ModelRecoveryDecision` 已提升为 Kernel 稳定领域模型，冻结完整
    failure/disposition 允许矩阵与 Retry-After 约束；Provider 分类器和
    `ModelCallResult` 共用同一验证入口，Provider 不能自行组合未经许可的恢复动作。
    历史记录缺少恢复字段时继续兼容读取，但新记录一旦声明恢复事实就必须满足矩阵。
  - [x] Kernel `ModelFailureClass` 已区分 `transport_unavailable`、
    `stream_interrupted`、Provider overload、
    rate limit、quota、budget、auth、policy、context overflow、user interrupt 与
    unknown；旧 `ModelCallResult` 缺省字段仍可读取。
  - [x] Kernel `ModelRecoveryDisposition` 已区分 `retry_transport`、
    `continue_model_step`、`wait_resource`、`fail_terminal`、
    `reconcile_side_effect` 与 `stop_interrupted`；失败类别与处置必须成对出现，产生
    内容后的尝试禁止标记为整请求 transport replay。
  - [x] Token/Model Call 审计已把连接前传输失败、部分流中断和用户取消分别投影为
    `retry_transport`、`continue_model_step` 与 `stop_interrupted`；短时 rate limit 和
    quota exhausted 已分开，恢复字段进入内容安全 Activity。
  - [x] `ModelOutputBoundary` 已区分 pre-output、完整响应、部分流、终态流和
    incomplete EOF；没有终态 chunk 的 EOF 不再误记成功。无内容时允许 transport
    retry，已有内容时禁止整请求重放。
  - [x] transport retry 使用 Retry wrapper 的独立 attempt 预算与退避；logical call
    和 fallback 耗尽后才转 durable timer Wait。Transport、provider overload 与
    rate limit 共用同 correlation 的自动 timer cycle budget，耗尽后持久化
    `recovery_exhausted`，不无限循环；
  - [x] `wait_resource` 保存独立 Resource Wait，timer/external event 成熟后由
    durable outbox 创建同 correlation 的新 Submission / Invocation，不占用旧槽位。
    Resource Wait 的 waiting/ready/dispatched/cancelled/exhausted 已进入统一 Chat
    Activity，不再依赖 `/wait-conditions` 单一视图或前端错误字符串。
  - [x] Provider `Retry-After` 秒数或 HTTP-date 经统一策略归一化为有限、非负 hint，
    写入 Model Call evidence 并决定 Resource Wait `not_before`；原始 header 不入 Kernel。
  - [x] Interrupt 以来源 Invocation、Stop-and-Clear 以 Conversation 建立恢复栅栏；
    dispatcher 用 Queue revision 关闭控制检查与恢复 enqueue 之间的竞态，执行前反向
    校验 Wait 与 Submission 绑定，取消后的旧任务不能被 timer 或资源事件复活。
  - [x] 受控 Harness 四方 admission 后写入 `HarnessStepContinuation` outbox；共享
    dispatcher 以原 correlation 创建新 Submission / Invocation，并用 Stop / Interrupt、
    来源后的新输入、Queue revision、执行前反向绑定、backend 一致性和 2-cycle 预算
    fencing 恢复。
- [x] 持久化 bounded stream outcome：产生部分输出后断流时，不盲目重放完整
  Turn，不把 partial assistant message 当完成；后续 Model Step 从 durable context
  重建。内容安全 continuation 使用稳定 outbox、新 Submission / Invocation、原
  correlation 与 2-cycle budget；Stop / Interrupt、crash-after-enqueue 和启动恢复均
  已建立 fencing / 幂等边界，状态进入现有 Chat Runtime Activity。
  - [x] Model Call 在恢复边界落库后产生 typed Runtime signal；错误保存不注入
    Envelope partial blocks，下一 Invocation 从最后一个完整 session 边界重建。用户
    主动取消仍保存已展示 partial，两种终态不再共用错误保存语义。
- [x] 以 Action Plane 完成副作用恢复：已成功 Action 不重做，failed 服从重试
  policy，uncertain 必须先对账或取得显式授权。只有具备 provider-neutral committed
  action identity 的 Adapter 才能开启流内工具执行。
  - [x] 过渡安全门：来源 Invocation 只要存在持久化 `ActionRequest`，Model Step
    continuation 即进入 `action_reconciliation_required`，不自动重放任何 Action。
  - [x] dispatcher 已按真实 `ActionRecord` 生成并持久化内容安全的 reconciliation
    assessment：区分 pending result、uncertain side effect 和 durable context
    required；Activity 只公开原因与计数，旧 SQLite 数据库原位迁移。
  - [x] terminal/certain Action 已通过不可变私有 Agent context snapshot 与内容安全
    `ModelStepContextCheckpoint` 自动续行；dispatcher 逐一校验 provider-neutral
    `CommittedActionItem`。同步 ToolResult 与同 Invocation 内进入上下文的后台 hint
    使用相同 binding；dispatcher 的 snapshot-first 崩溃修复也复用同一验证器，不会在
    重启后退回旧的 call-ID-only 证明。
  - [x] 受控 Harness Provider 已在 ActionResult 持久化后，由 Session Bridge 把对应
    tool output 与 `CommittedActionItem` 一起原子写入 Chat context；仅有远端完成事件
    或 provider history hydrate 时不生成 binding。若 Action 已成功但 context commit
    失败，Invocation 标记失败并保留成功 Action 证据，禁止自动重做。
  - [x] Harness 断流 admission 已冻结 `HarnessRecoveryContextCheckpoint`：provider
    context identity、provider history、Chat session binding、ActionStore digest 四方
    一致才以 `0600` 不可变文件落盘；provider context digest 绑定 Invocation 与来源
    Submission，原始 ID 不持久化且不能跨任务关联。
  - [x] 共享 dispatcher 消费 Harness checkpoint 创建 fenced continuation；后台
    Action 在结果 digest 确定后先准备未发布私有 snapshot，结果提交成功后再发布
    durable outbox，来源成功且未被用户输入或控制命令覆盖时跨 Invocation 主动续行。
    并行后台结果以 binding 去重并合并最新 Session，不依赖用户发送“继续”；启动修复
    只有在 ActionStore 精确匹配 action、invocation 与 observation digest 时才发布
    孤立 snapshot。
  - [x] uncertain Action 已接入统一 Chat Interaction：恢复 worker 以稳定
    `continuation_id` 创建 blocking Approval，绑定精确的终态 Action evidence digest；
    用户只能选择“已核实安全，重试一次”或“停止恢复”。授权引用随 model-step
    continuation 持久化，审批落库后即使进程重启也会创建同 correlation 的新
    Submission / Invocation；执行前再次校验 Interaction revision 和 Action digest，
    来源 Invocation 之后出现新 Submission 或证据变化均失败关闭。拒绝或停止会取消
    continuation，不改写原 ActionResult，不恢复旧协程，也不复用 Task 私有 retry
    API。
  - [x] Action retry 裁决已冻结为 Host-owned `qwenpaw.action-retry.v1`：Provider、
    Driver 或插件只能提交 `provider_retryable` hint，不能决定重放。只有无副作用、明确
    `failed` 且带瞬时失败 hint 的 Action 才允许以新 Action 重试；写入、进程和外部
    调用的 ERROR 默认进入 `uncertain -> reconcile_required`，不会因错误返回而推断
    “肯定未执行”。裁决随 `ActionResult` 持久化并进入统一 Observation；原 Action
    保持不可变。自动 dispatcher 仍须等待 executor 级幂等能力声明和 attempt lineage，
    因此本阶段不把 `retryable` 误报成“已自动重试”。
  - [x] Executor 幂等和 retry lineage 已进入稳定 Action 契约：
    `undeclared`、`host_guarded`、`executor_enforced` 明确区分 key 的真实执行边界；
    `ToolDefinition`、`DriverToolDefinition`、插件 SDK 和 Legacy Driver metadata 共用
    同一枚举。执行器通过 `current_action_execution()` 或 Driver request context 取得
    Action ID、稳定 key、attempt 与 lineage。重试创建新的 Action，记录 root/previous
    Action，并同时用显式 `executor_item_id` 维持模型上下文绑定，不再从幂等 key 猜
    Tool call ID。Host 会校验参数摘要、correlation、固定 generation 和当前环境契约；
    只有 executor 持久防重承诺可以让 effectful uncertain failure 进入安全 retry
    admission。自动 dispatcher 尚未接入，不能把 admission 当作已执行。
  - [x] Task SideEffect Ledger 已降级为 Action attempt 的兼容投影：Action Recorder
    存在时，SideEffect record 使用 `action:<action_id>` 作为本地防重身份，并直接继承
    Action 的安全参数摘要、Invocation、correlation、Approval 和 Policy；不再从
    tool-call ID 与原始参数另算一套竞争性幂等事实。没有 Action Recorder 的旧 Task
    Runner 继续走原兼容路径。由此每个 retry attempt 都有独立本地记录，而真正跨
    attempt 的 executor key 仍只由 Action 契约持有。
  - [x] Host-owned `ActionRetryPolicy` 已把最大 attempt、初始延迟、指数退避和延迟
    上限固定到每个 ActionRequest；ActionResult 的裁决持久记录下一 attempt、预算和
    `retry_after_seconds`。后续 attempt 继承原策略，预算耗尽转为明确 forbidden，
    延迟未到抛出可调度的 typed error。Activity 只公开上述内容安全调度事实。
  - [x] Lite 在发布 retryable ActionResult 前保存 `0600` 私有执行输入 checkpoint；
    公开 Action 与 Activity 只携带 checkpoint ID、参数安全摘要和因果身份，不暴露
    原始参数。相同 Action/attempt 重放幂等，内容冲突失败关闭；写入失败会把裁决降级
    为 `retry_input_unavailable`，不会留下一个无法执行却声称可重试的公开结果。系统
    Tool、插件 Tool、Driver 与 Harness Recorder 共用该 Store Port 和 Lite Adapter。
  - [x] retryable ActionResult 成功提交后发布独立、内容安全的 durable outbox；状态
    明确区分 `waiting_delay / ready / dispatched / cancelled`，延迟成熟、取消和
    dispatch binding 均幂等。outbox 只保存 checkpoint 与 observation digest，不复制
    原始参数；发布失败不篡改已提交 Result。Chat dispatcher 启动时按 Agent 枚举私有
    checkpoint，并用 Action、Invocation、ChatSpec、correlation、generation、capability
    与参数摘要精确反查已提交 Result，只补建缺失 outbox，不执行工具；重复启动幂等。
  - [x] `stop-and-clear` 在权威 Control Command 提交后取消同一 ChatSpec 下所有
    `waiting_delay / ready` Action retry；取消状态持久且幂等，不依赖前端 Queue。
    启动 repair 补建 outbox 后会再次读取 Control Ledger：来源 Invocation 已被
    Interrupt，或 Action 请求后出现 Stop 时立即取消，关闭“控制提交—outbox 发布”
    的崩溃竞态。
  - [x] 新的原生用户 Submission 先持久入队，再取消同一 ChatSpec 的旧 Action retry；
    自动 Interaction、Model Recovery、Harness 与 Background continuation 使用独立
    envelope，不被误判为新意图。若在入队与取消之间崩溃，启动 fence 通过来源
    Invocation 的 Submission sequence 识别更晚用户输入并取消旧 continuation。
  - [x] 提取模型无关的 `GovernedActionExecutor`：显式经过当前 permission decision、
    ToolCoordinator supervision 和 retry-aware ActionRecorder；允许时创建新的 lineage
    attempt，拒绝时不创建 Action。执行器返回前反查 terminal Action evidence，不能因
    result processor 只记录 warning 而误报成功。Provider/generation 解析仍由后续
    Runtime Orchestrator admission 接入，恢复 worker 不持有工具函数。
  - [x] Action 与私有 retry checkpoint 同步冻结实际 `ToolSelection`（mode、skill、
    feature 与 subagent whitelist），并在新 attempt admission 校验一致性；避免热更新
    后按当前 Chat 配置重建出另一套工具。历史 checkpoint 没有选择快照时，后续自动
    dispatcher 必须 fail closed，不能猜测或补默认值。
  - [x] AgentBuilder 提供 exact-action resolver：只从已 pin 的 assembly 中打开指定
    Tool Provider，原样传入冻结的 `ToolSelection`，并要求工具名唯一命中；Provider
    不在选择内、实现身份不符、工具缺失或冲突均 fail closed，不回退到当前 generation。
  - [x] retry outbox 支持按原 `dispatch_id` 对崩溃中断的 Submission 受控回队，并
    持久化 `recovered_dispatch_id` 保证幂等；错误 dispatch 身份不能复活任务，重新
    dispatch 或取消时会清理旧恢复绑定。上层仍须核实 Submission 确为 interrupted。
  - [x] Workspace dispatcher 使用 Invocation Control 实际恢复出的 orphan 清单做
    authoritative reconciliation：严格校验 envelope、ChatSpec、Invocation、Action
    lineage、generation、Provider、ToolSelection 与参数哈希；已有 terminal Action
    不重放，无终态才按原 dispatch 身份回队，身份漂移和多 attempt 均 fail closed。
  - [x] Action 与 retry checkpoint 固定 Provider execution digest，覆盖经过 schema
    校验的 capability config 与 credential alias 映射，但不持久化配置正文、alias 或
    secret；重试前由当前 Host 重新计算并校验，配置漂移时 fail closed。历史 Action
    缺少 digest 时不得进入后续自动 dispatcher。
  - [x] ToolCoordinator 增加受监督的 execution admission gate：permission check 在
    ToolCallContext 建立、call id 可见之后且工具执行之前运行；拒绝时移除 in-flight
    entry，不调用工具、不运行 Action result processor。Action retry 的审批、拒绝和
    pending 关联因此复用现有治理与 Interaction 身份，不再发生无 call id 的游离审批；
    admission 等待同时监听 ToolCall cancel event，Stop/Interrupt 可在审批期间及时
    取消且不会留下后台 permission task。
  - [x] 增加执行侧 `ActionRetryExecutionAdmission`：每次真正执行前重新加载私有输入
    checkpoint 与源 Action，要求 ToolSelection/Provider digest 完整、checkpoint 全量
    相等、源 observation digest 未漂移且 retry decision 仍指向同一 next attempt；旧版
    或损坏证据均 fail closed，dispatcher 不直接信任 outbox 中的参数引用。
  - [x] admission 编译确定性的 `ActionRetryExecutionPlan`：以 continuation id 与 revision
    派生 Invocation/ToolCall 身份，直接使用 `ChatSpec.id` 作为内部会话身份，并只选择
    原 Tool Provider；generation、correlation、ToolSelection 与 Provider digest 均冻结，
    durable Submission 重放不会生成另一组运行身份或混入无关 Provider。
  - [x] `RuntimeActionRetryRunner` 已连接 execution plan、指定 generation 的短生命周期
    RuntimeAssembly、exact-action resolver 与 GovernedActionExecutor；执行前再次比较
    Host 当前 Provider digest，Recorder 复用同一 ActionStore/输入/outbox 并生成新
    attempt，完成后无条件释放 generation lease，全程不调用模型。
  - [x] 自动 retry dispatcher 已接入上述单一身份：READY outbox 先经过
    Stop/Interrupt 与新用户输入 fence，再以内容安全 envelope 幂等绑定 durable
    Submission；consumer 执行前重新做 evidence admission，并以确定性 Invocation
    取得 Runtime lease、打开 pinned generation assembly、执行 governed Action，最后
    写入 Submission 终态并唤醒下一轮 outbox。该路径没有模型或流式输出边界，attempt
    budget 已固化在 checkpoint；重复调度只复用同一 dispatch binding。DISPATCHED
    outbox 仅保留审计关联，执行状态以 Submission/Invocation/Action 为权威。
  - [x] Provider resource availability 已冻结为 `ModelResourceRecoveryPort`：新
    quota wait 固定 Provider/Model identity；同 Workspace 的真实成功调用或现有 live
    model probe 只释放精确匹配的 external-event wait，并唤醒 durable dispatcher。
    旧 wait 缺少 identity 时失败关闭；不同模型、timer wait、非 live probe 和重复
    signal 不改变状态。Lite probe 只广播给已加载 Workspace，不为 health event
    隐式启动 Agent；Hub 的跨节点 durable health event Adapter 仍属后续实现。
- [ ] 把 WebSocket 增量续传、sticky route 和 HTTP fallback 保持为 Provider
  Adapter capability；严格验证 response identity/prefix，失败时回退持久上下文重建，
  Kernel 不感知具体传输。
  - [x] Kernel 已冻结 `ModelTransportContract`、Host-keyed HMAC Resume Evidence 与统一
    fail-closed validator；Provider 在建模时按实际 Model 声明能力，Attempt 在网络前
    固化。cursor resume 必须同时验证 response identity 与 prefix，sticky route 和
    WebSocket → HTTP continuation 必须显式声明；任一缺证或不一致直接选择 durable
    context rebuild。历史 Attempt 缺省为 HTTP/non-resumable，保持兼容。
  - [x] Lite 当前所有内置 Provider 明确落入安全默认：部分流到达通用 Wrapper 时不
    信任异常或 metadata 猜测续传，Result 记录 `durable_context_rebuild` 及原因，继续
    使用既有 Model Step checkpoint/outbox；成功、pre-output retry 和用户 Interrupt
    不伪造 transport recovery。
  - [ ] 至少一个真实支持 cursor resume 的 Provider Adapter 仍需接入，并完成 response
    identity、prefix、sticky route、HTTP continuation 的故障注入及真实断流验收。
- [ ] 完成跨平台长程时间语义：
  - [x] 首次 Run 保存 wall-clock durable deadline，恢复/replay 继承同一 deadline；
    每次进入执行边界只换算一次剩余时长，再由 `asyncio.timeout` 使用事件循环的
    monotonic clock 计时。deadline 已过时不调用 Runner，历史无 deadline 的 Run 以
    首次 `started_at` 兼容补算。
  - [ ] 分别在 macOS、Linux、Windows 验证 suspend/sleep、系统时间跳变和进程重启
    语义；本地单元测试不替代三平台真实运行验证。

验收：浏览器断连不停止执行；连接前失败可安全重试；部分流断开不误判成功或盲目
重放；Action 成功后不会重复执行；Interaction 回答或资源恢复在崩溃窗口内恰好创建
一个 continuation；quota/budget/auth/policy 和 Interrupt 不进入网络恢复；长程意图
可跨多个 Invocation 保持同一 correlation，短问答仍走单 Invocation 快速路径；
已授权且无歧义的多步骤任务自动推进，不要求用户逐步确认。

### I4：存储、产物与验证基建收口

- [x] Execution Ledger Port 与 SQLite Lite Adapter 明确分层。
- [x] Artifact Store Port 与文件系统 Lite Adapter 明确分层。
- [x] Conversation、Artifact、Evidence 引用统一 ownership 与 integrity 校验。
- [x] 冻结 Conversation Fork 身份契约：子分支创建新 `ChatSpec.id`，
  以持久化 `source_message_id` 为包含式锚点；不继承 Queue、Invocation 或
  pending Interaction。详细设计见 `docs/design/qwenpaw-conversation-fork.md`。
- [x] Conversation Fork 已提升为 Kernel `ConversationForkPort` 与稳定 SDK 模型；
  Lite 通过 JSON Adapter 兼容旧 AgentState 存储，外部契约只使用
  `agent_id + ChatSpec.id + source_message_id`，HTTP 不再直接操作 Session snapshot。
- [x] 实现原子 Conversation snapshot store、Chat Fork HTTP 适配器和
  Console API Client；并发幂等、运行中冲突及失败回滚已通过定点测试。
- [x] Chat 持久化消息 Fork 操作已接入，两个消息对锚点的历史截断与新
  `ChatSpec.id` 导航已通过浏览器验收；领域服务已验证父子 Queue、Interaction
  隔离、OS Queue 运行中门禁和 Artifact/Evidence lineage 只读授权；父子并行
  Ask User、Steer/Interrupt、严格审批及继承附件预览均已通过真实浏览器验收。
- [x] Plugin/PawApp Host 新增 `getCurrentChatId()`，只返回已进入 Chat registry 的
  `ChatSpec.id`；`getCurrentSessionId()` 降为传输兼容入口。PawTask 默认携带当前
  Chat identity，也可显式传入 Fork 子 Chat，并由 Host 校验用户、渠道和 PawApp
  namespace，防止二次开发继续把 runtime `session_id` 当作 Conversation identity。
- [x] Artifact Registry 投影支持版本、状态、supersedes 与 Verification 引用。
- [x] Verification 依据 Acceptance 与 Evidence 形成独立、可追溯的判定。
- [x] Artifact、Evidence 与 Verification Registry 保留宿主可信的事件因果身份；
  HTTP 保留旧引用数组并以新增 registry 字段兼容扩展。
- [x] Result Package 由权威引用组装，不复制事实内容。
- [x] Renderer、下载、预览与大内容边界统一。
- [ ] 为 Workstation/Hub 冻结数据库与对象存储 Adapter contract。

验收：Chat 产物可刷新恢复、跨会话读取失败关闭、摘要和哈希可校验；Kernel 不
引用 SQLite 或文件路径实现。

### I5：运行外设迁移

- [x] Tool/MCP Provider 完成公开配置与选择契约。
  - [x] `tool.provider` 使用 Agent Profile 的显式多 Provider 选择；缺省时自动发现，
    空列表明确关闭，Invocation 固定 registry generation，热替换不漂移旧运行。
  - [x] `config_schema` 随 Descriptor 固定；Runtime 在调用 `list_tools()` 前校验
    JSON 与 64 KiB 边界。公开 `ToolHost` 只暴露 detached config、alias-scoped
    credential handle 和统一 Interaction Broker，不泄漏 Workspace、request context、
    governor 或凭据 Store；无效配置 fail closed，未绑定 alias 返回 `None`。
  - [x] 系统与插件工具统一转换为 `ToolDefinition` 并进入 Tool Guard，名称碰撞拒绝
    装配；内置 Workspace Tool 仅通过私有兼容 Host 访问旧实现。
  - [x] MCP 明确作为 Driver 的具体协议，经选中的 `driver.provider` 转换为
    `DriverToolDefinition`，复用同一治理、审批、凭据和 Invocation 生命周期；不再
    建立平行 MCP Tool Provider，避免一个服务被双重暴露和双重持有生命周期。
- [x] Memory Provider 完成 Session 生命周期与持久化边界：公开 `MemoryHost` 只提供
  schema 校验后的 detached config snapshot 与 `MemoryStateStore`，第三方不再取得
  Workspace 私有 backend。Lite SQLite Adapter 按 capability provider、Agent 或
  `ChatSpec.id` 隔离，绝不回退 `session_id`；写入/删除强制 revision CAS，单值限制
  256 KiB JSON。内置 Memory 通过非 SDK 兼容入口继续复用既有 backend，生命周期仍
  归 Workspace。重开恢复、并发冲突、跨 provider/owner 隔离、删除竞态、配置接线、
  generation 替换与示例 SDK 共 58 项相关定点测试通过，并通过全部文件级静态门禁。
- [x] Agent Profile 冻结 `CapabilitySelectionOverrides`：新 Invocation 从当前
  Profile 读取显式选择，多值 Slot 未覆盖时保持自动发现；空列表明确关闭，多值
  与系统 Provider 可组合。可选标量 Slot 通过 `disabled_optional_slots` 明确关闭，
  `null`/缺省保持自动选择。不存在或 Slot 不匹配的 Provider fail closed 并释放
  generation lease；运行中 Invocation 继续固定原 selection 与 generation。配置
  JSON 往返、插件 Driver 选择、自动 Tool 保留、关闭语义、错误释放和 Runtime
  Profile 接线共 40 项相关定点测试通过。
- [x] Driver Provider 冻结第三方类型化工具、配置与 policy hint 契约。
  - [x] 内置与插件统一返回框架无关 `DriverToolDefinition` 和带 ownership 的
    `PromptFragment`；AgentScope 转换集中在唯一 Adapter，发布 Toolkit 前校验
    provider/capability/tool/prompt 身份、重复项和数量/字节边界。调用选择继续固定在
    Invocation generation 的 `driver_provider_id`。
  - [x] 公开 `DriverHost.require_approval()` 复用内置 Driver Gate、durable Task
    Approval Bridge 和 blocking Interaction；批准后继续，拒绝/超时/持久化失败通过
    `DriverApprovalRejectedError` 关闭，Invocation 取消保持取消语义。公开 SDK 示例
    已验证 approve、deny、参数脱敏和统一 Interaction 投影。
  - [x] `config_schema` 随 CapabilityDescriptor 固定在 registry generation；
    Agent Profile 将非秘密 `capability_configs` 与 alias -> credential ref 分离保存。
    Runtime 在 `DriverProvider.open()` 前完成 JSON schema 与 64 KiB 边界校验，Host
    只返回 detached config snapshot 和当前 capability 自己的 alias-scoped
    `DriverCredentialHandle`。handle 不暴露 Store 或任意 ref，repr 不包含 ref/secret，
    缺失记录/字段通过公开 `DriverCredentialUnavailableError` fail closed。示例插件
    仅引用公开 SDK；配置、隔离、脱敏、无效配置不调用 provider 及 Profile 接线已纳入
    57 项相关定点测试，并通过 manifest 校验与全部文件级静态门禁。
  - [x] 真实固定 Chat 先执行 `/clear`，再经内置 Workspace Driver 的新定义管线
    构建 Agent 并完成 `DRIVER_CONTRACT_SMOKE -> DRIVER_OK`；服务端 Queue 回到
    idle，浏览器刷新后显示完成步骤、持久化回复和消息级 Fork 入口。
- [x] Harness Runner 统一 Native Agent、Codex、Qoder 输入输出与取消语义。
  - [x] Codex/Qoder 已共享 Harness Event 翻译、OS durable Submission、统一
    `cancel_turn()` 和 blocking Interaction 收尾。
  - [x] `harness.runner` Contribution 已与 `runner` 共用公开 `TaskRunner`、
    `RuntimeContext`、generation lease、监督执行和 `RunnerSignal` 持久化管线；公开
    SDK 示例已经通过真实 `TaskExecutionCoordinator` 执行。
  - [x] 内置 Codex/Qoder 分别发布为
    `qwenpaw.system.tasks.codex-harness` 与 `qwenpaw.system.tasks.qoder-harness`，
    Slot 均为 `harness.runner`。系统 Adapter 直接消费同一 `TaskOrder`、`Run` 和
    `RuntimeContext`，把 Harness reasoning/tool/text/terminal 事件投影为统一
    `RunnerSignal`，最终回复通过同一 Artifact/Evidence Emitter 产出。
  - [x] Task Harness 不创建第二个 Chat Submission/Queue lease；取消由 Task
    Supervisor 的 cancellation root 传播到精确 provider turn，Codex/Qoder 的
    `cancel_turn()` 仍是唯一外部进程中止入口。错误、缺少 terminal、无 tool identity
    和 terminal 后继续输出全部 fail closed。
  - [x] 运行中的本地服务已热发布 1.3.0 generation，`/api/tasks/capabilities` 可发现
    两个 system Harness Runner；系统 Runner 经 `execute_selected()` 的真实 Ledger
    管线完成 conversation、reasoning、tool、Artifact/Evidence 与 `run.completed`。
  - [ ] 外部 provider 成功链路仍需环境验收：当前 Qoder runtime 已安装但未认证；
    唯一 Codex 可执行文件位于 ChatGPT.app 内，按隔离策略被判定为 embedded runtime，
    不作为 QwenPaw 子进程启动。取得已认证 Qoder 或独立 Codex CLI 后再做真实进程
    成功、审批与取消演示；当前不伪造通过状态。
- [x] Coding、Goal、Mission Strategy 完成安全激活与会话生命周期适配。
  - [x] `planner` 与 `strategy` 已补齐 system/plugin 共享行为合同：示例插件仅从
    `qwenpaw.plugins.sdk` 导入 `PlanStep`、`TaskOrder`、`RuntimeContext` 与
    `JsonObject`，经 manifest → generation → `TaskRuntimeOrchestrator` 真实生成
    Plan、固定 Strategy 和 Runner，并产出 Artifact/Evidence。Strategy 参数经过
    JSON/32 KiB 门禁后进入 Runner 的不可变 Context，插件不能旁路宿主执行管线。
  - [x] Default、Coding、Goal、Mission 均发布为 `strategy` Contribution；参数先
    进入不可变 `RuntimeStrategyDirective` 并受 JSON/32 KiB 边界约束。Console
    兼容 Runner 只桥接精确匹配的系统指令：Coding 仅修改本次 Invocation 的配置
    副本，Goal/Mission 只通过固定 `/goal`、`/mission` Command 激活。
  - [x] Agent Mode 与 Task Strategy 保持分层：Mode Session、Command、Prompt、
    Tool 和 Stop Gate 来自同一 Chat generation；`/clear`、`/new` 通过当前
    invocation 固定的 `AgentModeSession.reset_conversation()` 清理会话态，不改写
    Workspace 配置，也不会清理其他 Conversation 的 Goal 状态。
  - [x] Task resume 默认继承上一 Run 的 `runner_id + strategy_id`；显式
    `RuntimeLaunchConfig.strategy_id` 才能替换 Strategy。新 attempt 从新固定的
    generation 重新解析该 ID，缺失时 fail closed，不复用旧实现，也不静默降级到
    Default。相关 Runtime、系统 Contribution、Directive、Mode 与 `/clear` 生命周期
    94 项定点测试及所改文件静态门禁已通过。Resume 并发幂等和完成后重放另有
    Orchestrator 回归测试，确保同一 Run 不会被二次执行。
  - [x] 真实 Runtime 验收使用 generation 11 的系统 Contribution：Coding Task
    `da282534-fc0c-4dda-867c-bdfe086613d0` 零工具返回
    `CODING_STRATEGY_OK`；Goal Task
    `e5742b64-bd5c-4eae-a211-914f2f2c4eba` 实际调用 `update_goal` 并在第 1
    次迭代完成；Mission Task `daef6d8c-a21a-48cc-9ce7-b01bfbd5889a`
    以 `--max-iterations 1` 返回 `MISSION_STRATEGY_OK`。三者均通过同一 Console
    Runner、Conversation、Artifact/Evidence 与 Run Projection 管线完成，无待审批。
    固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 另完成 Goal 激活、
    `update_goal(complete)`、`/clear` 和普通消息 `POST_CLEAR_OK` 复验；清理后 Loop
    回到 Default、上下文为 0.0%，未继承 Goal 会话态。

验收：系统实现和示例插件分别通过相同契约套件；卸载新插件不影响已固定的旧
Invocation，新 Invocation 自动使用新 generation。

### I6：Scheduler、入口与 Delivery 兼容迁移

- [x] 冻结 Scheduler Port、Trigger、Lease、Retry 与 Idempotency 契约。
  - [x] system 与 plugin Scheduler 通过同一真实调度行为合同：Fire 先固定
    registry generation，再以 CAS lease 创建唯一 `TaskSource.SCHEDULE` Task，
    同一 generation 解析 Planner/Strategy/Runner，完成 Artifact/Evidence 后对重复
    Fire 幂等 replay。新 Store 实例可读取相同 definition，证明不是内存假象。
  - [x] 新增纯 Kernel `ScheduleTrigger / ScheduleDefinition / ScheduleFire /
    ScheduleLease`，严格区分 cron/once/interval payload，并以
    `owner_id + revision + expires_at` 冻结 claim/renew/terminal 所有权。
    `ScheduleWorkKind` 进一步区分 task/service/delivery；Task Dispatcher 对非 Task
    失败关闭。cron jitter 使用稳定 seed 确定性计算，重启不重新抽样。
  - [x] `SchedulerPort` 公开 upsert/remove/list、claim、renew、complete、fail 和
    recover-expired；复用现有 `ExecutionContract` 与 `RetryPolicy`，不在调度层复制
    Budget、Approval、Artifact 或 Delivery 状态机。
  - [x] 公开 fail-closed、process 生命周期 `scheduler` Slot，并从
    `qwenpaw.plugins.sdk` 导出所有契约。详细设计见
    `docs/design/qwenpaw-scheduler-contract.md`。
  - [x] Lite `SQLiteSchedulerStore` 已实现 Definition 持久化、并发幂等 claim、
    owner/revision CAS、renew、complete、fail 与 expired recovery；移除 Definition
    不删除 Fire 历史，重复键不同 Fire 内容 fail closed。Scheduler 身份已收口为
    `agent_id + schedule_id`，Fire 幂等域为
    `agent_id + schedule_id + idempotency_key`；SQLite 支持旧单 Agent 表事务迁移，
    两个 Agent 的同名计划通过隔离验证。Kernel、SDK、Store 与架构边界共 19 项
    定点测试及所改文件静态门禁通过。
  - [x] `qwenpaw.system.tasks.local-durable-scheduler` 已通过全局 Registry 发布；默认
    绑定应用级 `WORKING_DIR/scheduler.db`，不会闭包捕获首个 Workspace。插件
    `scheduler` Slot 也已加入同一 `SchedulerPort` 激活门禁，缺失方法时 generation
    发布前 fail closed。Scheduler、系统装配、插件契约与依赖边界共 34 项定点测试
    通过。
- [x] 建立 `ScheduledTaskDispatcher`：Fire claim 后以确定性 Task 幂等键创建
  `TaskSource.SCHEDULE`，绑定 Planner/Runner/Strategy，并强制 Task Runtime 使用
  Fire 创建时固定的 registry generation；并发 Worker 对同一逻辑 Fire 只会启动
  一个 Task。Task 创建后的启动失败保留可恢复 Task 事实，不伪造 Scheduler 失败为
  “从未创建”。
  - [x] terminal Fire 回放会根据持久化 `task_id` 恢复尚处于 created/planned 的
    绑定 Task；恢复仍使用 Fire 固定 generation。并发恢复通过 Ledger sequence、
    Task version 与状态转换 CAS 选出唯一 Run，输家只回放胜者事实；回放异常不会
    反向把已 completed 的 Scheduler lease 错写为 failed。
- [ ] Cron/Heartbeat 通过 Scheduler Port 创建或恢复执行，不直连旧 Runtime。
  - [x] 已迁移 Cron 的创建、更新、暂停、恢复、删除与 Workspace 启动恢复会在首次
    Fire 之前同步 Host-owned Scheduler catalog；降级到 legacy path 会删除旧定义，
    防止双重事实。JSON、APScheduler 和 catalog 任一步失败会恢复前一声明；启动恢复
    失败则禁用并移除唤醒器。该 catalog 切片完成时 APScheduler 仍负责唤醒；后续
    final/silent cutover 见下方独立完成项。Scheduler SQLite、system/plugin 合同、
    Cron/Heartbeat 与 Manager 当时共 79 项定点测试通过。
  - [x] Kernel 已冻结 Host-only `ScheduleTriggerCursor` / Store Port；Lite SQLite
    保存 definition hash、next/last occurrence 与 revision CAS，支持 due 有界查询、
    定义变更 reset、删除和跨 Agent 隔离。cron/once/interval 使用无状态 evaluator；
    once 保留过期点交给 misfire 判定，interval 首次注册对齐到不早于当前时间的周期，
    避免迁移时补跑全部历史。Cursor 已随 durable catalog 同步。
  - [x] durable trigger worker 基础设施已实现：按 Agent 有界消费 due Cursor；
    definition hash 变化先 reconcile；超过 misfire grace 跳到首个未来周期；handler
    异常或显式 `retry` 不推进，显式/默认 `handled` 才以 revision CAS 提交。Worker
    通过 generation-pinned Scheduler Provider 读取 catalog，不旁路统一 Capability
    契约。孤儿清理同样使用 hash + revision 条件删除；并发 Worker 只有一个进度赢家，
    业务副作用继续由 Fire Lease 幂等保护。不同 Schedule 同批并发消费，单个 Cron
    继续由既有 semaphore 限流。
  - [x] `CronManager` 已启动独立 durable trigger polling lifecycle；判定为 migrated
    的 final/silent Agent Cron 同步 catalog 后不再注册 APScheduler job，暂停、恢复、
    更新、删除与重启均复用 Cursor。handler 明确区分“失败事实已记账后 handled”与
    “Scheduler/记账暂态故障 retry”，misfire 写回既有 Cron history，next-run 从
    Cursor outcome 投影。Heartbeat 也已通过同一 worker 路由完整
    `ScheduleDefinition`，启动/热更新同步 definition + cursor，禁用同时移除两者，
    运行时重新解析 HEARTBEAT.md、active hours 与 last dispatch；迁移成功后不再注册
    APScheduler，并通过新 Runtime 实例恢复旧 Cursor。workspace service Cron 也已
    迁入同一 catalog/cursor worker：回调不伪装成 Task，每次 occurrence 先 claim
    可续租 Fire Lease，成功写互斥 `completion_ref`，异常或进程过期保留失败事实且
    不重放不确定副作用；callback registry 在重启时从声明重建。text-only 与 stream
    job 暂时继续走 APScheduler，因此父项保持未完成。
  - [x] APScheduler Trigger Adapter 为已迁移 Cron 与 Heartbeat 保存真实
    `scheduled_for` 并生成稳定 Fire 幂等键；手动触发使用独立操作键，不与定时槽
    竞争。尚未迁移的 Cron 类型继续走显式兼容路径。
  - [ ] 先实现 `Task Event → Delivery Projection → Channel Adapter`，再迁移
    `dispatch.mode=stream/final`；迁移前旧 Agent Cron 继续作为显式兼容路径，避免
    统一执行后丢失外部频道实时回复。
    - [x] final/silent 已迁移；stream 已冻结 completed Reply 与 Tool Activity 公共
      事件：Console/Harness 在终态提交 `conversation.assistant.completed`，Projector
      不外发 reasoning/token delta，Channel Adapter 输出 completed function call/
      output Message，工具参数与结果预览有 8 KiB 上限。
    - [x] image/audio/video/file 内联字节先进入内容寻址 Artifact Store，Ledger
      只保存有序 descriptor 与 ArtifactRef；Adapter 校验 Task 事件所有权和内容哈希
      后恢复 Channel Message。嵌入媒体与 `task-result.md` 不产生重复通知。
    - [x] 持久化 Ledger → Worker → generation Registry → System Adapter → 实际
      ConsoleChannel 串联通过；修复 Adapter Message 缺少 `object="message"` 导致
      Channel 静默丢弃但 Receipt 误报 delivered，并验证后台 Cron 不写 Console push。
    - [ ] 完成浏览器与至少一个外部媒体 Channel 的等价验收后，才允许
      `LiteCronTaskRuntime.supports(stream)`；当前仍显式回退旧 Executor。
  - [x] Heartbeat 默认复用同一 Dispatcher；`HEARTBEAT_OK` 由
    `DeliveryPolicy.suppress_exact_text` 表达为不生成投影，Task 结果仍完整留在 Ledger。
    `target=last` 使用 Channel Adapter，`target=inbox` 使用 system Inbox Adapter，
    `target=main` 不请求 Delivery；Scheduler 和 Runner 不复制 Inbox 状态机。
- [x] Chat、Channel、Schedule 共享 Invocation 与 Artifact/Evidence 引用。
  - [x] 每个 Run attempt 持久化独立 `invocation_id`，恢复 attempt 创建新
    Invocation 但保留原根 `correlation_id`；`RuntimeContext` 将两者交给
    system/plugin/Harness Runner。Console 只在存在内部 Task Approval Broker
    时允许固定 Runtime Assembly 身份，外部 payload 伪造无效。
  - [x] Execution Event、DeliveryRequest、Channel Message metadata 和 InboxItem
    只读传递相同 Invocation/Correlation；ArtifactRef/EvidenceRef 仍由已提交
    事件投影，Channel/Inbox 不复制事实源。
- [x] Reply、Result、Approval、Exception 与 Artifact Ready 进入统一 Delivery
  Projection；普通 Timeline 事件不进入 Inbox。
  - [x] 冻结 `DeliveryRequest / DeliveryDestination / DeliveryReceipt` 与
    `DeliveryAdapter`，核心契约不含 `session_id`；新增 public
    `delivery.adapter` Slot，并在 generation 发布前校验 Protocol 与 namespaced
    identity。详细设计见 `docs/design/qwenpaw-delivery-contract.md`。
  - [x] Lite SQLite Projection 以确定性 Delivery ID、显式 attempt、owner/revision
    CAS 和 lease expiry 提供并发幂等；只有明确 failed 才允许下一 attempt，过期、
    Adapter 异常或无效 Receipt 均 fail closed 为 uncertain。Dispatcher 固定 Request
    generation 并校验 Adapter/Receipt identity。Delivery Kernel、Store、Dispatcher、
    Slot 与插件门禁共 24 项定点测试通过。
  - [x] Task Event Projector 只从已提交 Ledger Event 派生 Reply、Result、Approval、
    Activity、Exception 与 Artifact Ready；system Channel Adapter 通过 opaque address 兼容旧
    transport 参数，并输出现有 Channel 可消费的 completed Message。Task Delivery
    Worker 支持 cursor replay 到终态，普通 Timeline 不产生外部投递。
    Artifact-only 插件 Runner 没有 Conversation delta 时，Result 使用稳定非空完成
    文本并保留 Artifact/Evidence 引用；`suppress_empty_text` 仍可明确保持静默。
    Adapter 超时只写 Delivery uncertain receipt，不改写 Task/Run/Event 源事实。
  - [x] system Inbox Adapter 与示例
    `delivery-provider.local-jsonl` 通过同一真实 Worker 行为合同：从已提交
    Task Event 投影 Request，按 Request 固定 generation 解析 Adapter，持久化
    Receipt 后重放不再执行副作用。示例插件仅依赖公共 SDK，并通过
    production Loader 的热安装/卸载验证；旧 generation lease 在卸载后仍可完成
    已固定的投递。
  - [x] Inbox Projection 接线，以及 Scheduled Task 的正式 `ChatSpec.id`
    Conversation 绑定；禁止为了迁移 Cron 把 `session_id/share_session` 提升为新领域
    身份。
    - [x] `ScheduleDefinition → RuntimeLaunchConfig → RuntimeContext →
      os_conversation_id` 已支持可选 `ChatSpec.id` 透传；未绑定 Chat 的后台 Task
      保持 `None`，不会用 Task ID 或兼容 `session_id` 伪造 Conversation。
    - [x] 宿主 Trigger Adapter 创建或解析 Chat，并在写入 Definition 前校验 Agent
      ownership；旧 `session_id/share_session` 只留在兼容 Adapter。
    - [x] 旧 `CronExecutor` 注册/复用 `ChatSpec` 后，把真实 `ChatSpec.id` 注入
      `os_conversation_id`；Runtime Assembly 不再只看到 Cron `session_id`。无法注册
      Chat 的旧安装仍保持显式兼容路径，正式 Scheduler Adapter 将改为 fail closed。
    - [x] 新增宿主侧 `CronConversationBinder` 与 `CronScheduleAdapter`：严格模式校验
      Chat 的 session/user/channel ownership，并把 agent Cron 的 Trigger、执行超时、
      Approval policy 和 Delivery policy 无损映射到 Kernel Definition。纯文本 Cron
      明确留在 Delivery；repeating-once 已通过带 `start_at/end_at` 的 Kernel interval
      保留首次执行时间以及 count/until/never 终止语义。
    - [x] `CronManager` 已通过 `CronTaskRuntime` Port 支持显式双路径路由；只有 Runtime
      声明可无损承接的 Job 才进入新管线，其余继续旧 Executor。Port 要求实现等待
      Task 与 Delivery 终态，禁止把 Dispatcher 的“已启动”误记为 Cron“已完成”。
    - [x] 装配 Lite `CronTaskRuntime`：Workspace 默认注入实现，严格绑定 Chat，调用
      `ScheduledTaskDispatcher`，跟随 Task Event 到终态并结算 Delivery Receipt 后
      才返回 Cron history 结果。同一 scheduled slot 重放复用 Task 且不重复投递。
      当前接管可无损表示的 final/silent（含 repeating-once）agent Cron；stream 与
      文本显式保留旧路径。`tool_safety=True` 使用 AUTO Approval，Definition 写入短于
      attempt 的 approval deadline；Tool/Driver waiter 只信任带内部 Broker 的 Host
      `ExecutionContract`，审批请求和异常进入 Delivery/Inbox，silent job 不投递普通
      Result。旧 `interactive_tool_safety` 原因码仅用于读取历史记录。per-job model
      已通过 Kernel `ModelSelection` 进入 Definition、Task metadata、
      `RuntimeLaunchConfig` 和 Console 兼容边界，重启/重试不会偷换默认模型。
      RuntimeConfig 同时传播已验证的 Cron approval level；普通 final policy 只投递
      Result，受保护任务额外投递 Approval/Exception；两者都不会因 Runner 生成
      response Artifact 而额外发送 Artifact Ready 通知。
      真实浏览器批准、拒绝、超时与外部通知验收仍是 I8 门禁，当前不以单元链路代替。
    - [x] 新增公共 `LiteScheduledTaskRuntime`，Cron 与 Heartbeat 共用 Scheduler claim、
      Task Runtime、Delivery Receipt 和 Inbox Projection 结算。Heartbeat 在执行前创建
      或解析正式 Chat，并只把兼容 transport context 留在 Channel Adapter 地址中；
      相同定时槽重放复用 Task，手动运行使用独立 Fire。
    - [x] HTTP Task、Cron 与 Heartbeat 已按同一 Capability Registry 复用
      `TaskApplicationHost`、Workspace bindings、Orchestrator 和 Supervisor；新
      generation 刷新 TaskService 事件身份，但不会替换仍在运行的应用宿主或另建
      Schedule 专用执行所有权。
- [x] 冻结 `InboxItem / InboxProjectionPort` 并实现 Lite SQLite Projection；Item
  以 Delivery ID 幂等、按 Agent 隔离，read/handled 通过 revision CAS 更新。Task
  Delivery Worker 已在 Receipt 终态后投影，Cron scheduled replay 不重复通知；真实
  测试证明标记已读前后 Task Event 完全不变。
- [x] Console Inbox API 合并读取 SQLite Projection 与旧 JSON 兼容来源，统一排序、
  分页、筛选和未读计数；新 Item 以 `source_type=task` 进入现有前端。read 操作按
  来源写入，delete 对新 Projection 转为 handled，不删除源事实。
- [x] 历史 Mail / Skill / Heartbeat / ReMe / Cron JSON 数据已通过 Workspace 启动时的
  非破坏、幂等 Migration 重放为 `OperationalEvent → Delivery → Inbox`；只有新投影
  存在时 Console 才抑制旧行，单行失败继续显示旧数据，原 JSON 暂留作为回滚来源。
  新 Heartbeat 已通过 Task Delivery，新 Skill Auto Sync / Auto Update、Mail
  Monitor、ReMe 以及 Cron / Heartbeat 兼容回退已通过
  `OperationalEvent → Delivery → Inbox`。仓库内已无旧 JSON Store 生产调用。Memory
  插件通过 `MemoryBackendContext.operational_event_publisher` 获得同一 Host Service。
- [ ] 经过只读观察期并验证回滚后，归档并删除旧 JSON Store 及 Console 双读适配层；
  在此之前禁止物理删除历史文件。
- [ ] 旧入口建立兼容矩阵、弃用告警和删除门槛。
  - [x] 冻结 Chat、Queue、Approval、Cron、Inbox、Plugin、Capability、Artifact 与
    transport 身份的兼容状态、事实源和删除门槛；详见
    `docs/design/qwenpaw-compatibility-matrix.md`。
  - [x] 可替换 Plugin 注册入口提供结构化迁移诊断，不只写日志。
  - [x] 运行时 `/api/plugins` 已将诊断聚合为去重的 v2 manifest
    patch、逐项 action 和 blocker；`qwenpaw plugin migration-plan` 提供
    text/JSON 投影。计划固定 `safe_to_apply=false`，不会把 legacy
    handler 误当 Provider factory，也不覆写第三方源码。
  - [x] Cron Runtime 选择使用结构化 decision；Job 详情、最近状态、历史和日志
    共享稳定 fallback 原因码与删除门槛，旧 `supports()` Runtime 显式标记兼容状态。
  - [x] 旧 Inbox Migration 将 agent-scoped 观察起点、扫描与成功/失败计数、源指纹、
    连续稳定扫描和脱敏错误持久化到新 SQLite 表；只读 API 以 7 天、3 次稳定干净扫描
    作为关闭双读门槛，但不授权物理删除旧 JSON。
  - [ ] Inbox、旧 capability ID 与外部 backend Queue 仍需完成真实观察期、归档和
    恢复演练；在完成前顶层门禁保持未通过。
    旧 Task capability ID 已具备 agent-scoped 持久化命中计数、精确替换建议和
    命中即重置的 7 天零使用门禁；持久化引用迁移保持独立阻塞，不由观察结果代替。
    外部 backend 已冻结 `conversation_queue` 能力位；Console 在唯一 legacy admission
    点发送不含消息内容的幂等诊断，服务端核对 agent 当前 backend 后按 agent/backend
    汇总，并通过只读 API 暴露替代能力和删除门槛。Codex/Qoder 当前仍明确为 false。

验收：进程内 Lite Scheduler 可持久化触发、重启后恢复且不重复执行；Channel 和
Cron 不形成独立审批或产物事实源。

### I7：插件 SDK 与二次开发体验

- [x] Manifest 覆盖全部公开 Slot；公开后端 Slot 与 Protocol 门禁具有完整性测试，
  `config_schema` 在安装前按 JSON Schema 校验并返回精确字段、错误码和恢复建议。
- [x] 激活前按 Slot 校验实现 Port 与 capability identity；UI Slot 校验入口
  描述，错误实现不得发布新 generation。
- [x] 已有等价 Slot 的旧 `register_*` API 在实际调用时生成结构化迁移诊断，包含
  目标 Slot、恢复说明与 v2 manifest 骨架，并通过 `/api/plugins` 暴露；无等价
  public Slot 的 Host 生命周期 API 保持兼容且不生成误导性迁移建议。
- [x] 提供 Tool、Memory、Driver、Harness、Scheduler、Delivery 最小示例。
  - [x] Tool、Memory、Driver 与 Harness 示例只引用 `qwenpaw.plugins.sdk`，并通过
    manifest 激活、能力 ID、session/runner 调用和 generation 固定验证；Driver
    示例中的 Driver 通过公开 Host 产生真实统一审批，不实现私有 waiter 或状态机。
  - [x] Scheduler 示例只引用公开 SDK，以可配置 SQLite 路径实现完整
    Port/Trigger/Lease/Retry 契约，并通过真实持久化定义验证。
  - [x] Delivery 示例只引用公开 SDK，通过稳定 `delivery_id` 记录
    JSONL 副作用，并与 system Inbox Adapter 共享 Task Event、generation、
    Receipt 和 replay 行为合同。
- [x] 插件安装、替换、失败回滚、卸载与 cleanup 全链路无需服务重启；替换时旧
  capability 保持可解析，新 bundle 通过门禁后只发布一个 generation，失败会恢复旧
  manifest、文件、注册和实现，更新不会误执行永久 uninstall hook。
  - [x] 2026-09-30 使用运行中的 `localhost:8004` 与固定 Chat 完成真实链路：从空
    Registry 安装 `chat-tool-provider` 1.0.0 后，无需重启即可调用；强制替换为
    1.1.0 后，新 Invocation 返回 `PLUGIN_HOT_V2`；不满足 `tool.provider` Port 的
    2.0.0 在发布前返回 HTTP 400，Registry 仍固定 1.1.0，随后浏览器新 Invocation
    仍返回 `PLUGIN_HOT_V2`；卸载后 `/api/plugins` 恢复为空。
- [x] SDK 文档与所有参考插件只使用 `qwenpaw.plugins.sdk` 稳定导入路径，并由 AST
  契约测试防止示例回退到 App、Loader、Registry 或 Store 内部模块。

验收：新开发者不阅读内部源码即可完成插件开发、校验、安装、热替换和卸载；
错误插件不会污染当前 generation。

### I8：基建完成门禁

- [ ] 完成 Handbook 增量基础契约，不把它们下放到 Task 页面私有状态：
  - [x] `ContextPolicy` / `ContextManifest` 覆盖每次真实模型调用的来源、版本、
    信任级、裁剪、能力披露和内容哈希；不保存隐藏推理或 Secret。固定 Chat
    `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 已真实验证 generation 11 的调用前
    Manifest：93 个 Fragment、70 个 Tool Schema、7 个无指纹隐藏推理占位，
    用户原文未落盘，文件模式 `0600`，Conversation key 与 Manifest hash 复算一致。
    当前 Token 为估算值，且记录的是最终 AgentScope 输入而非 Provider SDK 的
    序列化字节；实际 usage 与 formatter 版本留给 Model Call Plane。
  - [ ] `ActionRequest` / `ActionResult` 统一 Tool、Driver、MCP、Shell、Browser
    与 Harness Remote Action 的身份、风险、幂等、审批、结果和 Evidence。
    - [x] 冻结 `ActionKind` / `ActionStatus` / `ActionRequest` /
      `ActionResult` / `ActionRecord` 与 host-owned `ActionStore`；纯领域模型从
      Plugin SDK 导出，文件存储和 Recorder 不作为插件 API 暴露。
    - [x] 内置与插件 `ToolDefinition` 共用
      `PolicyGuardedTool -> ToolCoordinator -> RuntimeActionRecorder`：执行前请求
      留证，Artifact/Evidence 发布后写结果；内容类参数只留
      `[CONTENT OMITTED]`，参数与结果摘要仅覆盖安全投影，不生成原始 Secret 或
      工具输出的可猜测指纹，JSON 权限为 `0600`。
    - [x] 后处理落盘失败不再被 Coordinator 静默吞掉：已执行动作返回
      `unknown` 且禁止自动重试；请求落盘失败则 fail closed，并明确声明工具未执行。
    - [x] 2026-10-01 固定 Chat
      `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 在 generation 11 真实调用
      `write_file`：Action 与 ChatSpec.id、Invocation 和 provider 对齐，结果关联
      1 个 Artifact 与 1 个 Evidence，正文未进入 Action JSON，最终 SSE 完整结束。
    - [x] 系统与插件 Driver Provider 通过显式 Adapter 接入同一 Recorder；旧
      Driver Manager 兼容路径也生成 `DRIVER` Action。运行中产生的审批通过不可变
      `ActionApprovalLink` 关联 Request/Result；策略拒绝记为 `denied`，不误报为
      执行失败。MCP 作为 Driver 协议已覆盖，未建立第二套 MCP Tool Namespace。
      固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 已在 generation 11
      真实调用 dingtalkdoc `get_document_info`：Action 与 ChatSpec.id、Invocation、
      系统 Driver Provider 和真实 capability 对齐，结果成功且 JSON 权限为
      `0600`。远端未声明 `readOnlyHint`，因此按 `external_write/high` 保守记录；
      标准 MCP 只读注解的低风险映射已有定点合同覆盖，不根据工具名猜测权限。
    - [x] 统一与兼容 Browser 均通过显式 `ActionKind.BROWSER` 接入同一
      `PolicyGuardedTool -> ToolCoordinator -> RuntimeActionRecorder` 外层行动
      边界；内置 descriptor 和插件 `ToolDefinition` 使用同一元数据，不根据工具
      名或 Policy 名推断。Browser 内部逐方法副作用分类仍由 Browser 子系统负责，
      不复制第二套 OS 状态机。真实 Chromium 公共 Tool journey 已通过。固定 Chat
      `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 已在 `/clear` 后真实访问
      `https://example.com`，界面记录 7 个步骤和两次 Browser 调用，最终严格返回
      `BROWSER_ACTION_E2E_OK: Example Domain`。
    - [x] Browser 超限输出通过 Host-only 声明接入统一 Artifact Publisher，不向
      插件开放任意结果路径，也拒绝捕获 Workspace 外文件。固定 Chat 在 generation
      11 真实产生 1100022 字节 stdout：Browser Action 保持失败，完整输出生成
      `browser.output` Artifact 与 Evidence，并绑定 ChatSpec.id、Invocation 和真实
      Tool Call；256KB 内联预览正确返回 413，附件下载返回 200、1100022 字节且
      源内容哈希一致。失败 Action 保留诊断 Artifact 引用，不伪装为执行成功。
    - [x] Chat 提供有界只读 `GET /api/chats/{ChatSpec.id}/actions?limit=`，先验证
      Chat 所有权，再返回稳定 `ActionRecord`。`arguments` 与 `observation` 不进入
      响应；API 边界会二次清理历史 `redacted_arguments` 并重算投影哈希，因此旧版
      Browser code 不会因新增查询接口泄露。固定 Chat 实测返回 Browser kind、终态
      及 Artifact/Evidence 数量，历史三条 Browser code 均投影为
      `[CONTENT OMITTED]`，不读取或暴露私有 Action 文件路径。
    - [x] 本地 Codex/Qoder Harness Remote 已在统一 Event 翻译层接入：受控 Chat
      turn pin 住 capability generation，Action 保存 Harness EnvironmentRef；Codex/
      Qoder 审批在 Provider 恢复前生成 Request 与不可变 Approval Link，远程工具终态
      映射为 succeeded/failed/denied/cancelled。命令参数按内容字段省略，不进入 Action
      JSON。Provider 丢失 TOOL_COMPLETED 时终态为 `unknown`，副作用为 `uncertain`，
      禁止把断线伪装为失败后可安全重试。68 项 Harness 定点测试及全部 Python 文件
      门禁通过。
    - [ ] Hub remote runner 与跨主机执行仍待接入 attested Action/Environment
      Adapter；现有 Task `SideEffectRecord` 继续作为 Task 防重放权威，不迁移为第二
      状态机。仅有 TOOL_STARTED 的 Provider 事件属于 observed boundary，不宣称
      宿主已在实际执行前拦截。
  - [ ] `EnvironmentContract` 冻结 Workspace、Sandbox、Harness 和 Hub runtime
    共同需要的挂载、网络、凭据引用、依赖、资源、快照与清理语义。
    - [x] Kernel 已冻结 Contract、Resolution、Ref 与 Resolver/Store Port；Lite
      Chat 在 Agent 建立前解析并持久化 Invocation 环境事实，Action 通过 Ref
      关联，环境不满足时不会进入模型或工具执行，generation lease 正常释放。
    - [x] Lite 只报告能证明的 Host、继承网络、Workspace/Mount 访问与依赖；
      隔离网络、Secret、硬资源、超时/并发、快照和清理要求均显式失败关闭。
      Plugin SDK 仅公开稳定数据模型，不公开 host-owned Resolver/Store。
      固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 已在 `/clear` 后完成真实
      `read_file`：Resolution 为 `satisfied`，环境 JSON 为 `0600`，Tool Action
      的 Invocation、ChatSpec.id 与 EnvironmentRef 均和环境证据一致。
    - [x] 旧 SandboxConfig 已通过 Action 级 Adapter 映射到同一 Contract；
      Resolution 以具体后端 `_enforced_fields` 为事实，环境变量只留名称，Action
      Request 先关联不可变环境证据再准入执行。后端仅记录 warning 的内存、进程、
      domain allowlist 等约束不再被当作已兑现，统一执行前失败关闭。
      105 项 Sandbox/Environment/Action/Governance 定点测试通过；固定 Chat 因用户
      配置 `security.sandbox_enabled=false`，真实 Shell 正确保留 Host Ref，未伪造
      Sandbox 证据。开启态端到端仍待隔离配置环境验证。
    - [x] 本地 Codex/Qoder Harness 已接入 Chat 控制面：能力解析完成后、Provider
      command/turn 调用前持久化确定性 Resolution；环境失败时 Adapter 不执行且
      Queue lease 正常结算。Resolution 分开保存宿主验证的 Workspace/依赖和
      Provider 声明的 sandbox/permission，Secret 值不进入 Contract。Harness、
      Environment、Sandbox、Action 与 SDK 共 120 项定点测试通过。
    - [ ] Harness Remote、Workstation 与 Hub runner 仍需实现等价 Adapter、真实
      约束兑现和可验证 attestation；完成前父项保持未完成。
  - [x] 语义观测统一 `MODEL`、`ACTION`、`CONTROL`、`GUARDRAIL`、
    `COMPACTION`、`HITL`、`INTERRUPT` 与 `VERIFICATION`；模型意图、策略判定、实际执行和
    Runtime 独立 Evidence 使用稳定因果 ID 关联，不把 Agent 自报结果当作事实。
    - [x] 冻结 `RuntimeObservation`、Category/Stage/Status、Source 和只读
      `ObservationProjectionPort`，并从权威 Model Call 与 Action Record 动态派生，
      不复制 Prompt、消息、工具正文或隐藏推理。Chat-owned
      `GET /api/chats/{ChatSpec.id}/observations?limit=` 已接入；固定 Chat 的真实
      查询在同一时间线返回 11 条 MODEL 和 9 条 ACTION（limit=20），覆盖 policy、
      execution、intent、evidence，且 Action 结果保留 Artifact/Evidence 引用。
    - [x] `InteractionRecord` 冻结 request + optional resolution 历史契约，
      独立只读 `InteractionHistoryPort` 可按 Agent 与 `ChatSpec.id` 查询所有状态，
      不扩张或破坏既有运行交互 Port；Approval、User Input、Suggestion 统一派生 HITL
      intent/evidence。ActionRequest 的 policy decision、risk、effect 和 approval links
      独立派生 GUARDRAIL policy，不与 Action 结果混写。固定
      Chat 真实查询共返回 89 条观察项：MODEL 11、ACTION 28、GUARDRAIL 14、HITL
      36；未复制 prompt、回答正文、values、用户 ID 或隐藏推理。
    - [x] `ControlRecord` 冻结 command + latest receipt 查询契约，独立
      `ControlHistoryPort` 不扩张运行控制 Port；Steer、Cancel 与 Reorder 派生
      CONTROL，Interrupt Current 与 Stop and Clear 派生 INTERRUPT。投影保留目标、
      revision、状态和 Steer safe point，不复制 instruction、idempotency key 或
      receipt detail。固定 Chat 真实查询共返回 101 条观察项：MODEL 11、ACTION 28、
      GUARDRAIL 14、HITL 36、CONTROL 8、INTERRUPT 4；精确递归检查未发现禁止字段。
    - [x] COMPACTION 已接入权威、内容最小化的不可变记录：自动压缩、手动
      `/compact` 和 Provider overflow recovery 共用同一契约，记录策略、触发原因、
      前后消息数、淘汰/折叠数量、上下文身份与摘要是否变化及错误码，不保存消息、
      摘要、指令或异常正文。Agent 构建后的压缩与 Agent 构建前的 standalone
      Slash Command 均接入；后者是固定 Chat 验收发现并封堵的旁路。真实 no-op
      不伪造压缩成功记录：固定 Chat 当前上下文仅占 0.1%，`/compact` 后观察项仍为
      110 条（MODEL 20、ACTION 28、GUARDRAIL 14、HITL 36、CONTROL 8、
      INTERRUPT 4），COMPACTION 为 0；material/failure/overflow 路径由 130 项
      定点测试覆盖。
    - [x] VERIFICATION 直接从 Task append-only Ledger 的
      `verification.completed` 事件派生，不建立第二个 Verification Store；新增
      `VerificationHistoryPort` 按 `ChatSpec.id` 读取新 `conversation_id` 和旧
      `chat_id` 关联的 Task。Host-owned `VerificationRecord` 保留 event、Task、Run、
      Invocation、correlation、generation 与事件时间；观察项只公开 verifier、验收
      通过/失败数量及 Artifact/Evidence 数量，不复制 criterion、reason 或 metadata。
      临时真实 SQLite Ledger → Verification Event → Chat API 读链路及失败隐私投影
      共 67 项定点测试通过。运行中 24 个 Task 当前没有 Verification 源记录，固定
      Chat 查询正确保持 VERIFICATION=0，没有伪造通过状态。
    - [x] 完成跨来源 cursor/pagination：稳定 `ObservationPage` 保留旧列表 API，
      新增 Chat-owned `/observations/page`；Lite 内容零拷贝索引只保存 source pointer、
      UTC 排序键与单调 `indexed_sequence`。游标绑定 Chat、首屏水位和最后排序键，
      新增或后补 Evidence 不污染旧快照，畸形、不可用锚点和跨 Chat 游标失败关闭。
      各权威源提供 Lite 全量索引重建入口，不再截断每类 1000 条；同时间戳跨来源、
      翻页期间完成 Model Result、1001 条历史及 API 400 映射由 79 项定点测试覆盖。
  - [ ] 完成 Model Call Plane 全部路由与成本能力：
    - [x] 冻结 `RouteDecision` / `ModelCallAttempt` / `ModelCallResult` / Store Port，
      并在 `TokenRecordingModelWrapper` 的真实 Provider 网络边界留证。每个实际请求
      发送前记录 Provider/Model、generation、ContextManifest、策略版本、选择原因和
      previous attempt；同模型重试、跨模型 fallback 与 overflow retry 分别建独立
      Attempt。流式终态、提前关闭、取消、错误分类和 Provider usage 在结果中明确
      区分，不保存消息、Prompt、隐藏推理或 Secret。
    - [x] Chat 提供所有权约束的
      `GET /api/chats/{ChatSpec.id}/model-calls?limit=`。固定 Chat
      `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 在 `/clear` 后真实返回
      `MODEL_CALL_PLANE_E2E_OK_3`；只读记录为 DashScope `qwen3.8-max`、generation
      11、primary、succeeded、input 40042 / output 74 tokens，Invocation 与
      ContextManifest 均有稳定引用。真实验收发现并修复了 ContextVar 跨异步关闭和
      terminal chunk 被误判为取消两类仅流式路径问题。
    - [x] Route 已同时记录逻辑请求和实际 Provider/Model，Attempt 记录实际 Adapter、
      Formatter 及其包版本；Provider 返回的微单位成本直接留证，缺失价格明确记录
      `cost_unknown=true`，不冒充零成本。固定 Chat 再次 `/clear` 后真实返回
      `MODEL_CALL_FACTS_E2E_OK`，记录 requested/actual 均为 DashScope
      `qwen3.8-max`，Adapter/Formatter 版本为 `2.2.2b1`，调用成功且价格未知。
    - [x] Token Usage Summary 已把 Model Call 成本事实投影到 global、Agent、
      `ChatSpec.id`、Invocation turn、日期和实际 Provider/Model：`cost_micros` 只累计
      已报告微单位，`cost_unknown_calls` 独立累计未知价格调用。legacy JSON 全部明确
      视为未知，cutover 前 shadow 只补成本而不重复 token/call；Console 同时显示两者，
      不推断币种或把部分已知成本冒充完整账单。
    - [x] Model Call 写入不再丢失运行归属：Attempt 保存 Agent、`ChatSpec.id`、
      Invocation turn 与实际 Provider/Model；Result 保存 Provider 报告的 input/output、
      cache 和 cost。一次 turn 内工具循环的多个模型调用共享 Invocation，实际
      retry/fallback 仍各有 Attempt。Chat 消息继续携带本 turn usage 和上下文窗口投影，
      并显式区分 `provider_reported` 与 `local_estimate`。
    - [x] Chat Turn usage 对工具循环的同路由调用按 Provider/Model 合并并保留
      `call_count`；fallback 或中途切换路由时保存独立 `model_routes` 明细。Turn
      总量不再被误标为最后一个模型的用量，刷新后仍从消息 metadata 恢复同一明细。
    - [x] Token Usage Summary 已成为服务端权威统计契约：同一查询同时返回全局、
      日期、实际 Provider/Model、日期×模型、Agent、`ChatSpec.id` 和 Invocation turn
      聚合。Console 不再下载明细并维护第二套页面私有聚合口径；`/details` 仅保留为
      明细查询与兼容 API。不同 Agent 下相同 Chat/Turn ID 使用复合 scope key 隔离。
    - [x] Token Usage Summary 已从单一 JSON 数据源切换为 Model Call usage 的可重建
      Lite 投影。SQLite 只保存内容无关的派生字段，以 `attempt_id` 幂等；应用启动扫描
      全部已配置 Agent Workspace 并原子 rebuild。首次初始化冻结下一 UTC 日为 legacy
      cutover：新代码在水位前双写作 shadow，水位后停止有归属的 JSON 写入；查询过滤
      水位后的有归属 legacy row 后再合并投影，因此不按日期直接相加或双算。诊断 API
      返回 cutover、索引量、最后 rebuild 时间与条数，不暴露 Prompt 或消息。
    - [x] 同一 Summary/Details 契约已加入 Context Window 事实统计：Model Call Attempt
      固化实际 context window 与 compaction threshold；投影按缓存语义选择有效输入，
      并为 global/Agent/Chat/turn/date/model 输出加权利用率、峰值、可观测调用和临近
      压缩调用。水位前 shadow 只覆盖 Context 字段，Token/Calls 仍取 legacy JSON，
      因而不会双算；旧 SQLite 自动增加 nullable context columns。
    - [ ] Workstation / Hub 的健康度、成本和数据边界动态路由仍待实现。Lite 当前
      继续使用确定性主模型与显式 fallback 顺序，不静默切换。
  - [x] Run Completion、Verification 与业务 Outcome 分层；当前已阻止 Invocation
    success 被投影成业务完成，本阶段不建设完整 Evaluation UI。
    - [x] Kernel 已冻结 `ConversationOutcome` 与 Store Port；Lite SQLite 以具名 producer、
      correlation、Artifact/Evidence/Verification 引用和显式 supersession 保存不可变
      Outcome。Chat Runtime 只有读取到晚于最近 Submission 且当前无 live work 的显式
      Outcome，才投影 achieved / partial / not_achieved / abandoned。
    - [x] 冻结 Outcome producer admission：Host Outcome Broker 对 system/plugin 使用
      同一声明入口和热注册机制，普通 Chat 校验 Artifact/Evidence 的 ChatSpec 归属；
      Task-owned 声明校验 Agent、ChatSpec、Task、Run 与 correlation，只有既有
      Completion Gate 同时满足 Execution Contract、Verification Policy 和 Result
      Package 时才允许 achieved。Producer 不直接访问 Store。
    - [x] Broker 已装配到实际 Workspace/Invocation 生命周期：Tool/Driver 的内置与
      插件 Host 都实现可选 `OutcomeHostAccess`，旧 `ToolHost/DriverHost` Protocol 不
      增加必选方法；Invocation Host 固定 Agent、ChatSpec、correlation、Invocation、
      generation 和 producer。system capability 自动受信，plugin 必须由 Host 显式
      注册；热注册后新 Invocation 立即可用，无需重启。Producer admission 在
      Invocation 创建时冻结，卸载只阻止新 Invocation，已固定旧 generation 的执行可
      继续完成。
    - [x] 内置 Goal Mode 已从 transport `session_id` 主键迁到优先使用
      `ChatSpec.id`；只有没有稳定 Chat identity 的兼容 Channel 才回退 session。
      `update_goal(complete)` 与 `update_goal(blocked)` 必须先经 Invocation-bound Host
      Outcome Broker 分别落 `ACHIEVED` 与 `NOT_ACHIEVED`，再结束 Goal。Outcome
      持久化失败时 Goal 保持 active，Assistant Message、Tool 返回或 SSE 完成均不能
      冒充业务完成。迭代与预算上限仍只是技术终态，不凭空声明完成或部分完成。
    - [x] Goal active state 已迁入 revisioned `GoalExecutionStore`；Lite SQLite 保存
      objective、预算、进度、correlation 与确定性 Outcome intent。新 Invocation 在
      mode turn-start 恢复领域快照，不恢复旧协程。`OUTCOME_PENDING` 关闭 Outcome
      成功/Goal finalize 前的崩溃窗口，Broker 通过可选 exact lookup 跨 Invocation
      幂等确认；业务字段变化失败关闭。Chat Submission admission 通过
      `ConversationCorrelationResolver` 在 Goal active/pending 时继承原 correlation，
      terminal 后才创建新意图。
    - [x] Workspace 启动扫描 pending Goal，并通过内部 Submission 创建新的恢复
      Invocation；恢复固定当前 capability generation、继承原 correlation、经过相同
      Host Outcome Broker，但不调用模型或写入伪 Assistant Message。queued/running
      恢复项去重，orphan 被标记 interrupted 后允许新尝试，用户无需补发“继续”。
    - [x] 接入 correlation-scoped 可回放 Trajectory：复用现有 Model、Action、
      Submission、Steer/Interrupt、Interaction、Artifact、Evidence、Verification、
      Wait/Recovery 与 Compaction Observation，并把完整 Outcome supersession 链投影为
      `OUTCOME`。只存 content-free source pointer 的派生索引按发生时间正序固定快照
      分页；`GET /api/chats/{ChatSpec.id}/trajectories/{correlation_id}` 为只读 API。
      Legacy 无 correlation 的事实明确不进入轨迹，不从时间相邻关系猜测归属；不增加
      HTTP 写入口，也不允许模型文本声明 Outcome。
      2026-10-08 固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 的真实
      correlation `b19d5f7f-2045-4607-b36c-2ba70c45e41a` 返回 v1 Trajectory：10 个
      正序事实覆盖 Model、Action、Guardrail、HITL 与 Submission，全部 correlation
      一致；该链没有显式 Outcome，因此响应未伪造 Outcome 节点。
  - [x] `BudgetLease` 与现有 `ExecutionBudget`、Usage Scope、Capability lease
    明确区分：`ExecutionBudget` 是静态额度合同，Task Ledger 是持久用量事实，
    `BudgetLease` 是可派生、可撤销的运行授权。Lite 已将根 Lease 接入真实
    `TaskExecutionCoordinator`，HTTP 子 Agent 从 Usage Scope 派生独立 Lease；兄弟
    配额不能超额预留，释放归还未用配额，根撤销级联后代，实际超额仍先写 Ledger。
    Lite 的 Lease identity / 状态仅进程内存在，重启后从持久 `UsageSnapshot` 重建根
    准入；Workstation / Hub 的跨主机租约、TTL、fencing 与分布式级联仍待实现。
  - [x] Kernel `CommunicationContract` 已区分 S1 request/response、S2 request
    stream、S3 durable handle 与 S4 durable channel，并冻结 ordering、idempotency、
    cursor、retention、backpressure 和 disconnect policy。真实 Chat Runtime 声明
    SSE 仅为 latest-state snapshot reconnect，Submission 才是断线后继续运行的 durable
    handle；Lite 不虚构 replay/ack/durable-channel 能力。GET 与 SSE 首帧返回同一
    Contract，关闭流不取消 active Invocation 的行为已有 API 测试。
  - [x] Kernel `CapabilityRelease` / `CapabilityLockManifest` 已把 generation 整数
    展开为实际选中的 system/plugin release 证据。Assembly 在执行前以 pinned
    descriptor 编译并 `0600` append-once 持久化；InvocationScope 与 ContextManifest
    引用同一 lock ID/hash，冲突失败关闭。Chat 只读 API 可审计 Lock 与 Context 的
    对齐关系；不持久化实现、配置值或 Secret。
  - [x] Lite Registry 为 system/plugin provider 生成同构、内容寻址的 `stable`
    release tag；tag 展开 descriptor hash。替换版本可使用 expected release hash
    fencing 做 provider 级一次性回滚，回滚发布新的单调 generation、保留无关 provider
    的并发晋升，旧 lease 不漂移。可执行回滚点仅在进程内；重启由 manifest 重新装配。
    OS/插件管理面通过 `GET /api/plugins/capability-releases` 只读公开当前 generation
    和 stable tags；启动期从 Workspace Registry 读取同一事实源，不依赖 Task route，
    也不暴露实现、配置或 Secret。
  - [x] Kernel 冻结 Candidate、Promotion Check/Evaluation/Event 与 Journal/Gate Port；
    Lite 使用 `0600` append-once WAL。activate 与 rollback 均先写 prepared、发布内存
    generation、再写 committed；commit 失败恢复旧 snapshot/tag/fence 并写 aborted。
    `registry_epoch_id + generation` 区分跨进程重启的同号 generation；Plugin 管理面
    通过 `GET /api/plugins/capability-promotions` 只读查询，不开放远程晋升或回滚。
  - [x] Registry epoch 已从 Snapshot/Lease 贯穿 InvocationScope、Capability Lock、
    Context Manifest、Route Decision 与 Model Call Attempt；新证据 hash 纳入 epoch，
    历史无 epoch 记录保持兼容。真实 Chat 已验证同一 Invocation 的 epoch、generation、
    Lock hash 与 Context ID 引用闭合。
  - [x] provider deactivate 已纳入同一 Promotion WAL：prepared 成功后才发布不含该
    provider 的新 generation，commit 失败恢复 snapshot、stable tag 与 rollback fence
    并记录 aborted；不存在的 provider 保持幂等且不生成虚假 Journal Event。
  - [x] Promotion Evidence Bundle 已冻结并接入真实发布：每个 check 回指同一
    Candidate 的内容寻址证据；Lite Store 使用 `0600` append-only 文件。Registry 在
    prepared WAL 前持久化并回读 Bundle，证据缺失、跨 Candidate、check/outcome 不匹配
    或 Store 故障都失败关闭；只读 API 可按 candidate 回查，不暴露实现、配置或 Secret。
  - [x] Slot Contract 已声明机器可读的 promotion risk 与 scenario ID；Lite 首个真实
    Scenario Runner 在发布前对 staged `artifact.renderer` 执行有界 round-trip，验证
    identity、source hash、disposition、filename、output budget、安全 inline media
    type 与 attachment byte preservation。system/plugin 共用同一门禁；失败阻断
    generation，unsupported 明确记录 `not_applicable`，不冒充通过。Gate 自身异常时
    已完成的 Scenario 证据仍保留在拒绝 Bundle 中。
  - [x] `tool.provider` 已接入同一 staged Scenario Runner：使用无 Credential、无
    Interaction、无真实 Workspace I/O 的 Host 执行有界 catalog discovery，不调用
    工具本体；校验目录类型、128 项数量、唯一且有界名称、`ToolDefinition`/legacy
    callable、治理 target/pattern 参数、64 KiB 序列化预算和 5 秒超时。空配置不满足
    合法 schema 时记录 `not_applicable`，schema 非法或目录违规时失败关闭；真实
    `chat-tool-provider` 与内置 Workspace Tool Provider 共用该门禁。
  - [x] `memory.provider` 已接入受限 Session Scenario：Host 只提供空配置与进程内
    revisioned State Store，不连接用户 SQLite 或 legacy backend；验证 Session Protocol、
    32 KiB prompt、共享 Tool catalog 规则与 5 秒 open/close timeout，并保证失败路径
    仍关闭 Session。真实 `runtime-provider-kit.project-memory` 与内置 Workspace Memory
    Provider 共用门禁；Evidence 不保存 prompt 或 state value。
  - [x] `driver.provider` 已接入 catalog-only Session Scenario：Plugin Host 无
    Credential 且拒绝 open 阶段 Approval，系统私有 Host 只返回空兼容目录；场景复用
    Kernel `validate_driver_session`，验证 provider ownership、唯一 capability/name、
    128 tools、64 KiB catalog、16 fragments、32 KiB prompt 与 close timeout，绝不调用
    `invoke()`。Runtime 继续兼容导出同一验证函数，不再维护第二份规则。
  - [x] `delivery.adapter` 已接入 routing-only Scenario：Contribution 通过有界
    `metadata.delivery_addresses` 提供最多 8 个地址样例；宿主只调用 `supports()`，验证
    正确 adapter/address 被接受且 foreign adapter identity 被拒绝，绝不调用
    `deliver()`。缺少 hints 记录 `not_applicable`，错误 hints 或路由不一致失败关闭；
    system channel/inbox 与真实 local-jsonl plugin 共用门禁。
  - [x] `scheduler` 已迁移为无状态 `scheduler.provider`：Provider 只能通过
    `SchedulerHost` 取得宿主管理的 `SchedulerPort`，不再拥有数据库路径、环境变量或
    用户状态。Promotion 使用独立的进程内只读 Store 执行 `list_definitions()`，验证
    catalog 类型、Agent 所有权、唯一 ID、128 项与 64 KiB 上限；所有写方法失败关闭，
    不创建或读取真实 SQLite。公共 Plugin SDK 不再导出具体
    `SQLiteSchedulerStore`；旧 `scheduler` Slot 仅作为迁移兼容入口保留。
  - [x] `runner` / `harness.runner` 已接入 `runner.preflight` Scenario：使用明确
    `external_io_allowed=False` 的请求验证 capability identity、Slot、candidate
    generation、Contextual 支持与 Cost Accounting 声明，5 秒超时，绝不调用
    `execute()` / `execute_context()` 或解析 Workspace。旧 Runner 记录
    `not_applicable`；畸形或不一致结果失败关闭。该门禁是 SDK capability isolation，
    不是本地 Python 的 OS sandbox，真实执行仍由 Environment/Policy/Approval 管理。
  - [x] capability-bearing Plugin/PawApp 永久卸载已增加 exact-release 授权栅栏：
    首次请求返回 428 challenge，Console/CLI 在既有用户确认后携带 release hash
    重试；Loader 在同一 lifecycle lock 内校验，过期确认在 hook、注册和文件变更前
    失败关闭。显式卸载 Evidence 记录 `operator-authorized=passed`，内部热替换为
    `not_applicable`，不把更新误记为人工卸载。
  - [x] capability-bearing Plugin 的显式安装/更新已增加 exact-candidate 授权栅栏：
    首次请求返回 428，Console/CLI 只用响应中的 candidate hash 重试。Candidate 同时
    绑定 manifest、Contribution 契约与 64 MiB 有界的源码树摘要；Loader 在同一
    lifecycle lock 内、复制/依赖安装/代码执行之前校验，并在复制后再次核验。
    过期、跨插件或来源变化的确认失败关闭；启动恢复记为 `not_applicable`，显式
    安装 Evidence 记为 `promotion.operator-authorized=passed`。
  - [x] Promotion Evidence Bundle 已作为标准 Evidence Artifact 暴露：Host 从唯一
    权威 Bundle 确定性派生 `ArtifactRef` 与 canonical JSON，不复制第二份文件；
    `content_hash`、size 和下载字节一致，时间戳不影响 Artifact identity。只读 API
    返回 candidate/bundle/evaluator lineage，不暴露实现、配置、日志、主机路径或
    Secret，system/plugin 共用同一路径。
  - [x] Promotion 人工授权已从 Plugin Loader 特例提升为 provider-neutral Host
    Policy：`CapabilityPromotionOrigin` 区分内部启动、内部恢复与显式 operator ingress，
    risk 从 Host Slot Contract 聚合而不是信任 Provider 自报。low-risk 显式候选无需
    人工确认；medium/high 必须绑定精确 candidate hash，过期 hash 在 factory、依赖和
    插件代码执行前失败关闭。system/plugin 共用 Registry 强制点和
    `promotion.risk.<level>` Evidence；Plugin 的 428 只是同一策略的 HTTP Adapter。
    内部启动/恢复继续记录 `not_applicable`，不能伪造人工授权。关键词发现索引与
    Workstation/Hub Release Registry 仍待实现。

- [x] 每个公开扩展模块均有 system/plugin contract tests；system-only 模块具有
  核心激活、固定 generation、失败关闭和代际排空合同。
  - [x] Capability Registry 对 system 与 plugin 的每次 bundle activation 使用同一份
    Slot Protocol、capability identity、UI entrypoint 与兼容 Slot 实现门禁；错误实现
    在发布新 generation 前失败，两个 provider kind 共享同一负向合同测试。
  - [x] `agent.factory` 明确为 system-only 核心 Slot：仍由 Registry 固定 generation、
    校验 identity/protocol 并等待 lease 排空，但不再从 Plugin SDK 导出；第三方
    manifest 在实现加载前以 `system_slot` fail closed，避免完整 HookContext、
    AppServices 与 AgentScope event object 成为伪公共 API。Agent 行为扩展使用下层
    Mode/Prompt/Tool/Command/Hook/Gate/Memory/Driver 公共 Slot。
  - [x] UI experience Slot 已接入真实 Console bundle loader：后端只发布通过
    generation 门禁的 `ui.*` 声明，前端顺序建立宿主激活事务，校验
    plugin identity、声明 Slot、必须注册及禁止延迟注册。替换 bundle
    失败时保留旧 UI generation；V1 bundle 仍走显式兼容模式。
  - [x] `planner`、`strategy` 与 `runner` 的系统/插件实现通过同一真实 Orchestrator
    行为合同；Plan 持久化、Run Strategy 身份、Context 参数以及
    Artifact/Evidence producer 均按固定 generation 验证。
  - [x] `sensor` 的 system/plugin 实现通过同一 Context、Proposal ownership 与
    durable Approval 行为合同；内置 proactive cognition 已使用 system Adapter，
    旧插件保持兼容但不能伪造 Proposal source 或绕过 payload/batch 门禁。
  - [x] `prompt.provider` 具有 system/plugin 行为合同：稳定 identity、健康检查、
    deterministic provider-owned fragments、非空内容和不可变 InvocationScope；参考插件
    通过 manifest、固定 generation、RuntimeAssembly 与 AgentBuilder 真实合并链路。
  - [x] `command.provider` 具有 system/plugin 行为合同：固定目录、Provider
    所有权、稳定 ID、系统保留名和冲突关闭失败；插件不能拥有动态
    fallback。参考插件通过 manifest、固定 generation、RuntimeAssembly、
    Runtime 会话打开与 CommandRouterSession 真实分发链路。
  - [x] `hook.provider` 具有 system/plugin 行为合同：固定目录、Provider
    所有权、跨 Provider 确定性排序、sticky `SKIP_AGENT`、立即
    `SHORT_CIRCUIT` 和有来源的上下文注入。参考插件通过 manifest、
    固定 generation、RuntimeAssembly、Runtime 会话打开与真实生命周期执行链路。
  - [x] `loop.gate.provider` 具有 system/plugin 行为合同：固定目录、
    Provider 所有权、scope 选择、稳定优先级、turn/conversation reset 和
    第一个 actionable decision。工具调用轮次的继续指令在工具完成后的下一个
    reasoning safe-point 消费，不替代 Queue Interrupt 或 Steer。
  - [x] `tool.provider` 具有 system/plugin 行为合同：共享
    `InvocationScope`/`ToolSelection`/`ToolHost`/`ToolDefinition`，经过同一
    AgentBuilder 收集、名称冲突闭合、Tool Guard、Approval、Sandbox 与 Side
    Effect 管线。每个已包装工具固定 generation 内的治理元数据；热替换只更新
    新 Invocation，不改变运行中工具的 type/target/sandbox/effect。
  - [x] `memory.provider` 具有 system/plugin 行为合同：单选 Provider 通过固定
    generation 打开 invocation-scoped Session，Prompt 进入共享 Contributor，类型化
    Memory Tool 进入同一 Tool Guard。状态按 provider + Agent/`ChatSpec.id` 命名空间
    隔离并使用 revision CAS；插件 Host 不包含系统兼容 backend，Session 仅关闭自有资源。
  - [x] `driver.provider` 具有 system/plugin 行为合同：单选 Provider 通过 manifest、
    固定 generation、RuntimeAssembly 和 Builder 打开 Session；类型化 Tool 经唯一
    AgentScope Adapter，Prompt 进入共享 Contributor，高风险调用通过统一 Interaction
    审批后继续。第三方 Host 不包含 DriverManager、request context 或系统 `load()`；
    热替换只影响新 Invocation，旧 Session 的工具行为保持原 generation。
  - [x] `harness.runner` 具有 system/plugin 行为合同：内置 Codex Adapter 与公开 SDK
    Runner 共享 `TaskRunner`/`ContextualTaskRunner`、固定 generation、类型化
    `RuntimeContext` 和 `TaskExecutionCoordinator`。两者的信号进入同一 Ledger，
    Run/Task 使用同一终态；最终结果通过注入的内容寻址 Emitter 生成 Artifact 与
    Evidence，插件不写私有结果路径或另建执行状态机。
  - [x] `agent.mode.provider` 具有 system/plugin 行为合同：单选 Provider 经 manifest、
    固定 generation、RuntimeAssembly 打开 invocation-scoped Session，Builder 只从
    Session 获取 active names，Runtime 统一执行 turn start，`/clear` 与 `/new` 统一
    reset。插件 Host 仅提供 schema 校验后的 detached config，不含 Workspace context、
    系统 Mode snapshot 或兼容 lifecycle；热替换后的旧 Session 行为不漂移。
    - [x] Kernel/SDK 已冻结 namespaced `AgentModeState` 与 Host
      `read_state/write_state`：Provider/Agent/Chat/state-key 隔离，revision CAS 阻止旧
      generation 覆盖，新 generation 可显式升级 state schema。Lite Store 由 Host 持有，
      插件拿不到 SQLite；单值限制 64 KiB，系统与插件 Host 共用同一契约。
      `clear_state` 通过空值 revision 保留 reset 栅栏，避免物理删除产生 ABA 覆盖。
  - [x] `scheduler.provider` 具有 system/plugin 行为合同：Provider 通过最小
    `SchedulerHost` 绑定宿主持有的 Store，从 generation-pinned Fire lease 创建唯一
    Task，继续进入 Planner/Strategy/Runner 与 Artifact/Evidence 管线；持久化重开与
    重复 Fire 均不重复执行。旧 `scheduler` 只保留兼容解析，不再是正式公共合同。
  - [x] `delivery.adapter` 具有 system/plugin 行为合同：已提交 Task Event
    经 Projector/Worker/Dispatcher 生成固定 generation 的 Request 和持久化
    Receipt，重放不重复副作用，热卸载不破坏已 pin 的旧 lease。
  - [x] `artifact.renderer` 具有 system/plugin 行为合同：共享 ArtifactRef、
    EvidenceRef、内容哈希、MIME/大小门禁、固定 generation 和安全
    fallback-next 语义，Chat 与 Task 使用同一渲染服务。
  - [x] Runtime 不再在 Workspace 缺失 Host-owned Capability Registry 时创建
    隐式本地 Registry；错误产品装配直接 fail closed，防止 Chat、Task
    和 Plugin Loader 分裂为不同 generation 事实源。
  - [x] 已建立 Host-owned、机器可校验的 Capability Conformance Matrix，精确覆盖
    全部 Slot，并通过只读 `/api/plugins/capability-conformance` 暴露给二次开发者。
    Matrix 明确区分行为合同、系统生命周期、Experience activation 和旧兼容边界；
    API 明示它是 declared evidence 而非 runtime health，插件不能自行上报通过状态。
    新增 Slot 如果没有稳定性匹配的证据会 fail closed。
    - [x] `artifact.renderer` 的系统安全实现与真实 `task-insights` 插件实现通过
      同一 `ArtifactRenderService` 行为套件，覆盖 generation pin、预览选择、来源
      hash、MIME/大小边界和附件 fallback；协议存在性检查不计为通过。
    - [x] `runner` 的系统 Console 实现与真实 `task-insights` 插件实现通过同一
      preflight、`TaskExecutionCoordinator`、Task/Run 终态和 Artifact/Evidence
      行为套件；`harness.runner` 的通过不能替代普通 `runner` Slot 的证据。
    - [x] 16 个 public Slot 均绑定真实系统实现、插件 fixture 和
      `tests/contract/os/` 行为测试；本轮整组重跑 27 项全部通过。
    - [x] `agent.factory` 保持 Host-only system lifecycle；`engine`、`tool`、
      `memory`、`scheduler` 明示为 migration-only，禁止伪报行为对齐。
    - [x] 5 个 `ui.*` Slot 仅标记 activation/projection 已覆盖，Task Workbench
      行为仍按 Chat-first 顺序后置，不计入运行语义完成项。
- [ ] Python 定点测试、pre-commit 与前端 Chat 定点测试通过。
  - [x] Console 全量 `tsc -b --noEmit` 已恢复为 0 error；SSE、Chat durable
    admission/reconnect、Harness capability fixture、UI Contribution activation 与
    Task Strategy fallback descriptor 已对齐当前稳定契约。相关行为定点回归
    `207 passed`，Task 仅修类型漂移，未继续页面开发。
- [ ] 真实固定 Chat 先 `/clear`，再逐模块完成可见验收。
  - [x] STRICT internal tool 不再绕过 Governance；请求级 execution level 不修改
    共享 Policy，批准后工具执行并完成对话。
  - [x] 同一 Invocation 的两个并行 Tool Approval 同时持久化并显示；分别批准时
    未决项继续阻塞，全部处理后工具批次与 Submission 正常完成。
  - [x] Approval 拒绝形成终局 Tool 结果且不重试；审批等待时 Interrupt 同时清理
    Runtime owner、Submission 和 blocking Interaction，不遗留 waiter。
  - [x] `AskUser` 与 Approval 使用同一 Interaction 投影并按顺序阻塞；
    `SuggestUserAction` 非阻塞，Runtime 终态后仍保留到用户处理。
  - [x] 插件 1.0.0 安装、1.1.0 热替换、错误 2.0.0 发布前回退及卸载均在同一
    后端进程完成；浏览器分别观察到旧实现结果与 `PLUGIN_HOT_V2`，失败替换后
    再次调用仍命中 1.1.0，证明不是只检查管理 API 或组件存在。
  - [x] 真实插件慢工具处于 `running` 且 Submission active 时提交 Steer；工具先
    提交不可变结果，回执再以 `after_tool_batch` 转为 `applied`，Chat 写入可见
    Steer 消息并继续生成 `STEER_AFTER_TOOL_BATCH_OK`。已 offload 的后台工具没有
    active Invocation 时明确拒绝 Steer，不混淆两种取消语义。
- [ ] 架构文档、API 规范、迁移表和未覆盖边界同步更新。
- [ ] 完成主要功能 Code Review，Blocking finding 为零。
- [ ] 达到门禁后再恢复 Task Workbench 开发。

## 5. 实施节奏

每个模块按同一节奏独立闭环：

1. 盘点旧生产者、消费者和状态所有权。
2. 冻结 Kernel Port、schema、错误和生命周期。
3. 实现系统 Adapter，并让 Chat 走新路径。
4. 接入 Plugin Contribution 与 generation。
5. 保留最小兼容桥，记录弃用与删除条件。
6. 运行定点契约测试和固定 Chat `/clear` 实测。
7. 更新状态矩阵后进入下一个模块。

当前执行点：I0、I1 已完成。Kernel 递归纯度门禁、SlotContract Registry、统一
Session 回滚和抗二次取消清理已经通过定点验证。I2 审批状态机已完成首个收口：
并发决策只提交一次，等待取消与批量取消均通过 durable resolution hook，带持久化
投影的审批不会被内存 GC 静默删除；Tool、Governance Tool、Driver、Codex 与
Qoder 已复用同一 Task bridge。后续又将 ReMe 副作用命令和
Codex/Qoder Harness 收敛到 Task + Interaction 双桥，所有上述路径共用
`ExecutionContract.timeout_policy.approval_seconds`，并按 broker 创建时间
持久化 `expires_at`。任一桥接失败都 fail closed。PawApp
`UIBridge.confirm()` 现已持久化由 `ChatSpec.id + invocation_id` 归属的通用
`USER_INPUT` Interaction；SSE 只作为投递 Adapter，Task 取消会关闭该 invocation
的未决交互。它仍是应用确认，不冒充策略 Approval 事实。浏览器断线重连与热替换
验收仍待完成。Task Workbench 不参与当前开发验收。
I7 已建立插件激活门禁：公共执行 Slot 在 shadow generation 阶段校验公开 Port
和 namespaced identity，UI Slot 校验非空 entrypoint；兼容 Slot 保持最小校验。
任何契约、身份或健康检查失败均在原子发布前终止，当前 generation 及其 lease
保持可用。
Side Effect 已新增独立 projection 与 task-scoped 幂等键；同一 tool-call 成功结果
直接 replay，prepared/failed/uncertain 记录默认阻止盲目重放，Policy 决策、
Approval ID、Invocation/Correlation 与执行结果摘要可在同一记录中关联。
I2 状态机随后补齐进程丢失恢复：审批边界成为可恢复 Checkpoint；Task API 在
发现 durable approval 已失去进程内 waiter 时返回 `task_runtime_lost`，不会提交
一个无人消费的“批准”；`side_effect_recovery_required` 与显式 retry authorization
构成恢复前安全门。插件与内置 Runner 现已通过同一个 run-scoped Checkpoint Broker
主动保存安全 cursor；Runner 失败后新 attempt 可读取该 checkpoint，不要求先把 Run
伪装成 suspended。
I3 已完成 Execution Contract 类型冻结及 Task、Proposal/Sensor、Chat 后台入口
贯通；契约随 Task 持久化，经 TaskOrder 进入 RuntimeContext。Scheduler 已拥有
统一 Port、SQLite Store 与 generation-pinned Task Dispatcher；Heartbeat 和可无损
表示的 final/silent agent Cron 已迁移，其他 Cron 类型仍保留显式兼容路径，因此入口
对齐尚未全部完成。Execution Event 已增加向后兼容的
step/source/cause/correlation 字段，Runner、Approval 和 Side Effect 新事件开始写入
已有身份；每个 Run 建立根 correlation，恢复不会更换业务链路 identity，Ledger
拒绝伪造或倒序的直接原因。持续时间、Token、工具调用和重试预算已进入真实
执行边界；Console `turn_usage` 和 Tool Call 被转为 durable usage event，Workbench
Projection 返回跨 attempt 累计值。Cost delta 已可执行，但缺少价格信息的 Provider
现在显式记录 `cost_unknown`；带成本上限的 Run 会在 Provider 启动前失败关闭，
不再伪装为可计量。并行度已进入 Tool Coordinator 的实际 handler 边界，
后台 offload 在真实完成前不会释放槽。Lite 的跨 HTTP subagent/fork 已通过
host-issued opaque scope 解析回根 Usage Meter，子孙共享并发槽与累计账本；根先
结束时，后台子租约仍保持 scope 存活。Workstation/Hub 的跨进程 scope registry
仍需通过部署适配器落地，不把 Python 对象或可伪造的预算值放入 HTTP。Required
Exit Condition 已进入 completion gate：Acceptance、Artifact 和显式 Signal 从完整
Ledger 投影求值，最大迭代边界同时覆盖 Console 与发布 iteration signal 的插件
Runner；optional 条件已在结果信号持久化后通过宿主 safe completion gate 主动停止，
不会绕过 required Artifact、Evidence 或 Verification。
I4 已建立 Artifact Registry、Acceptance Verification 与 Result Package：Artifact
具备版本、状态和显式 supersedes，Evidence/Verification 引用会在投影时做归属与
完整性校验，`run.completed` 只有在必需产物和验证策略满足后才能提交。Artifact、
Evidence 与 Verification 的 provenance 已由宿主事件信封派生并共享根 correlation；
Workbench 通过完整分页回放读取结果，避免默认事件窗口截断长期任务；结果包只包含
引用，不复制内容。I4 剩余项是 Workstation/Hub 数据库与对象存储 Adapter contract。
API 迁移已完成 Create/List/Detail、Start/Cancel/Resume、Projection/Artifact、
Approval，以及 Event Page/SSE 批次；现有 `/api/tasks` 保持唯一公开入口，内部按
`HTTP -> Application -> Kernel Port` 渐进替换。Approval 的持久化决策、进程内
Runtime 唤醒、并行取消和 Proposal 调度已成为同一应用用例；Event Stream 的
回放、游标和终态退出也不再由 FastAPI route 判断。Sensor Poll 同样由 Application
绑定 Agent identity 并调用 approval-gated Contribution Host，不建立平行 `/v2`
事实源。I3A 已移除 Conversation/Tool/Approval 的 route 私有投影逻辑，并将
Registry、Edition、Supervisor、Application 与 Proposal 执行统一迁入
`TaskApplicationHost`。Cron 与 Heartbeat 也已复用该 Host，不再创建第二套
Supervisor/Orchestrator。Artifact ownership、Store lookup、完整性读取、预览与
Renderer generation，Capability Catalog lease，以及 Side Effect 恢复命令均已进入
Application Service；Task route 不再直接访问 Runtime、TaskService、Ledger 或
Artifact Store。I3A 的内部边界迁移已完成，后续只允许新增协议适配，不得把业务
编排重新放回 HTTP route。
I3B 审计确认 Queue、Steer、Interrupt 尚未形成统一服务端控制面：旧 Channel 队列
是非持久化的按优先级并行队列，Chat Interrupt 分散在 TaskTracker、Tool
Coordinator 与 Runtime cancel-save，Steer 尚不存在；浏览器队列反而承担了发送
所有权和恢复。后续先冻结 Invocation Control Port 与状态机，再让 Chat 切换；在
该模块完成前，不把前端队列现状计为 OS 基建完成，也不开展 Task 页面开发。
I3B 第一批实现已经冻结不可变 Kernel 模型、命令载荷互斥规则、Submission 状态
转换表和 `InvocationControlPort`。Lite SQLite Adapter 已以服务端事务分配 Queue
sequence/revision，支持提交与控制命令幂等、乐观并发、重启读取和待应用命令恢复；
已建立 workspace live mailbox、AgentScope reasoning middleware、批次级 tool
bridge 与 SQLite 恢复型 dispatcher。Console 只服务端注入 `ChatSpec.id`
作为 Conversation identity，Message identity 作为 submission 幂等键；
Runtime 会提交 active/terminal Submission 事实。持久化 accepted Steer
只在真正写入 Agent 上下文后转为 applied。
Codex/Qoder 的旧 Workspace 旁路已接入同一 Invocation Control：每次 Harness turn
在有 OS identity 时创建 durable Submission、绑定精确取消回调，并在成功、失败或
中断后提交唯一终态和释放 blocking Interaction。适配器层的主动中断与 Runtime
task cancellation 双向收口，相关 57 项 Harness 定点测试及静态检查已通过；真实
外部进程和浏览器路径仍保留为 I8 验收项。
Chat Control HTTP Adapter 已把 Queue 查询及五类控制意图直接投影为 Kernel
`QueueProjection/ControlReceipt`，写操作强制携带客户端观察到的 revision 与幂等键；
过期投影、重复键冲突和跨 Chat 目标均由服务端关闭失败。
`ConversationRuntimeProjection` 已把 Queue 与 open Interaction 聚合为稳定 cursor
的当前状态 SSE；断线可恢复最新完整快照，中间审计历史仍由 durable ledger 承担。
QwenPaw Chat 已迁出浏览器 admission：首轮发送先分配真实 `ChatSpec.id`，运行中的
后续输入直接进入 workspace Submission Dispatcher；页面只订阅、取消和排序服务端
投影。旧本地队列仅保留历史草稿恢复和外部 backend 兼容，不再接收新的 QwenPaw
Submission。

2026-09-28 真实浏览器验收使用 Chat
`22642923-1fe3-4749-9b05-4ef587951eb1`：第一轮阻塞于 Ask User 时，服务端连续接收
两条 queued Submission，页面显示“消息队列 · 2”；从 UI 取消第二条后立即变为 1，
刷新页面仍只恢复第三条。回答 Ask User 后第三条自动执行并返回 `THIRD_DONE`，最终
Queue revision 10、active 和 Interaction 均为空。请求扩展
`plugin_option.format=brief` 在 durable input envelope 中保留；伪造保留身份字段
则由 HTTP 契约返回 422。

2026-09-29 补齐 QwenPaw 首轮浏览器验收：从空白 `/chat` 以
`Ctrl/Command+Enter` 提交后，先导航到服务端分配的 Chat
`54d10df6-2195-4c89-92e1-bd4d2ca679d8`，再进入 durable Submission 管线。
最终回复为 `FIRST_TURN_DURABLE_OK`，Runtime Projection revision 4 且
active/queue/interaction 均为空；页面刷新后用户消息、回复和 Fork 入口均
从持久化历史恢复。详细证据见
`docs/design/qwenpaw-invocation-control.md` 第 6.9 节。

2026-09-29 审批 deadline 复验：Policy Guard、ReMe 与 Codex/Qoder
Harness 的定点契约测试已验证“创建超时 = 等待超时 = durable
`expires_at`”，以及 Task/Interaction 双桥 fail-closed。真实
`tool_safety=True` Cron 复验在 90 秒内停留于模型生成阶段，未产生
Tool Call，因此不将该次运行记为真实 Approval E2E 通过。

2026-10-01 完成 Chat 工具输出的宿主 Artifact/Evidence 捕获。工具只声明受
Tool Guard 治理过的路径参数，Tool Coordinator 在成功结果之后读取固定输入，
生成内容寻址 `ArtifactRef`、`EvidenceRef` 和 Chat-owned opaque receipt；历史
恢复与 Console 成果卡只消费 canonical 引用。真实 Chat
`1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 通过 `write_file` 生成 Artifact
`f570158f-8658-4e1c-ad76-f1eecc09f690`，固定 registry generation 11；内容端点
返回 200、`text/plain`、23 字节，响应 SHA-256 与 Artifact 引用一致，并携带
`nosniff`、sandbox CSP 和 renderer generation。后端相关定点测试 135 项、前端
成果卡测试 16 项通过。
