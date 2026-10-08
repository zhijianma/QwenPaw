# QwenPaw Scheduler Contract

- 状态：Kernel API、公开 SDK、Lite SQLite Store、system Contribution、
  `ScheduleFire → Task Runtime` Dispatcher，以及 Cron/Heartbeat 的可无损
  Trigger/Delivery 迁移已实现；其余旧 Cron 类型保留显式兼容路径
- 产品基线：OS / Chat-first；Task Workbench 页面继续后置
- 非目标：在 Scheduler 中复制 Task Budget、Approval、Artifact 或 Delivery 状态机

## 1. 边界

Scheduler 只拥有四类事实：

1. `ScheduleDefinition`：何时、由哪个 Agent、Runner 和 Strategy 创建 Task。
2. `ScheduleFire`：某个计划时间点产生的一次幂等触发。
3. `ScheduleLease`：哪个本地或远端 Worker 拥有该 Fire，以及租约 revision。
4. Fire 与其创建出的 `Task.id`、可选 `Run.id` 的绑定结果。

Task 的目标、预算、重试副作用、审批、取消、Artifact、Evidence 和恢复仍由现有
`ExecutionContract` 与 Task Runtime 拥有。Delivery/Inbox 只能读取 Task 结果投影，
不能通过“已读”或“已发送”反向修改 Task、Run 或 Schedule Fire 事实。

## 2. 稳定模型

### `ScheduleTrigger`

- `kind=cron`：只允许 `cron`。
- `kind=once`：只允许 aware `run_at`。
- `kind=interval`：要求大于 0 的 `interval_seconds`，并允许可选的 aware
  `start_at/end_at` 保存首次执行锚点与终止边界；`end_at` 不得早于 `start_at`。
- `timezone` 始终显式存在；Kernel 不依赖 APScheduler，也不解释具体 Cron 方言。

### `ScheduleDefinition`

定义包含稳定 `schedule_id`、Agent、目标、Trigger、Planner、Runner、可选 Strategy、已有
`ExecutionContract`、`RetryPolicy`、并发上限、misfire grace 和公开 metadata。
它不包含 Python callback、APScheduler Trigger 或 Channel 实例。

可选 `conversation_id` 是稳定 Chat 绑定，必须来自已有 `ChatSpec.id`。它通过
`RuntimeLaunchConfig` 进入 Task Runtime；未绑定的后台 Schedule 保持为空。Scheduler
不创建、猜测或以 Task ID 代替 Conversation，Conversation 的创建与权限校验属于
宿主适配器。

宿主可在 Definition metadata 写入已验证的 `approval_level`；Dispatcher 负责把它
转换为 `RuntimeLaunchConfig.approval_level`。缺失时才使用 Agent Profile，避免无人值守
迁移进入统一 Runtime 后悄悄改变 Tool Guard 行为。

可选的 `model_selection` 使用 Kernel `ModelSelection(provider_id, model)`
表达，并在 Fire 创建 Task 时同时写入 Task metadata 和
`RuntimeLaunchConfig`。因此重启恢复、新 attempt 和兼容 Console Adapter 均使用
同一个已固定模型；旧 `model_slot_override` 只在 Cron 边界被解析，不进入
Kernel 或插件 SDK。缺少 provider/model 的值失败关闭，不回退到当前默认模型。

`schedule_id` 只在一个 Agent 内唯一。目录、删除、Fire claim 和过期恢复都必须显式
携带 `agent_id`，不能依赖首次创建 Scheduler 的 Workspace 或全局当前 Agent。

### `ScheduleFire`

Fire 显式包含 `agent_id`。一次计划时间只产生一个稳定 `idempotency_key`。重试增加
`attempt`，但必须沿用同一逻辑 Fire 身份与幂等键，不能重复创建 Task。完整幂等域是
`agent_id + schedule_id + idempotency_key`。Trigger Adapter 在创建 Fire 时写入
`registry_generation`；claim、Task 创建和终态绑定都使用该 generation，插件热替换
不能把运行中的 Fire 偷换到新实现。

`fire_id` 由 `agent_id + schedule_id + idempotency_key` 确定性生成。进程重启后即使
`fired_at` 不同，只要逻辑发生事实相同，Store 就返回原 Lease，而不是误判为冲突。

### `ScheduleLease`

状态只有 `claimed / completed / failed`：

- `claimed` 不允许 Task、错误或完成字段。
- `completed` 必须绑定 `task_id`，可选绑定 `run_id`。
- `failed` 必须记录 `error_code`，可选 `retry_at`，不得伪称创建过 Task。
- 所有 renew/terminal mutation 都使用 `owner_id + expected_revision`。
- 租约过期只表示可以由恢复器判定失败或重新 claim，不代表 Task 被删除。

## 3. `SchedulerPort`

公开 Port 由 `qwenpaw.plugins.sdk` 导出：

- `upsert/remove/list_definitions` 管理纯 Schedule 定义。
- `claim` 对幂等 Fire 原子取得或返回已有租约。
- `renew` 以 revision CAS 延长仍由当前 Worker 拥有的租约。
- `complete` 只在 Task 创建成功后绑定 `task_id`。
- `fail` 记录有界错误与可选重试时间。
- `recover_expired` 返回已经终态化的过期租约，调用方再依据 RetryPolicy 决定是否
  创建下一 attempt。

公开 Contribution Slot 为 `scheduler`，process 生命周期，fail closed。插件只能从
`qwenpaw.plugins.sdk` 导入这些契约。

## 4. 迁移顺序

1. [已完成] 实现 Lite SQLite Scheduler Store 与 CAS 租约。
2. [已完成] 发布 `qwenpaw.system.tasks.local-durable-scheduler` system
   Contribution；它使用应用级数据库，不闭包绑定首次请求的 Workspace。
3. [已完成] `ScheduledTaskDispatcher` 在一个固定 generation 内解析 Scheduler、
   Planner、Runner 和 Strategy；同一 Fire 只创建一个 `TaskSource.SCHEDULE` Task，
   Task metadata 保存 schedule、fire、幂等键和 generation，Runtime 再独立持有同一
   generation lease 直到运行终态。Task 已绑定但 Runner 尚未启动时，相同 Fire
   回放会恢复该 Task，而不是等待 Delivery 超时；并发恢复只产生一个 Run。
4. [已完成] APScheduler 只作为 Trigger Adapter；已迁移 Cron 与 Heartbeat 回调使用
   捕获的真实 `scheduled_for` 生成稳定 `ScheduleFire` 并 claim，手动触发使用独立键。
5. Agent Cron 将 Fire 转成 `TaskSource.SCHEDULE` 的 Task/Run，不再调用
   `workspace.stream_query()`。
6. [已完成] Heartbeat 使用同一 Port；`DeliveryPolicy.suppress_exact_text` 将
   `HEARTBEAT_OK` 表达为“保留 Task 事实但不产生 Delivery/Inbox”。
7. 旧 text-only Channel 定时发送先保留兼容 Adapter；迁移完成后再设弃用门槛。

HTTP Task、Cron 与 Heartbeat 在同一进程中按 Capability Registry 复用同一个
`TaskApplicationHost`。因此三类入口共享 Workspace TaskService、Orchestrator 和
Supervisor；Host 在 generation 更新后刷新新事件使用的 generation，但已固定的 Run
仍由各自 lease 保持原版本。

当前 final/silent agent Cron（包含 per-job model）、repeating-once 与 Heartbeat
已切换到 Dispatcher。repeating-once 映射为带 `start_at/end_at` 的 interval；
`repeat_end_type=count/until/never` 均保留原语义。`tool_safety=True` 也已进入
Dispatcher：Definition 使用 AUTO Approval，审批 deadline 明确短于 attempt deadline，
审批请求和异常进入 Delivery/Inbox，silent job 不投递普通 Result。旧 Cron
`dispatch.mode=stream` 与 text-only 仍走兼容路径。其余类型只有在公共契约能无损
表达后才能迁移，禁止为了消除旧入口而丢失实时投递或审批语义。

每个旧 Cron 声明现在具有结构化 `CronRuntimeDecision`。Job 详情在首次执行前返回
当前判定；执行时 `CronManager` 使用同一个判定选择 `durable_task` 或
`legacy_executor`，并把判定保存到最近状态和 `CronExecutionRecord`。稳定原因码区分
text-only、尚未完成外部验收的 stream、缺失 request、无效模型选择和未安装
Runtime。旧的 `repeating_once_unsupported` 与 `interactive_tool_safety` 原因码只为
读取历史记录保留，新判定不再产生。旧的仅实现 `supports()` 的宿主仍可运行，
但会明确返回 `runtime_decision_unavailable` 或 `runtime_declined`，不能伪装成已完成
迁移。每个 fallback decision 都携带删除门槛，服务日志同时记录原因码。

stream 迁移已具备第一层公共事件基础：Console 与 Harness Runner 在终态提交
`conversation.assistant.completed`，Delivery 只从该事件产生 completed Reply；稳定
`tool.started / tool.completed` 映射为 Activity，reasoning 与 token delta 不外发。
image/audio/video/file 已在代码层完成“内联字节 → ArtifactRef → 所有权/完整性校验
→ Channel Message”的转换，Ledger 不保存 base64，嵌入媒体也不会重复产生 Artifact
Ready。进程内持久化 Ledger 到实际 ConsoleChannel 的链路已经通过；该验收还修复了
缺失 `object="message"` 造成 Channel 静默忽略的协议缺陷。浏览器和外部媒体
Channel 的上传/展示等价验收尚未完成，所以
`LiteCronTaskRuntime.supports(stream)` 仍为 false。这是显式迁移门禁，不是能力
探测遗漏。

## 5. 验收门禁

- 相同 `schedule_id + scheduled_for` 的并发 Fire 只创建一个 Task。
- Worker 崩溃后，过期 lease 可恢复；旧 owner/revision 无法完成新 lease。
- Task 创建后启动失败时，相同 Fire 从持久化绑定恢复执行；并发恢复只有一个
  Worker 返回 `recovered`，其他 Worker 回放同一 Run。
- 插件热替换后，已 claim Fire 保持原 generation，新 Fire 使用新 generation。
- Cron 与 Heartbeat 均产生 `TaskSource.SCHEDULE` 和统一 Runtime Projection。
- Inbox 已读、Delivery 失败或重试不改写 Task/Run/Artifact/Evidence 事实。

## 6. Lite SQLite Store

`SQLiteSchedulerStore` 使用独立 WAL 数据库实现 Kernel `SchedulerPort`。它是
Lite 宿主 Adapter，不从公共 Plugin SDK 导出：

- Schedule Definition 可更新或移除，但移除不会删除既有 Fire/Lease 历史。
- `(agent_id, schedule_id, idempotency_key)` 唯一；不同 Agent 可以使用相同
  `schedule_id`，相同 Fire 并发 claim 返回同一 Lease，相同键配不同 Fire 内容则
  抛出公开 `ScheduleFireConflictError`。
- renew/complete/fail 都校验 owner、revision、claimed 状态和未过期边界。
- terminal Lease 不可续租；Task 创建失败不会伪造 `task_id`。
- `recover_expired()` 在同一事务内把过期 claim 变成
  `failed / lease_expired`，由上层 RetryPolicy 决定下一步。
- 定义、Fire 和 Lease 均保存完整 canonical JSON，SQLite 辅助列只用于唯一约束与
  有界查询，不成为第二份领域真相。
- 旧单 Agent 表在首次打开时事务迁移到复合键结构；定义中的 `agent_id` 用于恢复
  历史 Fire 所有权，无法唯一推断时 fail closed，而不是把历史归到当前 Agent。

## 7. System Contribution

`qwenpaw.system.tasks` bundle 发布 `local-durable-scheduler` 的
`scheduler.provider` Contribution。应用组合边界创建
`WORKING_DIR/scheduler.db` 对应的 `SQLiteSchedulerStore`，再通过
`SchedulerStoreHost` 注入 Provider；Capability Registry 不构造或持有数据库路径。
Agent 隔离完全由 Kernel 复合身份和数据库约束保证。测试在 Host 侧注入临时 Store，
无需插件写入用户目录。

系统实现与插件实现通过同一个 `SchedulerProvider` 激活门禁。Provider 只能从
`SchedulerHost.scheduler_store()` 获得已准入的 `SchedulerPort`，公共 SDK 不导出
SQLite Adapter。声明 `scheduler.provider` 却未实现完整 Provider，或返回无效 Port
的插件，会在 generation 发布前 fail closed；旧 `scheduler` Slot 只保留迁移兼容。
