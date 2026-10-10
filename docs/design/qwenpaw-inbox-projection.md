# QwenPaw Inbox Projection Contract

## 定位

Inbox 是 Delivery 终态 Receipt 的可查询读模型，不是 Task、Approval、Artifact 或
Evidence 的事实源。普通 Timeline、reasoning delta 和 Runner 内部状态不直接进入
Inbox。

`InboxItem.item_id` 等于其 `delivery_id`，所以相同 Delivery 重放只产生一个 Item。
Item 保存 source event、Task、Run、Conversation、Artifact 与 Evidence 引用，以及
最多 2000 字符的展示摘要。`source_payload` 只保存来源事件声明的 JSON 业务载荷，
不复制完整 Delivery payload、执行历史或 Artifact 内容；重放时该载荷也属于不可变
来源事实，不能静默改写。
Conversation 归属在公共模型中只使用可选 `chat_id = ChatSpec.id`。SQLite 历史
`data` JSON 中的 `conversation_id` 由模型兼容读取，重新输出时只产生 `chat_id`；
不改变 Item ID、Delivery ID、revision 或数据库表结构。
并发重放若观察到相同 Delivery 正由另一 owner 执行，会读取持久化 attempt 并等待其
终态 Receipt，而不是创建第二次投递或把暂态误报为失败。

## 状态与并发

- Delivery 的后续显式 attempt 可以推进同一 Item 的投递状态，但不能更换源身份。
- `read_at` 与 `handled_at` 是 Inbox 独有状态，通过 `revision` 乐观并发更新。
- 标记已读、已处理或未来归档只能修改 Inbox Store；不得提交 Task Event、修改
  Approval、删除 Artifact/Evidence 或改变 Delivery Receipt。
- 所有查询和变更显式携带 `agent_id`。其他 Agent 的 Item 对调用方表现为不存在。

## Lite 实现

`SQLiteInboxProjectionStore` 使用 WAL、确定性主键与事务 CAS。`TaskDeliveryWorker`
可在 Delivery settle 后调用 `InboxProjectionPort.project`。当前统一 Cron final/silent
管线已接入该 Store；同一 scheduled slot 重放会复用 Task、Delivery 与 Inbox Item。
不属于 Task 的宿主或插件工作先提交不可变 `OperationalEvent`，再通过相同 Delivery
Dispatcher 和 Inbox Projection 发布通知，禁止直接把通知当作事实源写入 Inbox。

Skill Auto Sync / Auto Update、Mail Monitor 与 ReMe 后台结果已迁入
`OperationalEvent → Delivery → Inbox`，相同事实重放不会重复产生来源事件或通知。
Memory backend 通过 `MemoryBackendContext.operational_event_publisher` 使用 Host Service，
不导入 App Store，第三方 backend 可复用同一出口。旧 `app/inbox_store.py` JSON 列表
暂时保留为历史 Mail / Skill / Heartbeat / ReMe / Cron 数据的回滚来源。Cron 与
Heartbeat 的旧执行回退也已改用 Operational Delivery；仓库内已没有旧 JSON Store 的
生产调用。

Workspace 启动时运行可重放的 `LegacyInboxMigration`：每个旧行先以原 ID 提交为
`qwenpaw.legacy.inbox` Operational Event，再进入统一 Delivery 与 SQLite Projection。
迁移保留来源 `status`、时间、已读状态和业务 payload；单行失败不会中断其他行，且
旧 JSON 文件不被修改或删除。Console 只有发现带相同 `legacy_event_id` 的新投影后才
抑制旧行，因此迁移失败仍可见，handled 的新投影也不会因旧文件仍在而重新出现。

## Console 兼容

Console Inbox API 已合并 SQLite Projection 与旧 JSON 来源，统一排序、分页和未读计数。
新 Item 保留来源提供的 `source_type / source_id / event_type / source_status /
severity / title / source_payload`；未提供来源元数据的 Task Delivery 才使用兼容
默认值。标记已读路由到各自 Store，删除新 Item 只写入 `handled_at` 并从默认列表
隐藏，不物理删除 Delivery 或源事实。旧 JSON 的“全部已读”也已按 Agent 收口。

Legacy Inbox Migration 每次启动扫描都会在同一 `inbox.db` 的独立表中写入 agent-scoped
观察摘要，不修改旧 JSON，也不把观察状态混入 `InboxItem`。摘要包含观察期起点、最近
扫描时间、扫描次数、累计与最近的 attempted/migrated/failed、源内容指纹、连续稳定
无失败扫描数，以及脱敏后的最近错误。即使 Operational Delivery 初始化在逐行迁移前
失败，也会留下扫描级错误；空源文件不会因此被误判为成功。

`GET /api/console/inbox/migration-observation` 返回只读评估。默认关闭双读门槛为：观察
时间至少 7 天、同一源指纹至少连续 3 次完整无失败扫描、最近扫描没有行级失败或扫描级
错误。门槛只表示可以停止 Console 旧源双读，不授权删除旧 JSON；物理删除仍必须完成
备份、归档和恢复演练。

2026-09-29 在本地 8004 真实服务读取该端点返回 HTTP 200。默认 Agent 已跨服务自动
重载累计 7 次相同空源指纹的干净扫描，`stable_clean_scans=7`，但观察期起点仍为当日，
因此 `can_disable_dual_read=false`，唯一 blocker 是
`observation_window_incomplete`。这验证了扫描次数不能绕过时间门槛；未修改系统时间，
也未伪造 7 天观察期。

## 尚未完成

- Approval、Artifact Ready 与 Exception 的产品级筛选策略。
- 真实运行至少 7 天并积累 3 次稳定干净扫描；当前只实现可持久化观测与自动门禁，
  尚未达到时间条件。
- 旧 Inbox JSON 的备份、归档、恢复演练和最终物理删除授权。
- Workstation/Hub 的共享数据库与租户授权实现。
