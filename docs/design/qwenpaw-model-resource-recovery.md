# QwenPaw 模型资源等待与长程续行

## 1. 目标

模型限流或额度耗尽不再被压缩成一次 HTTP 请求失败，也不要求用户再发送一句话来
推动 Agent。Runtime 将其记录为独立、可查询、可跨进程恢复的资源等待事实；资源
恢复后创建新的 `Submission` / `Invocation`，继续使用原 `ChatSpec.id` 和
`correlation_id`。

这不是前端消息队列，也不是 `ask_user`：

- Queue 只表达已接受输入的服务端执行顺序；
- Interaction 表达审批、缺失事实、关键偏好和范围授权；
- Resource Wait 表达运行所依赖的模型资源尚不可用；
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
```

- `rate_limited`：Lite 使用有界 timer；后续可由 Provider Adapter 提供经过校验的
  retry hint，但不能把原始 header 直接写入 Kernel。
- `quota_exhausted`：默认等待外部资源事件，不自动循环消耗额度。
- enqueue 与 outbox 标记之间崩溃时，稳定 idempotency key 会取得同一 Submission，
  不重复创建执行。
- 后续执行重新检查副作用；未知或不确定的 Action 不能因模型恢复而盲目重放。

## 4. 长程交互替代“一问一答”

短 Chat 仍允许一次提问得到一次回答，但它只是产品快速路径。长程 Agent 的一个用户
意图可以沿同一 correlation 跨越多个模型步骤、工具调用、等待和进程生命周期：

1. 能由 Runtime 继续的故障自动进入 Retry / Resource Wait / Checkpoint Recovery；
2. 只有缺失必要事实、关键偏好、范围授权或高影响裁决才创建 Ask User；
3. 审批继续使用 Approval，不伪装成自然语言问题；
4. Suggestion 是非阻塞建议，不改变执行所有权；
5. 等待结束后由 durable continuation 主动创建新 Invocation，不要求用户发送“继续”。

当前 Console 兼容 Adapter 会装配一条固定、内容最小化的 runtime recovery input，并
标记 `qwenpaw_model_resource_wait`。这是旧消息执行入口的桥接，不是把恢复重新定义为
用户消息；后续 Runtime 原生输入应直接消费 typed envelope。

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
- [x] SDK 导出稳定 Resource Wait 领域类型。
- [x] 模型结果记录 pre-output、完整响应、部分流、终态流或 incomplete EOF 边界，
  不保存输出正文和隐藏 reasoning。
- [x] 未见终态 chunk 的 EOF 记为 `stream_interrupted`；未产生内容时可由
  transport policy 安全重试，已经产生内容时禁止整请求重放。
- [ ] 从真实 Provider retry-after hint 计算动态等待时间。
- [ ] 从部分流边界创建可验证的新 Model Step continuation。
- [ ] Action uncertainty 与 Checkpoint 决定恢复前自动对账。
- [ ] Provider resource health 事件自动释放 quota wait。
- [ ] 真实限流故障和进程重启的浏览器端到端演练。
