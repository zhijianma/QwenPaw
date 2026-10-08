# Codex 恢复机制与长程 Chat 对 QwenPaw 3.0 的吸收评估

> 日期：2026-10-01
>
> 输入材料：`Codex深度剖析报告.md` v5，重点为第十四章
>
> 适用范围：QwenPaw Agent OS 的 Chat-first Kernel、Runtime、Model Call、
> Action、Interaction、Control 与 Projection 设计
>
> 事实边界：报告是二手分析材料；本文用 QwenPaw 当前源码和既有架构文档
> 校验可迁移性，不把报告中的实现描述直接视为 QwenPaw 事实。

## 1. 结论

QwenPaw 应吸收 Codex 的“分层恢复”思想，但不应复制其具体传输实现，也不应再把
“一问一答”作为智能体运行的基本单位。

目标形态是 **Conversation Execution Chain（会话持续执行链）**：

- Chat 仍是用户的首要入口；
- 一条用户消息是 `Submission`，不是一次任务的完整生命周期；
- 一次模型调用是 `ModelCallAttempt`，不是一个 Turn 的全部工作；
- 一次运行尝试是 `Invocation`，可因等待、断网、进程重启而结束；
- 同一意图可沿同一 `correlation_id` 创建后续 `Invocation`；
- 工具、审批、提问、建议、Steer 和 Interrupt 都是执行链事件；
- Artifact、Evidence、Verification 和 Outcome 才说明工作完成到什么程度；
- 短问答继续走单 Submission、单 Invocation 的快速路径，不为简单对话增加负担。

因此，“一问一答”应从**内核生命周期模型**降级为**一种常见 UI 投影**。这既能
保持当前 Chat 体验，也能支撑分钟、小时甚至跨进程的长程任务。

这里要避免一种实现层误读：Handbook 已把“一问一答”归入上一阶段应用形态，并以
持续 Agent Loop 描述长程任务；其中采用请求/响应形式的 HTTP/SDK 示例不是 Runtime
生命周期契约。`Ask User` 不是智能体每一步的默认推进器，而是 Runtime 在缺少必要
事实或授权时创建的 durable suspension。正常路径应由目标、计划、事件、Action
结果、Artifact、Evidence 和 Verification 自主推进；Suggestion 是非阻塞提示，
Steer 是异步控制输入，Approval 是策略裁决，三者都不能退化成追问用户的聊天话术。

## 2. 当前能力与真实缺口

### 2.1 已经具备的恢复基础

| 能力 | 当前状态 | 判断 |
|---|---|---|
| 浏览器 SSE 断开 | 服务端执行不依赖 subscriber，重连可读缓冲和权威投影 | 已覆盖客户端断线，不等于模型流恢复 |
| Submission 接收确认 | 使用稳定 client message ID 和幂等边界 | 已具备“不确定时不盲目重发”的基础 |
| Chat 排队与运行所有权 | 服务端持久化 queued/admitted/running/terminal | 已不依赖浏览器存活 |
| 模型调用审计 | 已记录 route、attempt、stream success/cancel/failure 与 usage | 具备引入恢复裁决的事实基础 |
| 模型连接前重试 | `RetryChatModel` 在尚未产生有效输出时有界重试 | 部分覆盖；尚未与业务重试预算显式分离 |
| 流中错误 | 产生有效输出后不重放完整请求，创建 bounded Model Step continuation | 半截正文不提交，后续从 durable context 重建 |
| 工具副作用 | `ActionRequest/Result`、审批、幂等与 uncertain 状态 | 比“让模型猜工具是否执行”更适合可靠恢复 |
| Interaction | Approval、Ask User、Suggestion、WaitCondition 与 durable continuation 已统一 | 用户只在必要事实或授权边界介入 |
| 进程重启 | Queue、Ledger、Wait 等可查询；Python 调用栈不可恢复 | 必须创建新 Invocation，不能伪装原地续跑 |
| 时钟 | Provider timeout、rate limiter 等已使用 `time.monotonic()` | 保留，但 suspend 语义需逐平台验证 |

### 2.2 最大缺口

当前 `RetryChatModel` 把“流已产生内容后的网络中断”收敛为异常。这个做法比盲目
重试安全，但对长程任务仍不够：已经完成的模型输出、Action、Artifact 和 Evidence
可能有效，网络中断不应自动把整条执行链判为业务失败。

另一个结构性缺口是，Chat 的视觉模型仍容易让开发者把“用户消息 → 助手消息”
理解成完整运行边界。这样会导致：

- 等待审批或用户输入时长期占用进程内协程；
- 恢复时试图复活旧调用栈，而不是重建新 Invocation；
- 把中间 assistant message 误当最终 Outcome；
- Steer 被误建模为下一轮提问，而不是当前执行链的控制输入；
- 网络、配额、预算、用户中断和副作用不确定性混入同一个失败通道。

## 3. 对 Codex 第十四章的逐项裁决

| 报告机制 | QwenPaw 裁决 | 原因与落点 |
|---|---|---|
| 网络错误与业务失败分离 | 吸收 | 在 Model Call Plane 增加类型化 failure class 与 recovery disposition |
| 连接前独立退避 | 吸收思想 | 独立于业务 attempt 预算，但必须受可取消的总等待策略约束 |
| 无限连接重试 | 不照搬 | Lite 不能永久占用运行槽；超过 deadline 后转 Resource Wait，而不是失败或假暂停 |
| 流中断返回“需继续” | 吸收语义 | 表达为持久化 stream outcome，由 Runtime 决定后续新 Model Step/Invocation |
| 工具边收流边执行 | 暂不吸收实现 | 当前 AgentScope/Provider 抽象没有统一 committed action item；直接做会扩大重复副作用风险 |
| 断流后排空在途工具 | 有条件吸收 | 只处理已经持久化 `ActionRequest` 的动作；未提交的模型片段不得触发动作 |
| 工具结果写历史后不重做 | 强化吸收 | 以 `ActionResult` 和 SideEffect status 为权威，不能只依赖自然语言历史 |
| 用户中断独立终态 | 已对齐 | Interrupt 沿 cancellation root 传播，不能被网络恢复重新拉起 |
| WS 增量续传 | Adapter 可选能力 | 不进入 Kernel；需要 Provider 明确 token/prefix/route 契约，失败回退完整请求 |
| WS → HTTPS 降级 | Adapter 策略 | 只能由支持该能力的 Provider 实现，仍须遵守 `Retry-After` 与预算 |
| 单调时钟 | 吸收 | 活动耗时使用 monotonic，审计和 durable deadline 使用 wall-clock；逐平台验证 suspend |
| 精确 client ID 对账 | 已对齐并保留 | 只有相同 submission identity 才确认接收，不因模糊历史自动重发 |
| 重连状态可见 | 吸收 | 作为 semantic observation/control projection，不伪造 Queue 项 |
| 网络恢复与进程恢复分层 | 强化吸收 | 前者可保留当前 Invocation；后者必须从 durable boundary 创建新 Invocation |

## 4. 替代“一问一答”的领域模型

### 4.1 身份层次

| 概念 | 生命周期 | 作用 |
|---|---|---|
| `ChatSpec.id` | 整个会话 | 用户可理解的持续工作空间；避免再引入竞争性的 `session_id` |
| `Submission.id` | 一次输入或控制提交 | 幂等接收、排队和精确回执 |
| `correlation_id` | 一条用户意图的完整执行链 | 跨等待、恢复和多个 Invocation 保持因果连续 |
| `Invocation.id` | 一次占用 Runtime 的运行尝试 | 有明确开始和终态，不跨进程复活 |
| `ModelCallAttempt.id` | 一次 Provider 网络尝试 | 路由、成本、流结果和恢复证据 |
| `ActionRequest.id` | 一个已提交动作 | 审批、执行、结果、幂等与副作用不确定性 |
| `Interaction.id` | 一次人机等待 | Approval、Ask User、Suggestion 的统一事实 |
| `Artifact/Evidence` | 可持久复用 | 进度和结果，不依附某条临时 UI 消息 |

`correlation_id` 是持续执行链的关键。后续 Submission 可以由用户产生，也可以由
Interaction continuation、资源恢复或 Scheduler 产生，但它们必须显式引用前序因果，
不能靠“最后一条消息”推断归属。

### 4.2 执行链

```text
User Submission accepted
  -> Invocation N admitted
  -> Model Step
  -> zero or more governed ActionRequest / ActionResult
  -> Artifact / Evidence / Observation
  -> choose one:
       complete -> Verification -> Outcome
       interact -> persist Interaction + WaitCondition
                   -> end Invocation N
                   -> response creates continuation Submission
                   -> Invocation N+1, same correlation_id
       resource unavailable -> persist Resource Wait
                               -> recovery scheduler creates Submission
       interrupted -> terminal, never auto-recover
       uncertain side effect -> explicit reconciliation or authorization
```

这条链允许“一条用户消息产生多次模型调用和多轮动作”，也允许在没有新增用户问题时
继续执行。Assistant message 是给人的投影，不再充当 Runtime 状态机的分隔符。

### 4.3 与 Chat UI 的关系

Chat 不需要改成 Task 页面。当前阶段只需让 Chat 投影表达：

- 当前执行阶段：推理、动作、等待人、等待资源、验证、完成；
- 中间产物与证据；
- 可响应的 Interaction；
- 可作用于当前执行链的 Steer / Interrupt；
- 网络恢复是 Runtime 状态，不是“排队中”的假消息。

短对话仍可只显示用户消息和最终助手消息。只有执行跨越动作、等待或恢复边界时，
才展开持续执行活动。Task Workbench 继续后置，不能为了长程执行复制第二套状态机。

### 4.4 Human Interaction Policy

Runtime 默认持续执行，只有满足以下任一条件才允许产生 blocking `Ask User`：

- 缺少无法从 Workspace、Memory、Tool 或既有上下文取得的必要事实；
- 存在多个会显著改变结果且无法由 Acceptance/Policy 判定的用户偏好；
- 下一步需要新增权限、外部协调或扩大用户已经授权的范围；
- 高影响动作无法由现有 Approval Policy 给出确定裁决。

以下情况不得用 `Ask User` 代替基础设施能力：

- 用追问确认 Runtime 已经知道的事实；
- 每完成一个步骤就请求“是否继续”；
- 用自然语言问题代替 Policy Approval、Side Effect reconciliation 或预算门禁；
- 用下一轮普通聊天模拟 Steer、Interrupt、资源恢复或定时唤醒；
- 为了保持 Python 协程存活而阻塞等待用户。

回答必须提交为结构化 `InteractionResolution`，再经 outbox 创建 continuation
`Submission`；它可投影为一条用户消息，但语义上仍属于原 `correlation_id` 的同一
执行链。这样，用户可以在需要时参与决策，却不必成为长程任务的人工调度器。

该策略现已成为可执行 Kernel 契约：每个新 `USER_INPUT` 在进入
`InteractionService` 前必须声明 `UserInputReason`，取值仅限缺失必要事实、关键偏好、
范围授权或高影响裁决。内置 Tool 与插件通过同一 `RuntimeInteractionProducer` 传递
原因，PawApp 兼容确认也显式分类；历史未分类记录仍可读取，但不能继续产生未分类的
新等待。原因会进入内容安全 Observation，后续可通过评估发现“声明理由与实际提问
不一致”的滥用，而不依赖中英文关键词封禁。

### 4.5 Activity 是投影，不是新状态机

Chat Runtime snapshot 复用 `RuntimeObservation`，从 Model Call、Action、Interaction、
Control、Compaction 和 Verification 的权威 Store 派生最近活动，并将有界
`ObservationPage` 纳入 snapshot cursor。它不保存 prompt、回答、隐藏推理或第二份
生命周期状态；每条 activity 都必须携带可回查的 `ObservationSource`。

Invocation Control 现在公开只读 Submission history：接收事实投影为不可变 intent，
进入成功、失败、中断或取消后投影为不可变 terminal evidence，并贯穿
`submission_id + invocation_id + correlation_id`。queued/running 仍读取 Queue 当前
snapshot；旧 Store 没有逐跳转换记录，因此不会反向编造时间线。

Chat-owned Artifact/Evidence 已从 ownership receipt 独立投影：上传与内置/插件工具
输出共用 `ConversationArtifactHistoryPort`，工具产物保留 invocation、correlation 与
固定 registry generation。Task-owned 结果则由 `TaskResultHistoryPort` 一次回放
Artifact、Evidence 与 Verification，保留 Task/Run/Event/Step/Correlation 因果；同一
结果同时存在 Chat receipt 时，以 Task 因果记录为准并去重，不让 receipt 冒充 Task
registry。两种投影都只公开 ID、类型、媒体、大小、哈希和 producer，不公开 URI、
文件名、metadata 或 claim。

在各源 Store 具备 revision/notifier 之前，应控制重建频率；不能为了 UI 动画增加一套
易漂移的事件表，也不能把当前状态 SSE 宣称为完整审计日志。派生 Observation index
必须与当前 source 集合 reconciliation，版本变化后的旧指针只能失效，不能反过来
阻断权威事实读取。

## 5. 类型化恢复模型

Kernel 已新增 `ModelFailureClass` 与 `ModelRecoveryDisposition`，并将它们作为可选兼容
字段写入 `ModelCallResult`。连接前超时/断网映射为
`transport_unavailable → retry_transport`；产生有效内容后断流映射为
`stream_interrupted → continue_model_step`；用户取消映射为
`user_interrupted → stop_interrupted`；短时 429 与 quota exhausted 分别映射到独立
失败类别。旧结果没有这两个字段时仍可读取，但新结果一旦写入其中一个就必须同时写入
另一个，且已产生内容的尝试不能声明整请求 transport replay。

当前已完成恢复裁决、输出边界留证、Resource Wait scheduler 和跨 Invocation 的
bounded Model Step continuation：限流按 timer 到期，quota 默认等待外部资源事件；
两类恢复 outbox 均以稳定幂等键创建同一 `ChatSpec.id` 和 `correlation_id` 下的新
Submission / Invocation。部分正文不进入持久上下文或最终 Assistant Message；来源
Invocation 只要存在 Action 就失败关闭等待对账。尚未实现 Provider token 级原地续传
或基于 `ActionResult` / Checkpoint 的自动副作用对账，不能把新步骤误报成原流复活。

Runtime 已显式分开两种 partial 终止：用户 Stop/取消继续保存已经展示的 partial，便于
用户查看；自动 Model Step recovery 则在 outbox 持久化后用 typed error 穿过 Agent
执行层，错误保存不再把 Envelope partial 注入 session。因此下一 Invocation 只读取
最后一个完整提交边界，不会把半截 Assistant 消息当作事实或再次参与模型输入。

### 5.1 故障分类

建议在现有 `ModelCallResult` 和 Runtime policy 上冻结以下稳定语义，而不是依赖
异常字符串：

- `transport_unavailable`：尚未建立连接或未产生可观察输出；
- `stream_interrupted`：已经产生输出但没有收到合法终态；
- `provider_overloaded` / `rate_limited`：尊重服务端恢复时间；
- `auth` / `quota` / `budget` / `policy`：立即终止，禁止伪装网络重试；
- `invalid_request` / `context_overflow`：由明确修正或 compaction policy 处理；
- `user_interrupt`：唯一用户终止语义，不得自动继续；
- `unknown`：失败关闭，不能默认成安全可重试。

恢复裁决至少包含：

- `retry_transport`：同一逻辑 Model Step 的新网络 attempt；
- `continue_model_step`：基于 durable context 启动后续模型步骤；
- `wait_resource`：释放计算槽，等待网络或 Provider 恢复；
- `fail_terminal`：业务终态；
- `reconcile_side_effect`：存在 uncertain Action，先查证再决定；
- `stop_interrupted`：用户中断，永不自动续跑。

### 5.2 五层恢复

| 层级 | 故障边界 | 恢复方式 | 不变量 |
|---|---|---|---|
| L0 | 浏览器/SSE 断开 | 重订阅权威 projection | 不停止服务端 Invocation |
| L1 | 模型连接前失败 | 新 `ModelCallAttempt`，独立退避 | 无输出、无 Action，才允许安全重试 |
| L2 | 流中断且无 committed Action | 保存 partial outcome，重建下一 Model Step | 不把 partial message 当最终 Outcome |
| L3 | 已有 Action | 读取 `ActionResult`/side effect 状态 | succeeded 不重做，uncertain 先对账 |
| L4 | 进程重启 | 从 Wait/Checkpoint/Outbox 创建新 Invocation | 不恢复 Python stack，不覆盖旧 Invocation |

### 5.3 等待策略

QwenPaw 不采用无限占槽重试。推荐策略是：

1. 短暂抖动在 Provider Adapter 内有界退避；
2. 超过短等待预算后持久化 `Resource WaitCondition`；
3. 释放 Invocation 的模型连接和计算槽；
4. 本地恢复探针或 Scheduler 在资源恢复后写 continuation Submission；
5. 保持原 `ChatSpec.id` 与 `correlation_id`，创建新 Invocation；
6. 用户可随时 Interrupt，且恢复事件不能越过已提交的 Interrupt revision。

这样既不会把断网当失败，也不会像无限 retry 那样永久占用 Lite 单机资源。

## 6. 工具执行的安全边界

Codex 的“边收流边执行工具”建立在其 Provider 事件协议、工具项身份和 rollout
持久化之上。QwenPaw 当前不能只看到类似 tool call 的增量文本就执行。

只有满足以下条件，未来才可开启流内工具并发：

1. Provider Adapter 输出完整、校验通过且身份稳定的 committed action item；
2. Host 在执行前原子保存 `ActionRequest`；
3. Policy、Approval、Environment 和 generation 已固定；
4. handler 使用稳定 idempotency key；
5. `ActionResult` 或 `uncertain` 在流终止前后都可独立查询；
6. 断流 drain 只处理已提交 Action，禁止从残缺增量重建动作。

在此之前，保持“模型步骤终止后再执行动作”是合理的安全选择。性能优化不能破坏
副作用可证明性。

## 7. 分阶段落地

### R0：先完成 durable continuation

- [x] 完成 Chat Ask User 的 conversation continuation outbox；
- [x] Interaction 决定与 outbox 同一事务提交，enqueue/mark 崩溃窗口幂等恢复；
- [x] continuation 创建新 Submission 和 Invocation，并继承 `correlation_id`；
- [x] 不恢复旧协程，不依赖浏览器在线；并行 Ask User 全部保留。
- [x] Task-owned Artifact/Evidence/Verification 通过统一结果历史进入 Chat Activity，
  相同 Chat receipt 不重复显示且不丢失 Task 因果。
- [x] 在固定真实 Chat 中完成浏览器级回答、自动续接与最终消息验收。

### R1：冻结 Model Recovery Contract

- [x] 为 Model Call Plane 增加稳定 failure class、stream outcome 和 disposition；
- [x] 未见终态 chunk 的 EOF 不再误记成功；无内容可 transport retry，已有内容禁止
  整请求重放；
- [x] quota、budget、auth、policy 和 user interrupt 与网络错误分开分类；
- [x] 输出 semantic observation，供 Chat 展示恢复状态；
- [x] Rate Limit 的 `Retry-After` 秒数/HTTP-date 归一化为内容安全 hint，重试耗尽后
  进入 Resource Wait 的动态 `not_before`，不保存 Provider header；
- [x] transport/provider overload 先使用 Retry wrapper 的有界 attempt budget；整个
  logical call 与 fallback 耗尽后才进入 durable timer Wait。Transport、provider
  overload 与 rate limit 共用跨 Invocation 自动 timer budget，默认最多 3 个 cycle；
  耗尽后形成可查询终态，不无限占槽或循环复活。

### R2：Resource Wait 与恢复调度

- [x] 模型结果为 `wait_resource` 时持久化独立资源等待；
- [x] timer 到期或 external event 释放后，以幂等 outbox 创建 continuation；
- [x] Lite 使用本地 SQLite source 和 worker，不引入前端 Queue 或消息中间件；
- [x] Transport/Provider overload 经过短重试与 fallback 后转 timer Wait，并以同一
  correlation 下的持久化 cycle budget 限制自动续行；
- [x] Interrupt 按来源 Invocation、Stop-and-Clear 按 Conversation 对恢复提交进行
  fencing；Queue revision 关闭“检查后、入队前”的竞态，取消 Wait 不会复活；
- [ ] Workstation/Hub 用健康探针或分布式 lease 替换 Lite Adapter。

### R3：部分流与 Action 对账

- [x] 保存内容安全的 bounded partial stream boundary，而非把增量正文作为事实源；
- [x] 用稳定 outbox 创建同 correlation 的新 Model Step，crash-after-enqueue 恢复时
  不重复创建 Submission，Stop / Interrupt 可持久化 fencing；
- [x] Model Step recovery 与用户取消使用不同的 session-save policy；自动恢复不
  提交 partial Assistant blocks，用户取消仍保留可见 partial；
- [x] 来源 Invocation 存在 Action 时进入 `action_reconciliation_required`，不重做；
- [x] 读取真实 `ActionRecord`，将阻塞原因和计数持久化为 pending result、uncertain
  side effect 或 durable context required；不把“Action 已成功”误等同于“模型上下文
  已可恢复”；
- [x] terminal/certain Action 只有在不可变 Agent context snapshot 能逐一证明
  `CommittedActionItem` 时才生成 `ModelStepContextCheckpoint` 并自动续行；同步
  ToolResult 与同 Invocation 内的后台完成 hint 使用同一 provider-neutral binding，
  snapshot 私有存储，
  Kernel 与 Activity 仅公开内容安全引用和计数。来源之后出现新 Submission 时取消旧
  continuation，避免用旧 checkpoint 覆盖新上下文；
- [x] 受控 Harness Adapter 已把远端 ActionResult 与 Session Bridge 写入的规范化 tool
  output 绑定为 `CommittedActionItem`；单独的 provider completion event 和 history
  hydrate 不满足该协议。
- [x] `HarnessRecoveryContextCheckpoint` 已同时验证 provider context identity、history
  tool item、QwenPaw session binding 与 ActionStore digest；checkpoint 不保存 provider
  thread/session ID、工具输出或异常正文。
- [x] 已通过 admission 的 checkpoint 接入共享 Submission dispatcher，以 fenced
  durable continuation 启动后续 Invocation。outbox 使用稳定幂等键并沿用
  `ChatSpec.id` 与 correlation；Stop / Interrupt、来源后的新输入、Queue revision、
  反向绑定、backend 一致性和 2-cycle 预算共同限制恢复。
- [x] 已完成后台 Action 的跨 Invocation 主动 continuation：结果 digest 确定后先准备
  未发布私有 snapshot，ActionResult 成功提交后才发布 durable outbox；snapshot-first
  repair 也必须精确匹配 ActionStore 结果。来源成功且未被 Stop / Interrupt / 新输入
  覆盖时自动续行。恢复加载时把结果合并到最新 Session，
  因此同一来源的并行后台 Action 不会用旧快照相互覆盖。

### R4：可选 Provider 增量续传

- 作为 Adapter capability 声明 prefix/token/route/fallback；
- 严格验证前缀与 response identity；
- 能力不满足时回退持久上下文重建，不污染 Kernel。

## 8. 验收标准

1. 浏览器关闭或 SSE 断开后，已接收 Submission 继续执行且只执行一次。
2. 模型在产生任何输出前断网，可按独立 transport policy 重试，不消耗业务重试。
3. 产生部分输出后断流，不盲目重发完整 Turn，也不把 partial message 判为完成。
4. Action 已成功而网络随后断开时，恢复不能再次执行同一 Action。
5. Action 状态为 uncertain 时，系统必须先对账或取得显式重试授权。
6. Ask User 回答提交成功但进程在 enqueue 前崩溃，重启后恰好创建一个 continuation。
7. quota、budget、auth、policy 和用户 Interrupt 不进入网络恢复循环。
8. 同一长程意图跨多个 Invocation 时保持同一 `ChatSpec.id` 和
   `correlation_id`，每个 Invocation 有独立终态。
9. 短问答继续通过单 Submission、单 Invocation 快速路径完成。

当前后端已将上述身份约束固化为 `ConversationExecutionChain` 只读投影：同一
correlation 的多次 Submission / Invocation 聚合展示；blocking Interaction 显式进入
`waiting_user`；单次 Runtime 返回成功只进入 `inactive`，不推断业务 Outcome。Outcome
事实与 Verification 联动仍是独立后续切片，不能以该投影存在冒充完成。
10. Chat 可显示等待资源、等待用户和正在恢复，但不能把它们伪装成 Queue 项。
11. 进程重启后不尝试恢复旧 Python stack，只从 durable boundary 恢复。
12. macOS、Linux、Windows 分别验证 monotonic、sleep/suspend 与 deadline 语义。
13. 无歧义、已授权的多步骤任务不得在每一步产生 `Ask User`；它应自动推进至完成、
    明确失败、策略暂停、资源等待或用户 Interrupt。
14. Suggestion、Steer、Approval 和 Ask User 必须保留独立语义，不能互相伪装成普通
    问答消息。
15. 新 Ask User 未声明允许的 `UserInputReason` 时，必须在写入 Interaction Store 前
    被拒绝；旧未分类记录必须仍可查询和审计。

## 9. 明确不做

- 不把一问一答删除出 UI；只取消它作为内核的唯一生命周期模型。
- 不新建与 Chat 平行的“长任务会话”事实源。
- 不因长程执行而提前开发 Task 页面。
- 不在 Kernel 写死 WebSocket、HTTPS 或某一 Provider 的续传字段。
- 不采用无限且不可释放资源的连接重试。
- 不把自然语言提示“先检查是否做过”当作副作用一致性的唯一保证。
- 不从未完成的模型增量中猜测并执行工具调用。

## 10. 最终建议

QwenPaw 3.0 的差异点不应是“比别人多一种聊天框”，而应是：用户仍在熟悉的 Chat
中工作，但系统内部已经具备长程执行所需的持久身份、控制、等待、恢复、副作用
治理和证据链。

Codex 第十四章提供了很好的故障分层样本；QwenPaw 可以进一步做到：短暂网络抖动
原地恢复，长期不可用转 durable resource wait，进程重启创建新 Invocation，副作用
通过 Action Ledger 精确对账。这样既吸收其优点，也避开无限重试和 Provider 耦合。
