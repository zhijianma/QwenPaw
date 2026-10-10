# QwenPaw Delivery Contract

- 状态：Kernel 请求/回执/attempt、公开 Plugin SDK、`delivery.adapter` Slot、
  Lite SQLite Projection、generation-pinned Dispatcher、Task Event Projector 与
  system Channel/Inbox Adapter 已实现；final/silent Cron 与 Heartbeat 已接线；
  stream 的已提交 Reply/Tool Activity 与媒体 Artifact 契约已实现，但 Cron 切换
  已通过进程内真实 ConsoleChannel 链路，Cron 切换仍等待浏览器和外部 Channel 验收
- 定位：核心投影契约 + 可替换外设 Adapter
- 非目标：让 Delivery 拥有 Task、Approval、Artifact 或 Inbox 的领域真相

## 1. 不变量

1. 只有已经提交到事实源的事件才能产生 `DeliveryRequest`。
2. Delivery 失败、重试、抑制和已读状态不能改写源事件。
3. `DeliveryRequest.source_event_id + destination + idempotency_key` 决定一次逻辑
   投递；Adapter 重试只增加 attempt，不创建新的业务事实。
4. 核心契约只使用 `ChatSpec.id` 作为 Conversation 身份，不包含渠道
   `session_id`。外部频道地址封装在 `DeliveryDestination.address`，由对应 Adapter
   解释。
5. Artifact 只以不可变 `ArtifactRef` 传递，Delivery 不复制或修改内容。
6. `silent` 是明确的投递模式，必须产生 `suppressed` Receipt；不能用“没有记录”
   冒充静默成功。
7. Request 保留源事件的 `invocation_id / correlation_id`；Channel Message
   metadata 与 Inbox Projection 只读传递，不产生新的因果身份。

## 2. Kernel 模型

`DeliveryRequest` 是源事实的不可变投影：

- `source_event_id`：已经提交的 Execution、Interaction 或 Artifact 事件。
- `kind`：`reply / result / approval / activity / exception / artifact_ready`。
- `mode`：`stream / final / silent`。
- `agent_id`、可选 `chat_id / task_id / run_id`：稳定业务归属；`chat_id` 是
  `ChatSpec.id`。旧 `conversation_id` 只兼容读取，不进入新 JSON/schema。
- 可选 `invocation_id / correlation_id`：源事件的不可变因果链身份。
- `destination.adapter_id + address`：外设路由，不暴露 transport session。
- `artifact_refs`：只读内容引用。
- `idempotency_key`：跨崩溃重放使用的稳定键。
- `DeliveryPolicy.suppress_empty_text / suppress_exact_text`：只对完整 final result
  做显式空值或精确匹配；命中时不创建 Delivery Request，但源
  Task/Run/Artifact/Evidence 事实保持不变。它不等同于 `silent`，后者仍必须产生
  `suppressed` Receipt。
- Artifact-only 插件 Runner 可能没有 Conversation delta。若策略没有抑制空结果，
  Projector 使用稳定的 `Task completed` 文本形成可投递 Result，并保留事件中的
  Artifact/Evidence 引用；不会把插件没有聊天文本误判成 Delivery 失败。

`DeliveryReceipt` 只描述某个 Adapter attempt 的终态：

- `delivered`：外设确认接收。
- `failed`：必须携带有界 `error_code`，源事实保持不变。
- `suppressed`：策略明确选择不外发，不等同于失败或缺失记录。
- `uncertain`：外部效果可能已经发生但没有可信回执；禁止自动创建下一 attempt，
  必须由后续显式恢复策略处理。

`DeliveryAttempt` 使用 `owner_id + revision + expires_at` 冻结一次明确编号的执行权：

- 相同 attempt 的并发 claim 返回同一 owner 的记录，只有 owner 可以 renew/settle。
- `failed` 后调用方可以显式 claim 下一 attempt；attempt 编号不可跳跃。
- `delivered / suppressed / uncertain` 都阻止自动重试。
- 进程退出后，过期 claim 保守恢复为 `uncertain`，因为 Store 无法证明外部效果没有
  发生。

## 3. 扩展边界

`delivery.adapter` 是 public、process 生命周期、`fail_closed` Slot。目的地显式
选择一个 Adapter，失败不能静默转投另一个外设。插件实现
`DeliveryAdapter.adapter_id / supports() / deliver()`，并在 shadow generation 中
通过公开 Protocol 和 namespaced identity 校验后才能发布。Adapter 不获得 Task
Store、Approval Store 或内部 Workspace；Host 只传入已经脱敏和授权的
`DeliveryRequest`。

`DeliveryDispatcher` 按 Request 记录的 `registry_generation` 获取精确 capability
lease，校验 Adapter Slot、identity、supports 和 Receipt 所有权，再提交终态。热替换
只影响后续 Request；运行中的投递继续使用创建 Request 时的 generation。Adapter
调用抛错或返回错误身份时记为 `uncertain`；Adapter 在调用前不可用才是明确
`failed`。

### Stream 公开事件边界

`stream` 描述“运行过程中按已提交业务事实投递”，不等于把 Provider 的原始 token
流直接转发给 Channel。当前公共边界如下：

- `conversation.assistant.completed → reply`：只投递完整、已经写入 Ledger 的可见
  回复；`conversation.assistant.delta` 继续服务 Workbench 实时渲染，不产生外部
  Delivery，避免分片刷屏和崩溃前半条消息泄漏。
- `tool.started / tool.completed → activity`：映射为现有 Channel 协议的 completed
  `function_call / function_call_output` Message。Console 只在工具调用形成稳定完成块
  后提交 started，参数与文本结果预览上限为 8 KiB，并显式记录 truncated 标志。
- `runner.reasoning.delta`、Provider 临时帧与普通 progress 永不进入 Delivery。
- Harness 和 Console Runner 都必须在 Provider terminal marker 上提交唯一
  `conversation.assistant.completed`；Workbench 仍以 delta 构造实时正文，不消费该
  terminal 事件来重复追加文本。

Console 完成响应中的 image/audio/video/file 内联字节会先写入内容寻址 Artifact
Store；Ledger 只保存有序 content descriptor、Artifact ID 与完整性引用，不保存
base64。Channel Adapter 在发送前校验 `agent_id + task_id` 所有权，并确认每个
Artifact ID 确实来自该 Task 的已提交事件，再读取哈希校验后的内容并临时构造
`data:` URL。找不到内容、跨 Task 引用或哈希不一致都会在外部发送前返回明确
`artifact_unavailable`。

作为回复组成部分的 Artifact 标记 `delivery_disposition=embedded`，不会再额外发送
Artifact Ready；用于 Workbench 结果包的 `task-result.md` 标记
`result_projection`，也不作为 Stream 附件重复投递。其他独立 Artifact 仍产生
Artifact Ready。

代码级媒体契约已经覆盖 image/audio/video/file，并已通过
`Ledger → Delivery Worker → generation Registry → System Adapter → ConsoleChannel`
真实类链路验证。该验收同时发现并修复 Adapter Message 缺少
`object="message"` 导致 BaseChannel 静默忽略、但 Receipt 仍误报 delivered 的问题。
外部媒体 Channel 和浏览器展示尚未验证，因此 `LiteCronTaskRuntime.supports()`
仍拒绝 stream Job；旧 Executor 继续承担生产流量。

## 4. Lite 后续接线

1. [已完成] 建立 SQLite Delivery Projection，以 source event 与 destination 幂等
   claim；owner/revision/expiry 保护 attempt，进程崩溃后的过期 claim 进入
   `uncertain`，不会自动重复外发。
2. [已完成] Projector 只把 Reply、Result、Approval、Activity、Exception、
   Artifact Ready 转成请求；
   普通 Timeline 进度不进入 Inbox。
3. [已完成] system Channel Adapter 把 opaque address 映射到旧
   channel/user/transport context 参数；映射只存在于兼容层。Adapter 接受 final
   Result 以及 stream 的 completed Reply/Tool Activity Message，不接受原始 delta。
4. [部分完成] final/silent agent Cron、Heartbeat 与 text-only Cron 使用同一
   Delivery Store/Adapter；text-only Fire 先绑定稳定 Delivery ID，不经过 Task
   Projector。stream 的公共文本、工具事件与媒体 Artifact 基础已完成，旧
   `CronExecutor` 仅继续承接等待真实 Channel 验收的 stream 和其他不能无损表达的
   迁移期回退。
5. Inbox 读取 Receipt/Projection；标记已读不会删除或更新源 Task 事件。

Lite 已实现 `InboxProjectionPort` 与 SQLite Store，并由 `TaskDeliveryWorker` 在 Receipt
终态后写入。稳定模型、CAS 与旧 JSON Inbox 兼容边界见
`docs/design/qwenpaw-inbox-projection.md`。

## 5. 验收

- [x] Kernel 模型拒绝无 Task 的 Run 引用和跨 Conversation destination。
- [x] Destination、Request 与 Inbox Item 只输出 `chat_id`；旧 JSON 可直接恢复，
  冲突双身份失败关闭，Delivery ID 与 SQLite 表结构不变。
- [x] failed Receipt 必须有错误码，成功/抑制 Receipt 不得携带失败状态。
- [x] 核心请求序列化不含 `session_id`。
- [x] system 与 plugin Adapter 使用同一公开 Protocol 和 Slot identity 门禁。
- [x] 相同请求并发投递只产生一个 active attempt。
- [x] 进程崩溃后 pending attempt 恢复为 uncertain，已 delivered 请求不会重复外发。
- [x] Adapter 固定 Request generation；热替换不会改变运行中的投递实现。
- [x] Task Ledger 的 final response 可重建为单一 Result Delivery；stream 只从
  `conversation.assistant.completed` 形成 Reply，Tool Activity、Approval、Exception、
  Artifact Ready 使用各自稳定 source event identity；delta 与 reasoning 不外发。
- [x] Artifact-only 插件结果可形成非空 Result；显式 `suppress_empty_text` 时不创建
  Request，普通 Timeline 进度始终不投递。
- [x] Adapter 超时进入 uncertain 后，Task、Run、Plan、Event 与 Artifact 引用保持
  逐字段不变，只有 Delivery attempt/receipt 状态变化。
- [x] system Channel Adapter 产生现有 ChannelManager 可消费的 completed Message，
  transport context 不进入 Kernel 字段。
- [x] Console/Harness terminal response、Tool Activity 映射及 8 KiB 预览边界通过
  定点契约测试。
- [x] Console 内联 image/audio/video/file 外迁 Artifact，Ledger 不含原始 base64；
  Adapter 按 Task 已提交事件校验所有权、验证哈希并恢复有序媒体 Message，嵌入媒体
  与 result projection 不产生重复 Artifact Ready。
- [x] 持久化 Ledger、Delivery Worker、generation Registry 与实际 ConsoleChannel
  串联验证：只投递一次、文本/图片顺序正确、后台 Cron 不写 Console push。
- [ ] 在浏览器和至少一个外部媒体 Channel 验证 Stream 文本、工具及四类媒体；
  验收前 `LiteCronTaskRuntime` 仍拒绝 stream。
- [x] system Inbox Adapter 与插件 Adapter 使用同一 Slot；Heartbeat Inbox 目标不向
  Channel 外发，相同计划槽重放不重复创建 Task 或 Inbox Item。
- [x] Heartbeat quiet marker 不产生 Delivery/Inbox，但已完成 Task 与 Ledger 结果仍
  可审计；main 目标明确为不请求投影。
- [ ] `stream/final/silent` 在真实 Channel 与 Inbox 上完成端到端验收。
- [ ] 真实 Channel Delivery 失败时 Task、Artifact、Approval 投影保持逐字节不变。
