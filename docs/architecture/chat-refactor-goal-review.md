# Chat 重构目标文档评审

> 日期：2026-10-22
> 评审对象：`chat_refactor_goal.md`、
> `research_watch_history_live_reconnect.md`
> 结论：方向正确、覆盖充分；完成下列阻断决策和文本修订后再进入设计与开发
> 后续状态：待确认项已由 `chat-refactor-decisions.md` 关闭；实施范围以
> `chat-refactor-goal-v2.md` 为准。本文仅保留最初评审证据。

## 1. 总体裁决

目标文档已经覆盖当前 Chat 的主要架构缺口，也正确限制了 Research Watch 只能作为
经验来源。历史/实时分权、完整 turn 分页、超大 block fragment、稳定 snapshot、
有界 replay/queue、窗口化渲染、兼容 Runtime WebUI、迁移失败保留原始数据等要求均
应保留。

当前版本仍不是无歧义的开发合同。主要问题不是缺少功能清单，而是部分概念在现有
代码中没有唯一对应物，且数处要求彼此冲突。如果不先处理，最容易在存储建模、
Harness 分支、`/clear`、checkpoint restore 和回滚阶段返工。

## 2. 必须调整的阻断项

### B1. “当前会话数据库”没有唯一对象

目标在第 13、252 行要求直接使用 `main` 当前会话数据库，但当前实现实际有三套
不同职责的数据：

- `chats.json`：`ChatSpec` 与分组元数据；
- `sessions/<channel>/*.json`：完整 Agent/Harness materialized state；
- `history.db/conversation_history`：Scroll write-through 历史，默认保留 30 天。

因此必须在目标中写出准确物理落点。建议明确为：

> 使用每个 Agent workspace 现有的 `history.db`，新增独立的 `chat_*` 表作为通用
> Chat transcript 的权威事实源；不得直接把 `conversation_history` 当 UI 历史表，
> 新表不继承 Scroll 的 30 天清理策略。`sessions/*.json` 只保留 runtime checkpoint，
> 正式存量 JSON 仅作为迁移输入和兼容回退材料。

同时需要决定 `ChatSpec` 是否迁入同一数据库。如果继续留在 `chats.json`，必须定义
跨 JSON/SQLite 写入失败后的恢复规则；否则“正式 chatId + branch + 首条 turn”的
创建无法由一个事务覆盖。

### B2. `revision` 同时表示三种不同概念

文档中的 `revision` 至少被用于：

1. 最后一条用户消息改写产生的不可变版本；
2. live message/block 内容逐步物化的版本；
3. snapshot 与 SSE 的传输排序依据。

这三者不能共用一个字段。建议拆为：

- `turn_revision_id` 或 `supersedes_turn_id`：用户改写产生的不可变替代版本；
- `entity_version`：同一运行中 message/block 的单调物化版本；
- `event_seq`：同一 `run_id` 内的传输顺序。

第 197–206 行的合并规则也应修改。`revision` 不应成为实体匹配 key，否则新版本会
无法命中旧实体。建议改为：

> identity key 使用 `turn_id + message_id + block_id`；tool 关联使用
> `turn_id + tool_call_id`。命中同一 identity 后，再用 `entity_version`、终态优先级
> 和 `event_seq` 决定是否更新。

### B3. 改写与 branch 的关系未闭合

当前文档既说改写产生新 revision，又说原 revision 留在旧 revision 中，但没有定义
它是否创建新 branch、移动现有 branch head，或在同一 turn 下切换 active revision。

建议选择一个明确模型：

- 每个不可变 turn 节点只包含一个 user input 与该次 run 的完整输出；
- 改写创建一个与旧 turn 共享 `parent_turn_id` 的 sibling turn；
- 新节点记录 `supersedes_turn_id`；
- 当前 branch head 原子移动到新节点；旧节点仍可审计，但不会混入当前 lineage；
- 失败或取消的新节点仍保留为 terminal turn，不回写旧节点。

还需明确 active branch 是在请求被 durable accept 后切换，还是在新回复成功后切换。
建议前者，否则失败 turn 会没有稳定归属。

### B4. Harness 目前不具备分支能力

目标要求 Codex/Qoder 等 Harness 与原生 backend 同时支持完整 turn 分支，但当前
`HarnessAdapter` 只有 `history`、`run_turn`、`reset_session`，没有 fork/clone 或从
指定 turn 恢复的协议。Codex/Qoder adapter 都只按 QwenPaw `session_id` 映射一个
provider thread/session。

因此 transcript 分支可以实现，但“继承目标 turn 对应的模型上下文”目前无法对所有
Harness 保证。不能用重放历史消息代替 fork，因为它可能重复执行工具副作用，也不能
只复制 UI 历史后继续旧 provider thread，因为旧响应仍在 provider 上下文中。

目标必须在以下方案中选择：

1. 增加 Harness `fork_session` capability；不支持 fork 的 provider 暂不开放分支；
2. 接受 capability-gated 的降级语义，并在 UI 明示“新 provider session，无法保证
   内部上下文完全继承”；
3. 将“所有 Harness 支持分支/改写”移出首期。

在现有 adapter 能力下，无法同时满足“所有 Harness”“精确继承上下文”和“不重放
工具”三个条件。

### B5. `chatId + branchId` 与共享 `session_id` 的隔离仍有冲突

第 49 行允许多个 Chat 共享 runtime `session_id`，但当前 session JSON、Harness
provider thread 映射和 Scroll history 均以 `session_id` 为核心键。只替换前端缓存 key
不能阻止模型上下文和 provider thread 串线。

建议规定：

- 旧 `session_id` 只能作为迁移 alias；
- 每个 `chatId + branchId` 必须解析到唯一的 `runtime_context_id`；
- Harness provider session/thread 映射也必须 branch-scoped；
- 新数据禁止两个可写 branch 共享同一 runtime context。

否则删除、`/clear`、改写和并发运行都会影响共享 session 的其他 Chat。

### B6. 服务重启后的恢复等级不明确

文档要求服务重启后恢复完整可见历史，但同时不持久化逐 token delta，也没有要求恢复
模型进程。建议明确三个保证：

- 已 terminal 或已确认完成的 block：RPO 0；
- 当前活跃 partial block：按时间/字节阈值物化，给出允许丢失上限；
- 服务重启后的在途 run：标记为 `failed/interrupted` 并展示最后 durable materialized
  内容；不承诺恢复同一模型/工具执行，除非 backend 提供原生 resume。

还应规定 `completed` 事件只能在最终 transcript transaction 提交之后发送。

### B7. `/clear` 是明确的用户行为变更

第 34 行要求 `/clear` 不清理前端历史，但当前行为会在收到 `clear_history` 后调用
`removeAllMessages()`；Harness bridge 也会把 materialized session context 替换为空。
这与“用户行为不回退”不是同一行为。

建议把它明确标为有意修正，并补充：

- `/clear` 在 transcript 中写入可见的 context boundary turn/event；
- 只创建新的 `context_epoch_id`，不删除旧 transcript；
- 后续模型输入不得跨越该 boundary；
- 从 boundary 之前分支时恢复旧 epoch 的 checkpoint；
- `/new` 是创建新 Chat，还是与 `/clear` 同义，需要另行定义。

### B8. checkpoint restore 与不可变历史冲突

第 261、299 行要求 checkpoint 与新数据库闭合，但当前 checkpoint restore 的语义是
恢复 session 状态。新 transcript 不可原地回退，否则会破坏审计历史和分支引用。

建议定义：

- restore checkpoint 创建新 branch/head，不改写既有 turn；
- checkpoint 必须记录对应 `chatId/branchId/turnId/context_epoch_id`；
- restore files/memory 是否同时切换 Chat branch，需要在 preview 中展示；
- checkpoint GC 不得删除仍被 branch 引用的 runtime checkpoint。

### B9. 不要求 dual-write，但未定义版本回滚

保留正式存量 JSON 只能保障旧历史仍可读，不能让旧版本读取升级后新增的 turn。
如果不 dual-write，应用版本回滚会让升级后的新消息“消失”。

目标需要选择：

- 迁移窗口内 shadow/dual-write，审计稳定后停止；或
- 明确 schema 切换后不支持直接回滚旧应用；或
- 回滚前执行新 DB 到旧格式的兼容导出。

“不要求双写”可以保留，但必须同时写明选用的回滚策略。

### B10. 性能统计方法需要修正

第 338–339 行要求每场景至少 5 次并报告 P95。5 个样本无法稳定估计 P95，结果基本
等于最大值。建议：

- 冒烟阶段 5 次；正式 P50/P95 至少 30 次，并报告样本数、离散程度和最大值；
- 明确并发度、是否启用压缩、计时边界和 SQLite 冷/热 cache；
- Research Watch 只比较共同链路，不比较其特有的“首条 user 额外注入”或只读 UI；
- 2 MiB P95 300ms 指标需要注明本机 HTTP、是否包含 JSON 序列化和网络传输；
- 设定正式单页 byte hard limit，否则“2 MiB 历史页”与分页容量目标没有边界。

## 3. 应调整但不阻断总体架构的项目

### A1. 分页限制应按层定义

“每层同时限制 turn/message/block 数”在 block fragment endpoint 上没有清晰含义。
建议改为：所有页面都有 byte hard limit；每个 endpoint 再限制其主实体数，并保证
展开的子实体不能突破 byte limit。

### A2. 并发相同 cursor 请求的“合并”限定在客户端

服务端强行合并相同查询会引入取消传播、权限上下文和共享结果生命周期问题。建议
写为：前端必须去重同 key in-flight 请求；服务端保证幂等。只有压测证明必要时才做
服务端 singleflight。

### A3. 附件生命周期未闭合

全量可见历史包含附件，但文档只要求删除数据库数据。需要定义附件是否复制到受管
存储、如何引用计数，以及删除一个 branch/Chat 时何时删除物理文件。共享附件不能被
先删除的 branch 破坏。

### A4. usage 需要区分执行成本和 active lineage

改写和分支会实际产生额外 token 成本。建议分别定义：

- turn/run usage：不可变，记录所有真实执行；
- active branch context usage：仅当前 lineage；
- Chat/Agent billing aggregate：包含被替代 branch 的真实消耗。

### A5. 增加权限、跨 Agent 和跨平台验收

cursor 除绑定 chat/branch/snapshot 外，还需绑定 Agent/用户权限上下文，防止用其他
Agent 的 cursor 读历史。SQLite migration、文件锁和删除需覆盖 macOS/Linux/Windows。

### A6. 增加故障注入

现有测试矩阵很完整，建议再加入：SQLite busy/磁盘满/提交失败、进程在 user accept
后和 terminal commit 前被强杀、重复及乱序 SSE、迁移中断重试、删除与运行并发、
branch 切换与 queued send 并发。

### A7. 存储抽象禁令需要收窄

可以禁止“可插拔的通用存储引擎”，但不应禁止具体的 transcript repository/service
模块。否则 SQL、事务和迁移容易散落在 router、TaskTracker 和 UI adapter 中。

## 4. 建议保留的内容

- Research Watch 只作经验参考，不修改 Research 页面。
- 不持久化逐 token delta，而是持久化物化 message/block。
- 完整 turn 优先、超限后逐级降级的三级分页。
- block fragment、byte limit 和 snapshot-bound opaque cursor。
- subscribe-before-sync 与 terminal 状态单调性。
- replay buffer、subscriber queue、live snapshot 和前端状态全部有界。
- 稳定实体 ID、增量合批、窗口化列表和滚动锚点。
- Runtime WebUI 默认保留，先收敛适配边界，不贸然重写成熟能力。
- 正式存量迁移失败时保留源数据并允许重试。
- 功能、性能和专项一致性三类验收矩阵。

## 5. 开发前需要用户确认的决策

1. “当前会话数据库”是否明确指每个 Agent workspace 的 `history.db`？是否同意新增
   独立 `chat_*` 表，并禁止这些表继承 30 天 purge？
2. `ChatSpec`/group 是否也迁入 SQLite，还是继续保留 `chats.json` 并接受跨存储恢复
   状态机？建议迁入，旧 JSON 只作迁移输入。
3. 最后一条用户消息改写是否采用“创建 sibling turn + 移动 branch head”的模型？
4. 改写请求 durable accept 后立即切换 active head，还是回复成功后切换？建议立即。
5. 失败/取消的改写 turn 是否保留？建议保留，并允许用户切回旧 head。
6. Codex/Qoder 不具备精确 fork 能力时，是 capability-gated、允许明确降级，还是移出
   首期？这是当前最大的范围阻断。
7. 服务重启是否只要求恢复 durable UI 历史并把 run 标记 interrupted，不要求模型和
   工具跨进程继续？建议如此。
8. partial active block 的允许丢失上限是多少？建议按 100–250ms 或 32–64 KiB 物化，
   以先达到者为准，具体值再压测。
9. `/clear` 是否确认改变为“保留 UI 历史、建立 context epoch boundary”？`/new` 是否
   必须创建新的 Chat？
10. checkpoint restore 是否确认创建新 branch，而不是回写当前历史？
11. 不采用 dual-write 时，是否接受 schema 切换后不能直接回滚旧版本？
12. Chat/branch 删除是否也删除 provider thread、runtime checkpoint 和无引用附件？
13. 历史是否永久保存直到用户删除，即不受 `history_retention_days` 影响？若是，需要
    增加容量告警/配额但不得自动删历史。
14. 正式性能 P95 是否接受至少 30 次样本，并补充并发度和 page byte hard limit？

## 6. 推荐执行顺序

1. 先回答第 5 节，修订目标文档中的术语和范围。
2. 输出数据模型与不变量：ID、branch DAG、context epoch、状态机、usage 和 GC。
3. 输出 API/SSE 契约：分页 cursor、snapshot watermark、event sequence 和 resync。
4. 输出 Harness capability matrix，先证明每个 backend 的分支/改写可达性。
5. 输出迁移、回滚和故障恢复方案。
6. 固化现有行为 contract tests 与正式 benchmark fixture。
7. 方案对齐后再进入分阶段开发。
