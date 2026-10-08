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
  的持久化 `ActionRequest`。一旦存在任何 Action，恢复失败关闭为
  `action_reconciliation_required`，等待后续 Action 对账能力，不自动重做。
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
- [ ] 根据 `ActionResult` / uncertain side-effect 与 Checkpoint 自动完成恢复对账。
- [ ] Provider resource health 事件自动释放 quota wait。
- [ ] 真实限流故障和进程重启的浏览器端到端演练。
