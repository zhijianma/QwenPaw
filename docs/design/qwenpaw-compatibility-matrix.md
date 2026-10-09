# QwenPaw OS 兼容入口与删除门槛

- 状态：执行中
- 范围：OS R0 基础设施迁移；Task Workbench 不在本矩阵实施范围
- 原则：兼容入口只能翻译旧契约，不能成为第二事实源

## 1. 状态定义

| 状态 | 含义 | 删除规则 |
|---|---|---|
| `active-adapter` | 新旧流量均可能经过，Adapter 只翻译身份或载荷 | 等价链路和回滚验证完成前禁止删除 |
| `legacy-fallback` | 新契约无法无损表达时仍承担生产执行 | 必须先补齐契约和真实验收 |
| `read-only-source` | 不再接收新写入，只用于迁移、回滚或历史读取 | 完成观察期、归档和恢复演练后删除 |
| `deprecated-api` | 保持兼容运行，但已有等价公开 API | 必须提供结构化迁移指引和使用观测 |
| `stable-transport` | 仅为 Channel/Provider transport 私有字段 | 不提升为领域身份，也不按弃用 API 删除 |

## 2. 兼容矩阵

| 入口或状态 | 当前状态 | 权威事实源 | 兼容层职责 | 当前可观察性 | 删除门槛 |
|---|---|---|---|---|---|
| QwenPaw Chat 提交 | `active-adapter` | `InvocationControlService` + SQLite Submission Queue | HTTP/Console 将消息与附件转换为版本化 envelope | Runtime Projection、Control Receipt、Queue revision | 固定 Chat 多标签 FIFO、浏览器关闭后继续、进程重启恢复全部真实通过 |
| 外部 backend Chat 队列 | `legacy-fallback` | 外部 backend 自身能力 | Console 保留旧本地队列，不冒充 OS Queue；仅已知外部 backend 的 `agentId` 可启动 compatibility sender，QwenPaw 与未知 Agent 的遗留项失败关闭 | Harness `conversation_queue` 能力位；实际 fallback 通过幂等收据写入 agent/backend-scoped SQLite 汇总；`/api/chats/compatibility/external-queue` 只读查询 | 外部 backend 公开 Conversation/Queue capability，通过相同隔离与恢复套件，并建立旧队列零使用观察窗口 |
| `/stop` 与旧取消入口 | `active-adapter` | Invocation cancellation root | 显式映射 `interrupt_current`；清队列必须走 `stop_and_clear` | Control Receipt、Invocation 终态；Workspace 启动即建立 7 天零使用窗口，旧 Console API 与 Channel `/stop` 只记录入口、处置和时间，`/api/chats/compatibility/legacy-stop` 返回 agent-scoped 删除门禁 | 所有客户端迁移到显式 `interrupt` / `stop-and-clear`，连续 7 天旧调用量为零，并由迁移负责人确认后方可删除 |
| 旧 Approval waiter | `active-adapter` | durable Interaction / Approval Broker | 唤醒旧 Future，不拥有审批状态；仅缺失 Interaction、Chat 或 Invocation 身份时保留 legacy-only wait | Tool、Governance Tool、Driver、Codex/Qoder Harness 与 ReMe 共用 Execution Contract deadline；Workspace SQLite 只记录 legacy-only 的 source、缺失身份原因、时间和次数；`/api/approval/compatibility/legacy-waiter` 返回 7 天删除门禁 | 固定 Chat 及真实 Scheduler Task 的批准/拒绝/超时/取消验收全部通过，进程丢失恢复无孤立审批，连续 7 天 legacy-only 命中为零，并由迁移负责人确认调用方已迁移 |
| PawApp `UIBridge.confirm()` | `active-adapter` | workspace `InteractionService`，以 `ChatSpec.id + invocation_id` 归属 | `pawapp:confirm_request` 仅投递兼容事件；响应进入通用 Chat Interaction API | durable request/resolution、revision、expires_at、Task cancel 联动；SDK 保留完整交互 envelope | 完成浏览器断线重连与热替换中的真实 PawApp 验收，并观察旧 request-only 客户端为零 |
| Cron `final` / `silent` agent job | `active-adapter` | Scheduler Fire + Task Ledger + Delivery Receipt | `CronManager` 仅选择新 Task Runtime 并回写旧 history | Task/Run、Delivery Receipt、Cron history | 保持兼容 history 期间不删；新管理入口替代旧 Job schema 后再评估 |
| Cron `stream` | `legacy-fallback` | 旧 Cron Executor | 保留实时外部 Channel 语义 | 公共 Reply/Activity 事件；每次使用写 Cron history，首次同 Job/decision/trigger 通过幂等 Operational Event 持久化迁移告警与删除门槛 | 浏览器与至少一个真实外部媒体 Channel 等价验收，且观察窗口内不再新增 fallback history 后启用新 Runtime |
| text-only Cron | `active-adapter` | Scheduler Fire + Delivery Attempt/Receipt | 固定文本直接进入 Delivery，不伪造 Task；旧 Job/history schema 保留 | `durable_delivery` decision、稳定 Delivery ID、重启与不确定态回放测试 | 新管理入口替代旧 Job schema 后再评估兼容 Adapter |
| repeating-once Cron | `active-adapter` | Scheduler Fire + Task Ledger + Delivery Receipt | 转换为带 `start_at/end_at` 的 interval，保留 count/until/never | 旧 `repeating_once_unsupported` 原因码仅供历史读取 | 新管理入口替代旧 Job schema 后再评估兼容 Adapter |
| `tool_safety=True` Cron | `active-adapter` | Scheduler Task + durable Approval + Delivery/Inbox | AUTO Guard；Host 契约给出短于 attempt 的 approval deadline；silent 只通知审批/异常 | 契约和定点测试已证明 deadline/`expires_at`；真实 Cron 复验在模型生成阶段超时，未触达 Tool Approval | 完成真实浏览器批准/拒绝/超时及外部通知验收，并观察旧原因码零新增 |
| 旧 Inbox JSON | `read-only-source` | Operational Event + Delivery + SQLite Inbox Projection | 启动时幂等重放；失败行继续从旧源可见 | SQLite agent-scoped Observation 保存起点、扫描/成功/失败、源指纹和脱敏错误 | 至少 7 天、同源连续 3 次完整无失败扫描；再完成备份、归档与恢复演练 |
| Console Inbox 双读 | `active-adapter` | SQLite Inbox Projection；旧 JSON 仅历史来源 | 统一排序、分页、未读和 handled 语义 | `/api/console/inbox/migration-observation` 返回只读关闭门禁 | Observation 允许关闭后，验证历史查询与未读计数；物理删除仍需额外授权 |
| 可替换的 `PluginApi.register_*` | `deprecated-api` | Contribution manifest + generation Registry | 旧调用继续注册，同时生成目标 Slot 迁移诊断 | `/api/plugins[].migration_diagnostics` + `migration_plan`；CLI 可输出去重 v2 manifest patch、action 和 blocker，不自动覆写源码 | 对应 API 使用量观察期为零，参考插件与文档全部采用公开 SDK |
| V1 frontend bundle | `legacy-fallback` | Console legacy frontend loader | 保留 menu/route/slot 历史自注册行为 | V2 `ui.*` 已由宿主激活事务校验声明、身份、完整性和替换回滚；V1 显式无该保证 | V1 bundle 迁移到 v2 UI Contribution，热替换/回滚套件通过并建立零使用观察窗口 |
| 无等价 Slot 的 Plugin Host API | `active-adapter` | Plugin Host lifecycle | HTTP router、安装/卸载 Hook、Inbound Channel、Model Provider、Skill Provider 继续由 Host 管理 | Plugin load/unload 日志与注册表 | 先冻结等价 public Slot；在此之前禁止标记为可替换或删除 |
| 旧 Task capability ID | `active-adapter` | namespaced Capability Registry | 解析时将已知旧 ID 映射到新系统 ID，观测不参与代际选择 | agent-scoped SQLite 命中计数、首末时间、7 天零使用窗口与 `/api/tasks/capabilities/compatibility` 替换建议 | 连续 7 天无旧 ID 命中，并单独确认所有持久化 Profile/Task 引用迁移 |
| 旧 Artifact 文件位置 | `read-only-source` | Artifact Registry + content-addressed Store | 只读 fallback，外部 API 仍校验 Task ownership | Artifact content API 与 registry metadata | 旧位置迁移完整、哈希校验通过、回滚恢复演练完成 |
| Channel `session_id` / `user_id` | `stable-transport` | `ChatSpec.id`、Task/Run/Invocation identity | 只在 Adapter address 中定位外部传输目标；插件 Host 以 `getCurrentChatId()` 暴露 Conversation，旧 `getCurrentSessionId()` 仅兼容 | Delivery Destination metadata；PawTask 默认携带 `X-QwenPaw-Chat-Id` | 不删除 transport 所需字段；禁止写回 Kernel 领域身份；二次开发文档与参考插件不再新增 `getCurrentSessionId()` 调用 |

## 3. 弃用告警规范

弃用诊断必须是结构化数据，不能只写日志。最小字段为：

- `code`：稳定机器码。
- `entrypoint`：实际命中的旧入口。
- `replacement`：等价新 Port、Slot 或 API；不存在时不得伪造。
- `reason`：为何仍走兼容路径。
- `removal_gates`：尚未满足的删除条件。

可替换的 Plugin 注册 API 与 Cron Runtime 选择已满足该规范。Cron Job 详情、最近
状态和执行历史会返回同一个 `CronRuntimeDecision`，并区分各类 fallback 原因和删除
门槛。旧 Inbox 双读已有持久化观察摘要和只读门禁，但尚未经历真实 7 天观察期，也未
完成归档恢复演练。旧 Stop 入口已由 Host-owned SQLite 观察器区分 Console API、
Channel 命令及 OS Interrupt、兼容取消与无活动运行；新 Control API 不计入旧入口命中，
观察幂等键只保存 SHA-256；报告不保存 Chat、Session、消息或用户内容。即使零使用
窗口完成，客户端迁移确认仍是
独立删除门槛。旧 Approval waiter 已对真正缺少 Interaction Service、`ChatSpec.id` 或
Invocation identity 的 fallback 建立独立观察窗口；完整 OS Interaction 路径不计入命中，
观察 ID 只保存 SHA-256，不落审批参数、Tool、Chat、Session 或用户内容。即使连续 7 天
无命中，旧调用方迁移确认仍是独立删除门槛。旧 Task capability ID 已记录真实解析命中，并提供精确的新 ID、
Slot 建议和可重置的 7 天零使用窗口；服务启动时持久化观察起点，任一旧 ID 命中都会
重置窗口。即使窗口完成，持久化引用迁移仍是独立阻塞条件。外部 backend Queue 已
冻结 `conversation_queue` 能力位，并在唯一 legacy admission 点记录不含消息内容和
session 身份的幂等诊断；当前 Codex/Qoder 均未发布该能力，也尚未建立零使用窗口，
因此本矩阵不能作为物理删除授权。

## 4. 删除决策

任何兼容入口只有同时满足以下条件才可移除：

1. 新链路能无损表达旧语义，并且旧入口不再产生新事实。
2. system 与 plugin 契约测试覆盖相同输入、终态、取消和失败恢复。
3. 固定 Chat 或真实 Channel 完成端到端验证，不能用 mock 页面代替。
4. 已定义观测窗口，窗口内旧入口调用量为零且迁移失败为零。
5. 已完成备份、回滚和恢复演练，并记录负责人和版本门槛。
6. 删除后不存在双写、双审批、双 Queue、双 Artifact 或双 Inbox 事实源。

## 5. 下一批实施顺序

1. 迁移旧 Task/Profile 中的 capability ID，并积累完整零命中观察窗口。
2. 外部 backend 发布 Conversation/Queue capability，通过隔离/恢复套件并完成旧队列
   零使用观察后，删除 Console 本地 admission。
3. 累积真实 Inbox 观察期并完成备份、归档和恢复演练。
4. 完成上述运行观测与真实验收后，才制定物理删除版本。
