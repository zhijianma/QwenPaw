# QwenPaw Chat 长程 Goal 持久化契约

## 1. 定位

Goal 是 Chat 中一个跨 Invocation 的长程意图，不是 Task 页面状态，也不是某条
Assistant Message 的附属字段。短问答仍可在单 Submission / Invocation 内完成；只有
显式 `/goal` 或已存在的 active Goal 才进入本契约。

稳定身份为：

```text
ChatSpec.id
  -> GoalExecution.goal_id
  -> correlation_id
  -> Submission N
  -> Invocation N
  -> Action / Interaction / Recovery
  -> ConversationOutcome
```

transport `session_id` 只用于没有 `ChatSpec.id` 的兼容 Channel，不参与持久 Goal 主键。

## 2. Kernel 模型

`GoalExecution` 使用 `qwenpaw.goal-execution.v1`，由 `agent_id + conversation_id` 定位
当前快照，并保存：

- 不可变身份：`goal_id`、Agent、Chat、correlation；
- 不可变执行契约：objective、迭代上限、token budget、开始时间；
- 可恢复进度：iteration、tokens used、verdict、feedback；
- 业务终态准备：确定性 `outcome_id + outcome_status`；
- CAS `revision` 与更新时间。

状态机为：

```text
ACTIVE
  |-- explicit complete/block --> OUTCOME_PENDING
  |                                  |-- Host Outcome admitted --> COMPLETED
  |                                  `-- Host Outcome admitted --> BLOCKED
  |-- /clear or /new ------------> ABANDONED
  `-- iteration/token cap --------> EXHAUSTED
```

只有 `COMPLETED` 和 `BLOCKED` 必须绑定显式 Outcome。`ABANDONED` 与 `EXHAUSTED` 是
技术/控制终态，不伪造 achieved 或 partial。

## 3. Lite Adapter

Lite 使用 `.qwenpaw/lite/goals.db`。`SQLiteGoalExecutionStore` 提供：

- `read(agent_id, conversation_id)`；
- `write(execution, expected_revision)`；
- `active_correlation(agent_id, conversation_id)`。

写入使用 `BEGIN IMMEDIATE` 和 CAS revision；同一 Goal 的身份、objective、预算和开始
时间不可漂移。active Goal 不能被另一个 `goal_id` 覆盖；只有 terminal Goal 可被新的
ACTIVE Goal 替换。数据库及 WAL/SHM 在 POSIX 上限制为 owner-only。

## 4. Invocation 与崩溃恢复

`AgentMode.on_turn_start()` 在 Agent 构建后、Stop Gate scope 评估前读取持久 Goal，恢复
的是领域快照，不是旧 Python stack。

完成采用 prepare / declare / finalize：

1. CAS 写 `OUTCOME_PENDING`，并由 `goal_id + outcome status` 生成确定性 outcome ID；
2. 当前 Invocation 通过 Host Outcome Broker 声明业务结果；
3. CAS 写 `COMPLETED` 或 `BLOCKED`。

若步骤 2 后、步骤 3 前崩溃，新 Invocation 读取 pending Goal，并用同一 outcome ID
重放。`ConversationOutcomeLookupPort` 允许 Broker 确认已存在且业务字段完全相同的
Outcome；Invocation、generation 和声明时间可以不同。任何 owner、correlation、状态、
producer、summary 或结果引用变化都以 `outcome_replay_conflict` 失败关闭。

Workspace 启动后由 Submission Dispatcher 扫描 `OUTCOME_PENDING`，创建内容安全的内部
恢复 Submission。该 Submission 继承原 correlation、固定当前 capability generation，
但不调用模型，也不产生 Assistant Message；它只通过系统 Outcome Host 完成 declare /
finalize。重复启动若已有 queued/running 恢复项则不重复入队；运行中的恢复项成为 orphan
后会标记 interrupted，并允许新的恢复尝试。由此恢复不再依赖用户补发“继续”。

## 5. Chat Submission Admission

`ConversationCorrelationResolver` 是只读 Port。Chat durable submission 在创建
`TurnSubmissionRequest` 前查询 active Goal：

- ACTIVE / OUTCOME_PENDING：继承 Goal correlation；
- COMPLETED / BLOCKED / ABANDONED / EXHAUSTED 或无 Goal：生成新 correlation。

因此，活动 Goal 中的新输入属于同一执行链，而不是因为一次 HTTP request 或 transport
session 变化被拆成新意图。Interaction、资源恢复和 Action retry 已显式携带 correlation，
不再经过此推断。

## 6. 内置与插件边界

`GoalExecution`、`GoalExecutionStore` 与 `ConversationCorrelationResolver` 从 Kernel 和
Plugin SDK 导出，Edition Adapter 可以实现相同契约，但这不授予插件共享 Goal Store
写权限。插件自有的轻量长程状态使用 namespaced `AgentModeHost.read_state/write_state`；
Host 自动绑定 Provider、Agent、Chat 和 generation。插件不能直接打开 Lite SQLite，
也不能用无类型 Mode State 修改内置 Goal 的 Outcome 状态机。

## 7. 验收

- 新 Goal 可跨 `GoalMode` 实例恢复 objective、correlation、预算与进度；
- 进度更新使用 CAS，陈旧 revision、active replacement 和契约漂移被拒绝；
- `/clear` 只把当前 Chat Goal 标记为 abandoned；
- 活动 Goal 的新 Chat Submission 继承原 correlation；
- Outcome 已写、Goal 未 finalize 的崩溃窗口可在新 Invocation 收敛；
- Outcome 尚未声明的 pending Goal 可在 Workspace 启动后主动收敛；
- 重复启动不重复排队，orphan 恢复 Invocation 可由新尝试接管；
- Outcome 重放业务字段变化时拒绝；
- 无 Chat 的兼容 Channel 保持内存 session fallback；
- 不恢复旧协程，不从 Assistant Message、SSE 或 HTTP 终态推断业务完成。
