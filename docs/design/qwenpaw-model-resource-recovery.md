# QwenPaw 模型资源等待与长程续行

## 1. 目标

模型传输中断、Provider 过载、限流或额度耗尽不再都被压缩成一次 HTTP 请求失败，
也不要求用户再发送一句话来推动 Agent。短重试与 fallback 全部耗尽后，Runtime 将
可恢复故障记录为独立、可查询、可跨进程恢复的资源等待事实；资源恢复后创建新的
`Submission` / `Invocation`，继续使用原 `ChatSpec.id` 和 `correlation_id`。

这不是前端消息队列，也不是 `ask_user`：

- Queue 只表达已接受输入的服务端执行顺序；
- Interaction 表达审批、缺失事实、关键偏好和范围授权；
- Resource Wait 表达运行所依赖的模型资源尚不可用；
- Model Step Continuation 表达部分模型流结束后由持久上下文继续推进；
- Assistant Message 只向用户表达，不决定 Runtime 生命周期。

## 2. 领域边界

`ModelResourceWait` 只保存恢复所需的最小事实：

- 稳定 `wait_id` 与来源 `attempt_id`；
- 原 `invocation_id`、`correlation_id`、`ChatSpec.id` 和 Agent 所有权；
- Provider 无关的 `failure_class`；
- timer / external-event 触发方式、`not_before`、revision 和 dispatch 状态；
- 成功创建的后续 `submission_id`。

它不保存 Prompt、模型输出、异常正文、Provider 凭据或隐藏 reasoning。实际模型调用
证据仍属于 `ModelCallResult`；Resource Wait 只是该事实的恢复投影和 durable outbox。

`ModelStepContinuation` 同样是内容安全的 source of truth，只保存来源 attempt、
Invocation、correlation、`ChatSpec.id`、输出边界、revision、状态和后续
`submission_id`。半截输出只用于当次流式展示，不提交为最终 Assistant Message，
也不作为后续模型上下文；后续步骤从已持久化的完整上下文重建。

当来源 Invocation 已产生 Action 时，内容安全元数据与私有上下文分离：Kernel
`ModelStepContextCheckpoint` 只保存 checkpoint、continuation、Invocation、
`ChatSpec.id`、来源 Submission、Action 证据摘要和计数；真实 Agent context 存在权限
为 `0600` 的不可变 Lite snapshot，不进入 Activity、Kernel JSON 或插件 SDK payload。
Action 摘要只覆盖 Action ID、终态、内容安全 observation digest 与 side-effect status，
绝不对原始 Tool output、Prompt、凭据或被省略内容做指纹。

## 3. 状态机

```text
ModelCallResult(wait_resource)
        |
        v
     waiting --------------------------+
        |                               |
        | rate-limit timer              | quota/resource event
        v                               v
      ready ----------------------> dispatched
                     idempotent enqueue     |
                                            v
                              new Submission / Invocation

waiting / ready -- Interrupt or Stop-and-Clear fence --> cancelled
transport recovery budget exhausted ----------------> recovery_exhausted

ModelCallResult(continue_model_step, partial boundary)
        |
        v
      ready ----------------------> dispatched
        |        idempotent enqueue      |
        |                                v
        |                    new Submission / Invocation
        |
        +-- prior Action exists --> action_reconciliation_required
        +-- Stop / Interrupt ----> cancelled
        +-- cycle budget --------> recovery_exhausted
```

- `rate_limited`：统一错误策略将 Provider `Retry-After` 的 delta-seconds 或 HTTP-date
  解析为有限、非负的 `retry_after_seconds`；Kernel 和 SQLite 只保存这个内容安全
  hint，不保存原始 header。无合法 hint 时回退 Lite 默认 timer。
- `quota_exhausted`：默认等待外部资源事件，不自动循环消耗额度。
- `transport_unavailable` / `provider_overloaded`：`RetryChatModel` 先执行有界的
  同 Invocation 短重试；整个 logical call 与 fallback 均失败后，才创建独立 timer
  Wait。Lite 对 transport、provider overload 和 rate limit 共用同 correlation 的
  自动 timer budget，默认最多恢复 3 个跨 Invocation cycle，之后持久化
  `recovery_exhausted`，投影为 `WaitCondition.expired`，不再自动复活。
- enqueue 与 outbox 标记之间崩溃时，稳定 idempotency key 会取得同一 Submission，
  不重复创建执行。
- `Interrupt Current` 以来源 `invocation_id` 阻止同一运行的迟到恢复；
  `Stop and Clear` 还会取消该 `ChatSpec.id` 下尚未入队的全部等待。
- 恢复 worker 先读取 Queue revision，再读取持久化 Control；若 Stop 恰好发生在检查
  之后，恢复 enqueue 的乐观并发校验必定失败，下一轮再将 Wait 终结为 cancelled。
- enqueue 只先落库，不立即唤醒 consumer；Wait 与 `submission_id` 绑定成功后才允许
  通用 dispatcher 执行。进程启动也先修复 Resource Wait outbox，再恢复 Queue 消费。
- 执行恢复 Submission 前再次校验 Wait 已处于 dispatched 且反向绑定当前
  `submission_id`，取消态或伪造 envelope 不能进入 Runtime。
- 后续执行重新检查副作用；未知或不确定的 Action 不能因模型恢复而盲目重放。
- 部分流续行以稳定幂等键创建新的 Submission / Invocation，沿用原 correlation，
  默认最多自动续行 2 个 cycle；进程在 enqueue 后崩溃也只会绑定同一 Submission。
- Model Call 在 continuation 已落库后抛出 typed `ModelStepRecoveryError`，Runtime
  将它与用户取消和普通失败分开处理：保存失败 Turn，但不把 Envelope partial blocks
  注入 session。用户主动取消仍保留既有“保存已展示 partial”语义。
- 当前 AgentScope 只在完整响应后进入 Action 阶段；dispatcher 仍扫描来源 Invocation
  的持久化 `ActionRequest`。一旦存在 Action，dispatcher 会生成内容安全的
  `ModelStepReconciliation`，将阻塞原因区分为结果未落库、副作用不确定，以及所有
  Action 已终态但下一步缺少可重建的 durable tool context。三种情况都进入
  `action_reconciliation_required`，不自动重做；assessment 与 continuation 一同
  持久化并投影到 Activity，旧记录没有 assessment 时仍可读取。
- 所有 Action 已终态且副作用确定时，Runtime 还必须证明 Agent snapshot 中存在每个
  executor item 对应的 `CommittedActionItem`。同步 ToolResult 与后台完成 hint 都携带
  同一内容安全 binding；它先原子保存不可变私有 snapshot，再发布
  `ModelStepContextCheckpoint`；如果进程恰好在两步之间退出，dispatcher 可从稳定
  checkpoint ID 重新发现 snapshot 并补齐元数据。
- Action Recorder 在结果持久化成功后生成 `CommittedActionItem`，绑定 Action ID、
  Invocation、executor item ID 和 observation digest。AgentScope 原生 ToolResult 与
  后台 hint 都保留该 binding；checkpoint 必须四项精确匹配，不能用历史中重复的 call
  ID 或只完成审计、尚未进入模型上下文的 Harness event 冒充结果。
- Runtime 首次 checkpoint 与 dispatcher 的 snapshot-first 崩溃修复使用同一个
  `CommittedActionItem` 验证器；同步 ToolResult、后台 hint 和 Harness session tool
  output 在重启前后不会退回旧的 call-ID-only 协议。
- checkpoint continuation 从不可变 snapshot 装配，而不是读取可能已漂移的最新
  Chat session。若来源 Submission 之后已经接受了新输入，旧 continuation 直接进入
  cancelled，绝不越过或覆盖新输入；enqueue 仍使用 Queue revision 关闭检查后的竞态。
- Model Step 状态通过现有 Chat Runtime Observation 投影为 pending、accepted、
  cancelled、blocked 或 failed；它不是 Queue 假状态，也不包含半截正文。
- Resource Wait 与 Model Step 共用只读 `ModelRecoveryHistoryPort`；Resource Wait
  同样进入 Chat Runtime Observation，展示 waiting、ready、dispatched、cancelled 或
  exhausted，而不是要求前端从 Queue 或错误字符串推测恢复状态。

## 4. 长程交互替代“一问一答”

短 Chat 仍允许一次提问得到一次回答，但它只是产品快速路径。长程 Agent 的一个用户
意图可以沿同一 correlation 跨越多个模型步骤、工具调用、等待和进程生命周期：

1. 能由 Runtime 继续的故障自动进入 Retry / Resource Wait / Checkpoint Recovery；
2. 只有缺失必要事实、关键偏好、范围授权或高影响裁决才创建 Ask User；
3. 审批继续使用 Approval，不伪装成自然语言问题；
4. Suggestion 是非阻塞建议，不改变执行所有权；
5. 等待结束后由 durable continuation 主动创建新 Invocation，不要求用户发送“继续”。

运行时只有在 Outcome 已形成、发生显式 Stop / Interrupt、命中不可自动化的 typed
WaitCondition，或恢复与预算策略到达终态时才结束这条执行链。Assistant Message、SSE
连接关闭和一次 HTTP response 都不是生命周期边界。这样替换的不是 Chat 的问答外观，
而是内核用“用户再说一句话”作为调度器的隐含假设；短问答仍自然退化为一条仅含一个
Invocation 的执行链。

当前 Console 兼容 Adapter 会从 typed envelope 装配固定、内容最小化的 runtime
recovery input。Resource Wait 与 Model Step Continuation 都只传递权威事实引用；这是
旧消息执行入口的桥接，不是把恢复重新定义为用户消息。后续 Runtime 原生输入应直接
消费 typed envelope。

## 5. Edition 定位

- Lite：SQLite wait source、单机 timer、durable Submission outbox；不引入消息队列。
- Workstation：Provider health、预算补充事件、进程监督和 Checkpoint 恢复策略。
- Hub：分布式 lease、durable channel、集中限流预算和跨节点 fencing。

Provider 特有 resume cursor、stream token continuation 或 request replay 能力只能由
Adapter 声明；Kernel 不假设任意模型流可以原地续传。

## 6. 当前验收与未完成边界

- [x] Rate Limit 与 Quota 形成不同触发语义。
- [x] 等待事实和下一次 Submission 均可跨服务重启恢复。
- [x] 恢复 Submission 沿用 `ChatSpec.id` 与原 `correlation_id`。
- [x] crash-after-enqueue 通过稳定 idempotency key 防止重复 Submission。
- [x] `/wait-conditions` 可合并投影 Interaction 与 Resource blocker。
- [x] `ModelRecoveryHistoryPort` 统一读取 Resource Wait 与 Model Step；两者进入同一
  Chat Activity，且投影不包含 Prompt、Provider payload、异常正文或凭据。
- [x] SDK 导出稳定 Resource Wait 领域类型。
- [x] 模型结果记录 pre-output、完整响应、部分流、终态流或 incomplete EOF 边界，
  不保存输出正文和隐藏 reasoning。
- [x] 未见终态 chunk 的 EOF 记为 `stream_interrupted`；未产生内容时可由
  transport policy 安全重试，已经产生内容时禁止整请求重放。
- [x] Interrupt / Stop-and-Clear 对资源恢复提交建立持久化 fencing；竞态 enqueue
  使用 Queue revision 关闭，取消后的 Wait 不会复活旧任务。
- [x] 从真实 Provider `Retry-After` 秒数或 HTTP-date 计算动态等待时间，并通过共享
  WaitCondition `not_before` 投影供 Chat 查询。
- [x] Transport/Provider overload 的短重试耗尽后转 durable timer Wait；统一的跨
  Invocation timer cycle budget 也覆盖 rate limit，防止长期故障形成无限恢复循环。
- [x] 从部分流边界创建内容安全、可跨重启、可验证的新 Model Step continuation；
  使用独立 Submission / Invocation、原 correlation 和 2-cycle 自动恢复预算。
- [x] Runtime error-save 区分用户取消与 Model Step recovery；自动恢复时 partial
  output 不进入 session 或下一次模型上下文，用户取消路径保持兼容。
- [x] 来源 Invocation 存在任何持久化 Action 时失败关闭，进入
  `action_reconciliation_required`，不自动重放副作用。
- [x] 根据真实 `ActionRecord` 将阻塞原因分类为 pending result、uncertain side
  effect 或 durable context required，并跨重启持久化内容安全计数。
- [x] 对 terminal/certain Action 验证 Agent context 中的 `CommittedActionItem`，保存
  不可变私有 snapshot，并用内容安全 `ModelStepContextCheckpoint` 自动续行；同步
  ToolResult 与同 Invocation 内已进入上下文的后台 hint 使用同一协议。
- [x] 受控 Harness turn 通过 Session Bridge 将已持久化 ActionResult 与规范化 tool
  output 绑定为 `CommittedActionItem`；只有 session 原子写入时 binding 才进入上下文，
  completion event 本身与 provider history hydrate 均不会猜测该事实。Action 已成功但
  session 写入失败时 Harness Invocation 失败关闭，并明确禁止重做 Action。
- [x] Harness 断流后用 `HarnessRecoveryContextCheckpoint` 四方校验 provider context
  identity、provider history item、QwenPaw session binding 与 ActionStore digest；私有
  provider thread/session ID 只参与 Invocation/Submission scoped SHA-256，不进入
  checkpoint 或公开响应，也不能跨任务形成稳定关联指纹。
- [x] 共享 dispatcher 从该 checkpoint 创建 fenced Harness continuation：durable
  outbox 使用稳定幂等键创建同一 `ChatSpec.id`、原 correlation 的新 Submission；
  Stop / Interrupt、来源之后的新输入、Queue revision、执行前反向绑定和 backend
  一致性共同阻止迟到或串错执行，自动恢复最多 2 个 cycle。恢复仍要求四方 admission，
  不会仅凭 completion event 自动恢复。
- [x] 后台 Action 跨 Invocation 完成后由 durable continuation 主动触发新执行，而非
  等待用户再发送一条消息。Action Recorder 计算结果 digest 后先准备带
  `CommittedActionItem` 的未发布私有 context snapshot；ActionResult 成功落库后才
  发布内容安全 outbox。来源
  Submission 成功终止、未被 Stop / Interrupt 或新用户输入覆盖，且原 Session 尚未
  消费该 binding 时才创建新 Invocation。snapshot-first repair、稳定幂等键和 2-cycle
  预算覆盖崩溃窗口；同一来源的并行后台 Action 作为兄弟 continuation 顺序合并到最新
  Session，不用旧快照互相覆盖。
- [x] 无副作用且证据完整的 Action retry 不依赖用户发送“继续”：durable outbox
  幂等创建同一 `ChatSpec.id`、原 correlation 的 Submission，执行时取得新的确定性
  Invocation，并在 pinned generation 中重新经过当前 Permission、ToolCoordinator、
  action-scoped Sandbox 与 ActionRecorder。Stop/Interrupt、新用户输入、配置漂移、
  证据漂移或旧 checkpoint 均失败关闭；执行完成后 Submission 是权威终态。
- [ ] Provider resource health 事件自动释放 quota wait。
- [ ] 真实限流故障和进程重启的浏览器端到端演练。
