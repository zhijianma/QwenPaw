# QwenPaw Scheduler Contract

- 状态：Kernel API、公开 SDK、Lite SQLite Store、system Contribution、
  Task/Service/Delivery Dispatcher，以及 Cron/Heartbeat/Service Cron 的可无损
  Trigger/Delivery 迁移已实现；其余旧 Cron 类型保留显式兼容路径
- 产品基线：OS / Chat-first；Task Workbench 页面继续后置
- 非目标：在 Scheduler 中复制 Task Budget、Approval、Artifact 或 Delivery 状态机

## 1. 边界

Scheduler 只拥有四类事实：

1. `ScheduleDefinition`：何时、由哪个 Agent 执行哪类 scheduled work。
2. `ScheduleFire`：某个计划时间点产生的一次幂等触发。
3. `ScheduleLease`：哪个本地或远端 Worker 拥有该 Fire，以及租约 revision。
4. Fire 与其创建出的 `Task.id`，或非 Task `completion_ref` 的互斥绑定结果。

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
- cron 可声明 `jitter_seconds + jitter_seed`。Host 使用稳定 seed 与 nominal
  occurrence 计算确定性偏移；重启不会重新抽样，interval/once 禁止携带 jitter。

### `ScheduleDefinition`

定义包含稳定 `schedule_id`、Agent、目标、Trigger、`work_kind`、Planner、Runner、
可选 Strategy、已有 `ExecutionContract`、`RetryPolicy`、并发上限、misfire grace 和公开 metadata。
它不包含 Python callback、APScheduler Trigger 或 Channel 实例。

`work_kind=task/service/delivery` 是 Host 路由边界，默认 `task` 以保持旧插件兼容。
`ScheduledTaskDispatcher` 对非 Task 失败关闭；Service callback 由专用 Host Dispatcher
执行，不能借用虚假 Task ID。固定文本由 Delivery Dispatcher 直接投递，也不创建
Task。

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
- Task 的 `completed` 必须绑定 `task_id`，可选绑定 `run_id`。
- 非 Task work 的 `completed` 改为绑定 `completion_ref`；它与 `task_id` 必须且只能
  二选一。
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

### Host-owned Trigger Cursor

时间计算属于 Host 基础设施，不交给 Provider 或插件持有。Kernel 的
`ScheduleTriggerCursor` 只保存 `agent_id + schedule_id`、完整 Definition hash、
`next_fire_at`、`last_fire_at`、`retry_not_before/retry_count` 和 revision；不保存
APScheduler 对象。Cursor Store
提供 reconcile、exact get、due query、revision CAS advance 和 remove：

- 同一 Definition 重启时保留原进度，不从当前时间重新猜测。
- Definition 内容改变时提升 revision 并按新 Trigger 重建进度。
- Worker 只有在处理一个 occurrence 后才能 CAS advance；崩溃后再次读取同一 due
  occurrence，并依靠 Schedule Fire 幂等键回放，不会丢任务。
- handler 异常或显式 `retry` 不提交 occurrence，而是以 revision CAS 写入持久化
  `retry_not_before`。due query 在冷却期不返回该 Cursor；重启后继续遵守
  `RetryPolicy.backoff_seconds`，并至少等待 Host 的一秒防热循环下限。成功、misfire
  或 Definition 变更会清零 retry 状态。
- 删除 Cursor 不删除 Definition 之外的历史 Fire/Lease；Worker 清理孤儿 Cursor
  也必须携带 definition hash + revision，避免与 catalog 重建发生删除竞争。

cron/once/interval 的 recurrence evaluator 是纯函数。cron 从当前时间计算下一次；
once 保留已经过去的时间点，让 Worker 根据 misfire grace 裁决；interval 的首次进度
对齐到不早于当前时间的周期边界，避免导入旧 `start_at` 时制造历史补跑风暴。
Cursor Store 是 Host-only Port，不从 Plugin SDK 导出；Scheduler Provider 仍只操作
宿主准入的 `SchedulerPort`。

## 4. 迁移顺序

1. [已完成] 实现 Lite SQLite Scheduler Store 与 CAS 租约。
2. [已完成] 发布 `qwenpaw.system.tasks.local-durable-scheduler` system
   Contribution；它使用应用级数据库，不闭包绑定首次请求的 Workspace。
3. [已完成] `ScheduledTaskDispatcher` 在一个固定 generation 内解析 Scheduler、
   Planner、Runner 和 Strategy；同一 Fire 只创建一个 `TaskSource.SCHEDULE` Task，
   Task metadata 保存 schedule、fire、幂等键和 generation，Runtime 再独立持有同一
   generation lease 直到运行终态。Task 已绑定但 Runner 尚未启动时，相同 Fire
   回放会恢复该 Task，而不是等待 Delivery 超时；并发恢复只产生一个 Run。
4. [已完成：兼容层] APScheduler Trigger Adapter 为 Heartbeat 与 legacy Cron 回调
   捕获真实 `scheduled_for`；migrated final/silent Cron 已在后续切换到 Cursor Worker。
   手动触发始终使用独立键。
5. [已完成：final/silent] Agent Cron 将 Fire 转成 `TaskSource.SCHEDULE` 的
   Task/Run，不再调用 `workspace.stream_query()`。创建、更新、暂停、恢复、删除与
   Workspace 启动恢复也会在首次 Fire 前同步 durable catalog；stream 仍属于具名
   兼容路径。
6. [已完成] Heartbeat 使用同一 Port；`DeliveryPolicy.suppress_exact_text` 将
   `HEARTBEAT_OK` 表达为“保留 Task 事实但不产生 Delivery/Inbox”。
7. [已完成：基础设施] Host-owned durable trigger worker 按 Agent 有界读取 due
   Cursor，执行前裁决 definition 变更与 misfire，只有处理成功或明确跳过后才 CAS
   advance；handler 通过 `handled/retry` 显式回执区分“失败已记账”和“基础设施暂态
   失败”，未捕获异常等同 retry。Worker 的 catalog 读取通过同一 generation-pinned
   Scheduler Provider，不能绕过插件/内置统一契约。并发 Worker 由下游 Fire Lease
   保证副作用幂等、由 Cursor CAS 选出唯一进度赢家。不同 Schedule 在同一 tick
   并发消费，单个 Cron 的并发上限仍由 Manager semaphore 约束。retry 退避由 Cursor
   持久化，不依赖进程内 sleep 或轮询频率。
8. [已完成：service] ReMe 等 workspace service 声明使用
   `work_kind=service`，启动时同步 catalog/cursor，迁移成功后移除 APScheduler
   唤醒。每次 occurrence 先 claim Fire Lease；长 callback 自动 renew，成功写稳定
   `completion_ref`，异常写 failed，进程崩溃留下的过期 lease 标为
   `lease_expired` 且不自动重放不确定副作用。callback registry 仍由 Host 在重启时
   从 service 声明重建，Python callback 不进入 Kernel 或 SQLite。
9. [已完成：text-only] 固定文本使用 `work_kind=delivery`。Fire 先绑定稳定
   `delivery_id`，再由 DeliveryAttempt 执行外部发送；进程重启可从绑定结果继续，适配器
   异常记为 `uncertain` 且不自动重复可能已经发生的发送。它不创建 Conversation、Task
   或 Run。

HTTP Task、Cron 与 Heartbeat 在同一进程中按 Capability Registry 复用同一个
`TaskApplicationHost`。因此三类入口共享 Workspace TaskService、Orchestrator 和
Supervisor；Host 在 generation 更新后刷新新事件使用的 generation，但已固定的 Run
仍由各自 lease 保持原版本。

当前 final/silent agent Cron（包含 per-job model）、text-only、repeating-once 与 Heartbeat
已切换到 Dispatcher。repeating-once 映射为带 `start_at/end_at` 的 interval；
`repeat_end_type=count/until/never` 均保留原语义。`tool_safety=True` 也已进入
Dispatcher：Definition 使用 AUTO Approval，审批 deadline 明确短于 attempt deadline，
审批请求和异常进入 Delivery/Inbox，silent job 不投递普通 Result。旧 Cron
`dispatch.mode=stream` 仍走兼容路径。其余类型只有在公共契约能无损
表达后才能迁移，禁止为了消除旧入口而丢失实时投递或审批语义。

每个旧 Cron 声明现在具有结构化 `CronRuntimeDecision`。Job 详情在首次执行前返回
当前判定；执行时 `CronManager` 使用同一个判定选择 `durable_task`、
`durable_delivery` 或 `legacy_executor`，并把判定保存到最近状态和
`CronExecutionRecord`。稳定原因码区分尚未完成外部验收的 stream、缺失 request、
无效模型选择和未安装 Runtime。旧的 `text_delivery_only`、
`repeating_once_unsupported` 与 `interactive_tool_safety` 原因码只为读取历史记录保留，
新判定不再产生。旧的仅实现 `supports()` 的宿主仍可运行，
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
探测遗漏。每次实际 fallback 都继续写带结构化 decision 的 Cron history；同一
Job/decision/trigger 的首次使用还会通过幂等 Operational Event 投影持久化警告、
稳定原因码和 removal gates。告警投影失败不会阻止旧生产执行，但会留下错误日志。

Cron 的 JSON 声明、APScheduler 唤醒器与 Scheduler catalog 现在由
`CronManager` 作为一个迁移事务协调：新声明只有在 catalog 同步成功后才提交；更新
失败恢复旧声明；删除底层仓库拒绝时恢复 catalog；启动恢复失败会把 job 持久化为
disabled 并移除 APScheduler job。切换到 legacy path 时会主动删除确定性的 Kernel
schedule definition，但不会删除既有 Fire/Lease 历史。final/silent Agent Cron、
text-only、Heartbeat 与 service Cron 已由
独立 durable polling lifecycle 消费 Cursor，不再向 APScheduler 注册同名 job；
stream job 仍保留原 APScheduler 兼容路径，因此父
迁移项尚未完成。

## 5. 验收门禁

- 相同 `schedule_id + scheduled_for` 的并发 Fire 只创建一个 Task。
- Worker 崩溃后，过期 lease 可恢复；旧 owner/revision 无法完成新 lease。
- Trigger handler 暂态失败在持久化冷却期内不会被轮询热循环；重启后仍按原
  occurrence 重试，并发 Worker 只有一个 retry CAS 胜者。
- 第一进程提交 retry Cursor 后被强制终止，第二个独立 Python 进程打开同一 SQLite
  数据库：冷却到期前不调用 handler，到期后只处理原 occurrence，并清零 retry 状态。
- Task 创建后启动失败时，相同 Fire 从持久化绑定恢复执行；并发恢复只有一个
  Worker 返回 `recovered`，其他 Worker 回放同一 Run。
- 插件热替换后，已 claim Fire 保持原 generation，新 Fire 使用新 generation。
- Cron 与 Heartbeat 均产生 `TaskSource.SCHEDULE` 和统一 Runtime Projection。
- Service Cron 不创建伪 Task；并发 occurrence 复用同一 Fire Lease，成功绑定
  `completion_ref`，失败与过期保留终态证据。
- Text Cron 不创建伪 Task；Fire 绑定稳定 Delivery，重启和重复触发不会重复发送，
  失败与不确定结果由 DeliveryReceipt 保留。
- Inbox 已读、Delivery 失败或重试不改写 Task/Run/Artifact/Evidence 事实。

## 6. Lite SQLite Store

`SQLiteSchedulerStore` 使用独立 WAL 数据库实现 Kernel `SchedulerPort`。它是
Lite 宿主 Adapter，不从公共 Plugin SDK 导出：

- Schedule Definition 可更新或移除，但移除不会删除既有 Fire/Lease 历史。
- `(agent_id, schedule_id, idempotency_key)` 唯一；不同 Agent 可以使用相同
  `schedule_id`，相同 Fire 并发 claim 返回同一 Lease，相同键配不同 Fire 内容则
  抛出公开 `ScheduleFireConflictError`。
- renew/complete/fail 都校验 owner、revision、claimed 状态和未过期边界。
- terminal Lease 不可续租；Task 创建失败不会伪造 `task_id`，非 Task 完成也不能
  同时填写 `task_id` 与 `completion_ref`。
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
同一 SQLite Adapter 还实现 Host-only Trigger Cursor Store，使 Definition、Cursor
与 Fire Lease 位于同一 WAL 数据库，但 Provider 看不到 Cursor 生命周期 API。
