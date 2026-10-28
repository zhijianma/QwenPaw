# 通用 Chat 重构详细设计

> 初版日期：2026-10-22
> 当前基线：2026-10-27 working tree
> 状态：设计、实现与 Console 验收口径已校准；M0–M7 完成，V2 默认开启；
> 当前仅保留版本交付，不在本文范围内执行 commit 或发布
> 需求基线：`docs/architecture/chat-refactor-goal-v2.md`
> 决策记录：`docs/architecture/chat-refactor-decisions.md`

本文既是后续维护的规范设计，也是当前实现的结构说明。出现“必须”时表示持续有效的
合同；出现“当前实现”时表示 2026-10-27 working tree 的事实；标记为“待完成”的内容
不得被测试报告或发布说明描述为已经交付。

> **当前合同：** 一个 Chat 只有一条时间线；Copy 总是创建新的独立 Chat。schema、路由、
> 缓存和运行态均不保留同 Chat 分支兼容层。

当前完成度：

| 范围                           | 当前状态                      | 尚未闭环     |
| ------------------------------ | ----------------------------- | ------------ |
| M0–M2 事实源、迁移、统一写链路 | 已实现并定点验证              | 无实现阻断项 |
| M3 三级分页与有界前端          | 已通过                        | 无实现阻断项 |
| M4 实时、重连、重启校准        | 已实现并完成刷新/切换重连验证 | 无实现阻断项 |
| M5 独立 Chat Copy 与 `/clear`  | 已实现并完成真实 Console 验收 | 无实现阻断项 |
| M6 Checkpoint、备份、删除      | 已实现并定点验证              | 无实现阻断项 |
| M7 性能与灰度                  | 已通过并默认开启              | 仅剩版本交付 |

## 1. 设计结论

本设计选择以下总体架构：

```text
所有入口（Console / Feishu / DingTalk / QQ / Harness / Cron）
                         │
                         ▼
                 Chat Application Service
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
  Catalog Store     Conversation Store   Live Hub
  chat_catalog.db   conversations/<id>.db  进程内有界状态
        │                │                │
        │                ├── transcript ──┤
        │                ├── turn lineage
        │                ├── runtime      │
        │                └── outbox       │
        │                                 │
        └──────────────► Recall Projector ◄┘
                              │
                              ▼
                        chat_recall.db
                    （可重建的 Scroll 投影）
```

核心选择如下：

1. 每个 Agent workspace 使用一个全局目录库、每个 Chat 一个独立 SQLite 正文库、
   一个可重建的 Scroll 检索投影库。
2. 正文库是 transcript/turn/runtime 的唯一事实源；目录库规范保存 workspace 级展示
   与控制数据，并缓存正文运行态投影；Scroll 库是可重建的跨 Chat 召回投影。
3. `turn` 以父指针组成不可变 lineage；`chat_meta` 保存唯一 head 与当前上下文状态。
   产品层 Copy 会物理创建新的独立 Chat，不在源 Chat 内创建可切换状态。
4. 发送先持久化 user turn/run，再开始模型调用；流式 delta 在内存中合批，并以
   250ms 或新增 64 KiB 的先到条件物化。
5. 历史 API 固定 Chat 的读取 head，用双向 keyset cursor 分页；单实体
   过大时继续下钻到 message、block 和 fragment。
6. 重连先取得 active turn 的合并快照，再接收边界后的新事件；不逐 token 重放全部
   历史。
7. Checkpoint 对目标 Chat 正文库执行 SQLite online backup，恢复时只原子替换该正文
   库；catalog 中该 Chat 的运行态投影和 Scroll 投影随后修复，workspace 展示/控制数据
   保持当前值。
8. 切换后的 Chat 不再写旧 JSON 或 `conversation_history`。旧文件只保留升级前回退
   能力，直到用户删除 Chat。

这套布局比全局单库多了文件和连接管理，但它是同时满足“单 Chat 物理恢复”“冷 Chat
懒迁移”“删除隔离”和“全局 Scroll 搜索”的最小闭合方案。

本次仍不包含 user/assistant/reasoning/tool block 编辑，也不为“最后一条用户消息改写”
预留专用 API、表或 UI。需要该能力时另做基于不可变 turn 的设计，不能借本次实现暗中
引入可变历史。

## 2. 重构前问题证据与持续约束

本节记录立项时旧链路的问题来源，用于解释设计取舍，不代表 2026-10-27 working tree
仍按这些路径读写。当前实现证据统一列在第 25 节和完成度审计中。

### 2.1 三个事实源

- `src/qwenpaw/app/chats/repo/json_repo.py` 将 ChatSpec 和 group 写入单一
  `chats.json`。
- `src/qwenpaw/app/chats/session.py:205` 以后由 `SafeJSONSession` 按 session 写完整
  AgentState JSON；保存是整文件替换。
- `src/qwenpaw/agents/context/scroll/history.py:67` 以后定义
  `conversation_history`，Scroll 把消息再次写入 workspace 级 `history.db`。
- `src/qwenpaw/agents/context/__init__.py:58` 明确当前 `history.db` 默认存在 30 天清理
  行为，因此不能作为永久 transcript 表直接复用。

结果是：列表、当前运行状态、可见尾部和被 Scroll 移出的历史分属不同物理结构，删除、
恢复、分页和分支都没有统一事务边界。

### 2.2 当前读取与前端成本

- `src/qwenpaw/app/chats/api.py` 的 Chat detail 路径读取完整 session JSON，再把完整
  AgentState 转换为历史响应。
- `console/src/pages/Chat/sessionApi/index.ts:1617` 每次调用完整 Chat detail，
  `:1636` 对全部消息执行 `convertMessages`。
- 当前 SDK 的 Runtime WebUI 分页位于
  `console/node_modules/@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/Chat/MessageList/index.js:53`，
  只是从已经全量在内存中的数组切出 10 条显示；它不能降低网络、JSON 解码、转换和
  session cache 的成本。
- `console/src/pages/Chat/index.tsx:4684` 整体渲染 Runtime WebUI；现有 Chat 页和
  session adapter 均为大型单文件，实施时必须先抽边界，不能在其中继续叠加 Copy 和分页
  状态。

### 2.3 当前实时与恢复限制

- `src/qwenpaw/app/task_tracker.py` 的 active run、replay buffer 和 subscriber queue
  都只在进程内；buffer 和 queue 当前没有按字节设界。
- 重连会重放内存 buffer，并通过 `replay_end` 分隔；进程重启后这些状态全部消失。
- `src/qwenpaw/hooks/session/session_hook.py` 主要在响应结束后保存完整 AgentState，取消
  和异常路径尽力保存，无法保证 user message 和 partial block 已先持久化。

### 2.4 Checkpoint 的硬约束

- `src/qwenpaw/checkpoints/restore.py` 当前 conversation restore 只物理写回目标 session
  JSON。
- `src/qwenpaw/checkpoints/policy.py` 将 `sessions/` 视为 checkpoint-owned 状态，而
  `history.db` 和 `chats.json` 不属于单 conversation 恢复对象。
- `src/qwenpaw/backup/_ops/create_helpers.py:71` 已使用 SQLite backup API 对
  `history.db` 创建一致性备份，证明仓库已有可复用实现先例。

因此，全局单一 `chat.db` 不能被单 conversation restore 直接覆盖；逻辑导出/导入又会
改变已确认的物理恢复语义。

## 3. 方案比较

| 方案                                       |        单 Chat 物理恢复 |         全局 Scroll | 懒迁移 |   写入事务 | 结论                   |
| ------------------------------------------ | ----------------------: | ------------------: | -----: | ---------: | ---------------------- |
| 单一 workspace `chat.db`                   | 不满足，会回滚其他 Chat |              最简单 |   一般 |     最简单 | 淘汰                   |
| 每 Chat 单库，无全局投影                   |                    满足 | 需要打开/扫描大量库 |   最好 |       简单 | 淘汰                   |
| 单库 + checkpoint 逻辑导入                 |                  可模拟 |              最简单 |   一般 |     最简单 | 改变物理恢复语义，淘汰 |
| 目录/控制库 + 每 Chat 正文库 + recall 投影 |                    满足 |                满足 |   满足 | 需投影修复 | 采用                   |

采用方案中的跨库数据不是同等事实：正文库提交成功即代表 transcript/runtime 成功；
catalog 中 workspace 级展示/控制字段有自己的单库事务；正文运行态目录投影或 recall
更新失败只产生可修复的投影延迟，不得回滚或丢弃正文。

## 4. 物理布局

每个 Agent workspace 新增：

```text
<workspace>/.qwenpaw/chat/
├── chat_catalog.db
├── chat_recall.db
└── conversations/
    ├── <conversation_uuid>.db
    └── ...
```

约束：

- 文件名只使用规范 UUID 小写十六进制和 `.db`，兼容 Windows/macOS/Linux。
- `.qwenpaw` 是应用保留目录，避免与用户创建的 `workspace/chat/` 文件目录冲突。现有
  Checkpoint 对 `.qwenpaw` 的通用排除保持不变，Chat 正文通过专用 snapshot artifact
  显式加入 Checkpoint。
- 所有 SQLite 使用 WAL、foreign keys、busy timeout 和短 `BEGIN IMMEDIATE` 写事务。
- checkpoint/backup 不直接复制带 WAL 的 live 文件，必须通过 SQLite backup API 生成
  单文件一致性 artifact。
- 每个正文库连接采用有界 LRU；冷 Chat 不常驻连接。初始上限每进程 32 个空闲连接，
  active run 的连接不参与驱逐。
- 正文库的 `PRAGMA user_version` 与 manifest schema version 都必须校验；发现未来版本
  时只读报错，禁止降级写入。

### 4.1 目录能力是否必需

在“每 Chat 一个正文库”的物理布局下，**目录能力是必需的**，否则 Chat 列表、外部
alias 路由、冷数据迁移、删除续跑和启动恢复都只能扫描全部正文库。这里要求的是一个
workspace 级的逻辑 catalog，而不是强制某个文件名或某种数据库拆分。

首期推荐独立 `chat_catalog.db`，原因是它的数据体量小、启动路径关键，并包含不能从正文
全文可靠重建的控制数据；将它与可重建且可能很大的 FTS recall 库合并，会放大锁竞争、
损坏和重建的影响面。未来可以更换物理实现，但必须保留本节的数据所有权和事务边界。
现有 `chats.json` 已承担了弱化版目录职责；本次不是凭空新增概念，而是把它升级成可校验、
可恢复的控制面。

### 4.2 目录库不是纯投影

Chat 列表不能为一次请求打开数千个正文库。字段所有权分为两类：

- catalog 规范字段：name、pinned、archived、group、列表排序等展示元数据；
- 正文库规范字段：conversation identity、安全归属、backend、head/context、run 状态、
  transcript、runtime context 和 provider alias。

catalog 缓存 run 状态、更新时间和物理路径供列表查询；这些是正文库投影。
正文库可保留展示元数据 mirror 供诊断/灾难修复，但不能在 checkpoint restore 时反向覆盖
catalog 当前 name/pin/archive/group。这样 group 更新只需要一个 catalog 事务，也保持现有
Checkpoint 不回滚 ChatSpec 展示字段的行为。

正文状态更新顺序为：

1. 正文库事务提交，并增加 `catalog_generation`；
2. 同进程立即 upsert 目录中的运行态投影；
3. 若第 2 步失败，正文库 outbox 保留 dirty generation；
4. 启动、访问 Chat、空闲维护时按 generation 修复运行态投影。

展示元数据更新先在 catalog 单库事务提交，再 best-effort 更新正文 mirror；mirror 失败不
影响列表操作，并由 repair job 修复。

Checkpoint restore 后只从正文库重建目标目录行的运行态投影和 alias index，并保留
catalog 当前展示元数据，因此不需要恢复整个目录库。

目录库包含以下逻辑表：

| 表                      | 职责                                              | 是否规范数据                 |
| ----------------------- | ------------------------------------------------- | ---------------------------- |
| `chat_catalog_entries`  | 展示元数据、物理路径、运行态摘要、generation      | 展示字段规范；运行字段投影   |
| `chat_alias_index`      | workspace 内 alias 的唯一占用与快速路由           | 路由归属规范；运行字段可投影 |
| `chat_groups`           | Console 自定义 group、顺序和 pin                  | 是；不属于单 Chat checkpoint |
| `chat_migrations`       | legacy inventory、状态、fingerprint、错误、retry  | 正文创建前是规范状态         |
| `chat_deletion_jobs`    | 可恢复删除 saga 与 tombstone                      | 是                           |
| `chat_runtime_intents`  | send/queue 的跨库恢复意图，只用于定位非终态正文库 | 是，terminal 后清除          |
| `chat_recall_sequences` | workspace recall sequence high-water/lease        | 是，只前进不回退             |
| `chat_catalog_repairs`  | 投影 repair queue/lease                           | 派生维护状态                 |

因此“目录库是投影”只特指 run 等正文运行态摘要；alias 的 workspace 路由归属、
name/pin/archive/group、
迁移、删除任务、runtime recovery intent 和 sequence allocator 是 workspace-scoped
规范控制数据。它们不被单 conversation Checkpoint 回滚。

### 4.3 Scroll recall 库只是投影

`chat_recall.db` 保存跨 Chat 的可搜索行与 FTS，不保存唯一正文。每条投影行带源坐标：
conversation、turn、message、block、稳定 recall sequence 和 source generation。

正文库事务写入 `chat_projection_outbox`。Projector 至少一次消费，以唯一源坐标幂等
upsert。删除和 checkpoint restore 都产生重建命令。投影损坏可从
全部正文库重建，不影响 transcript。

## 5. 身份与作用域

### 5.1 规范身份

- `conversation_id`：Chat 的规范 UUID；API、目录和正文文件统一使用。
- `turn_id`、`message_id`、`block_id`、`run_id`：随机 128-bit ID；排序不依赖 ID 或
  时间戳。
- `write_seq`：正文库内单调递增整数，用于提交顺序和 cursor，不跨 Chat 比较。
- `entity_version`：可变 entity 的单调版本，只用于实时去重和防止旧状态覆盖新状态。

这里不保留无用途的通用 `revision` 字段。只有确实承担“提交排序”或“实体覆盖门禁”的
字段才存在，并分别命名为 `write_seq` 和 `entity_version`。

### 5.2 legacy/provider alias

旧 `session_id`、channel/user 组合、Harness provider thread、外部平台 thread 等只作为
alias，不再充当 conversation 主键。

alias 唯一键为：

```text
(agent_id, alias_namespace, external_key, owner_scope)
```

解析结果只指向 `conversation_id`。Console 的 legacy `channel` 只参与旧数据定位，不决定
新 Chat 的业务实现；外部入口同样映射到独立 Chat。

alias 分为两个职责层：目录库 `chat_alias_index` 规范拥有 workspace 范围的路由占用，
正文库 `chat_aliases` 保存该 Chat 可随 Checkpoint 恢复的 alias 声明和 provider metadata。
正文声明不能单独取得或覆盖 workspace 路由。创建、修复、导入和 Checkpoint restore
发布 alias 前，必须在 catalog 比较既有 owner；若唯一键已属于其他 Chat，则事务失败或将
目标 Chat 隔离等待人工处理，禁止 last-write-wins 或静默窃取路由。当前实现通过 catalog
唯一索引和投影冲突拒绝落实该合同。

## 6. 正文库数据模型

下表是实施 schema 的逻辑字段。最终 SQL 可以拆分辅助索引，但不能改变约束。

### 6.1 核心表

| 表                       | 关键内容                                                                                     | 强制约束                                 |
| ------------------------ | -------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `chat_meta`              | conversation、owner、backend、head、current context、schema/generation、展示 metadata mirror | 单行；conversation ID 与文件名一致       |
| `chat_aliases`           | legacy/provider/external alias                                                               | namespace + external key + owner 唯一    |
| `chat_turns`             | parent turn、state、run、usage、时间                                                         | parent 不可变；terminal 后正文不可改     |
| `chat_messages`          | turn、role、ordinal、state、metadata                                                         | `(turn_id, ordinal)` 唯一                |
| `chat_blocks`            | message、kind、ordinal、state、长度、hash、版本、metadata                                    | `(message_id, ordinal)` 唯一             |
| `chat_block_segments`    | block、segment ordinal、byte range、payload/hash                                             | byte range 连续且不重叠                  |
| `chat_tool_links`        | tool call/result block 和 provider call ID                                                   | call 唯一；result 最多一个规范关联       |
| `chat_blocks_fts`        | 当前 conversation 的全文检索投影                                                             | derived；可从 block 重建                 |
| `chat_runs`              | turn、状态、进程 epoch、durable/live 进度、错误                                              | 每 Chat 最多一个 active run              |
| `chat_queue_items`       | 客户端幂等键、已持久化 turn、输入、附件引用、状态、顺序                                      | 同 Chat 幂等键与 ordinal 唯一；turn 非空 |
| `chat_context_revisions` | 父 context、锚定 turn、clear/compact/turn commit、runtime state ref                          | 不可变                                   |
| `chat_runtime_blobs`     | 内容寻址的压缩 runtime payload                                                               | hash 唯一；不含物理附件正文              |
| `chat_projection_outbox` | catalog/recall 投影命令和 generation                                                         | 投影确认前保留                           |
| `chat_migration_record`  | source fingerprint、版本、校验、来源信息                                                     | completed 后不可回到 legacy 可写         |

### 6.2 Turn 定义

一个 turn 包含：

1. 一个已接受的用户输入或系统触发输入；
2. 因该输入产生的 assistant/reasoning/tool call/tool result/app-visible message；
3. 一个明确终态：`completed`、`failed`、`canceled` 或 `interrupted`；
4. 该 turn 的 usage 与完成后的 context revision。

`accepted`、`running` 是非终态。只有 `completed` turn 可以作为 Chat Copy 点。取消、
失败和中断 turn 仍是完整可见 turn，只要其终态和已经物化的内容已提交，但当前不允许
从这些状态创建 Copy。

### 6.3 Block 与 segment

所有 block 都使用 segment 存储：普通 block 通常只有一个 segment，流式文本和超大
工具结果按 append-only segment 写入。这样不会每次 delta 重写整段 TEXT，也不会形成
O(n²) 拷贝。

- segment 按 UTF-8 字节边界切分，API offset 也是字节 offset。
- `chat_blocks.total_bytes` 是已 durable 长度；terminal 时写完整 SHA-256。
- partial block 只保证到最后 durable segment；恢复时标记 `interrupted`。
- 图片、上传附件和 workspace artifact 只保存引用、媒体类型、可选内容 hash，不复制
  或取得物理文件所有权。
- 单 segment 默认目标 64 KiB；结构事件、工具结果终态和 run terminal 立即 flush。

### 6.4 Tool 关联

tool call 和 result 都是可见 block，`chat_tool_links` 负责稳定关联，禁止通过相邻位置
猜测。工具开始即建立 call 行；result 到达后原子补齐 result block 和 tool status。

进程重启时：

- 有 durable result 的 tool 保持原结果；
- 只有 call、没有 terminal result 的 tool 标记 `interrupted/unknown`；
- 不自动重试，不伪造错误 result。

## 7. 不变量

实施和测试必须直接验证以下不变量：

1. 一个可见实体只属于一个 conversation；所有跨表外键最终可追溯到该 conversation。
2. 一个 turn 只有一个不可变 parent；内部 lineage 不形成环。
3. `chat_meta.head_turn_id` 是当前时间线最新已接受 turn，可处于非终态；历史分页使用从该
   head 回溯得到的最近 terminal turn 作为稳定 view head。空 Chat 的 head 为 NULL。
4. terminal turn 的 message/block 顺序和内容不可原地修改；只有状态校准字段按版本前进。
5. 每个 Chat 同时最多一个 active run；每个 client idempotency key 最多创建一个
   turn。
6. user turn durable commit 成功后才返回 accepted/开始模型；commit 失败不调用模型。
7. terminal SSE 只在包含全部 pending segment、usage、tool 状态和 run terminal 的数据库
   事务提交后发送。
8. history snapshot 或 live event 只能把 entity version 向前推进，不能回退 terminal。
9. cursor 不能跨 conversation、direction、读取 head 或实体层级使用。
10. transcript 不因 compact、clear、Scroll retention 或投影重建被删除。
11. 创建独立 Chat Copy 后，source 与 target 的 transcript、运行态和生命周期互不影响。
12. 删除 Chat 不删除附件或 workspace 文件，只删除引用。
13. 正文提交成功而投影失败时，正文仍成功；投影必须可检测和修复。
14. legacy 与 V2 对同一 Chat 不会同时可写。

数据库层至少使用 foreign key、unique index、check constraint 和 partial unique index覆盖
可表达的不变量；DAG 可达性和 terminal 规则由事务服务再次校验。

## 8. 独立 Chat Copy 与上下文状态

### 8.1 单 Chat 时间线

`chat_turns.parent_turn_id` 指向上一完整 turn，`chat_meta` 直接保存唯一 head 和 current
context。读取历史时从固定 head 沿 parent 遍历。正文库不包含分支表、分支外键或隐式
默认分支。

产品层 Copy 不复用 source Chat 的祖先行：它将复制点及之前的 lineage 复制到新的
per-Chat DB，并重新生成 conversation、session、turn、message、block 和 context identity。
这样 source/target 可独立删除、归档、Checkpoint、迁移和继续对话。

每个 turn 保存 `lineage_depth`，用于检测异常链和辅助分页。插入时必须满足：

```text
new.depth = parent.depth + 1
```

### 8.2 Context revision

可见 transcript 与模型上下文分离。`chat_context_revisions` 是不可变链，记录：

- turn commit 后可用于下一次调用的 runtime/context 状态；
- `/clear` 产生的空上下文状态；
- `/compact`、原生压缩或 Scroll eviction 产生的摘要/索引状态；
- provider/mode 等无法只从可见 transcript 推导的状态。

runtime payload 使用内容寻址压缩 blob。payload 不重复保存完整可见正文，而保存：

- 被模型上下文引用的 message/block ID；
- summary、eviction index 等派生内容；
- provider/mode 的必要 opaque state；
- schema/version 和完整性 hash。

每个 terminal turn 指向其完成时的 context revision。Chat 从 turn T 复制时，目标 Chat
使用 T 对应的 context revision，不重放工具，不再次执行压缩。

### 8.3 从完整 turn 复制 Chat

Copy 定义为从 completed turn 创建一个新的独立 Chat。请求只需 source Chat 和 source
turn。顺序如下：

1. 验证 QwenPaw capability、source Chat 可见性，以及 source turn 在固定 lineage 中且为
   `completed`；
2. 验证 source Chat 没有 active run 或 pending queue；否则返回 409，不自动取消；
3. 为目标 Chat 生成新的 `conversation_id` 和 runtime `session_id`；
4. 将 source lineage 从根到 source turn 的 transcript 与每个 turn 已提交 context revision
   复制到目标 conversation DB，重新生成目标 Chat 内部实体 ID 和 recall sequence；
5. 目标 Chat 不复制 run、queue、runtime checkpoint、provider runtime mapping、workspace、
   terminal、artifact 或物理附件文件；历史消息中的文件引用仍指向同一 workspace 文件；
6. 通过 catalog creating -> ready 发布目标 Chat，再把独立 Chat 元数据写入 Chat 列表；
7. 默认标题使用原始家族标题加递增数字（`(2)`、`(3)`），显式名称优先；source Chat
   不改变 active head/context，前端刷新侧边栏并导航到新 Chat。

目标 Chat 后续写入、删除、归档、Checkpoint 和备份生命周期均独立。任一 Chat 删除都不能
破坏另一方；来源关系只保存为审计 metadata，不参与运行时路由或权限继承。
公开 API 使用 `POST /api/chats/{chat_id}/copies` 创建独立 Chat；不存在同 Chat 分支的
list/create/activate/delete 接口，分页和 live URL 也不携带分支 identity。

Console 的创建入口放在 completed response card 的 footer，因为复制点是“用户输入 +
完整响应”组成的 terminal turn，而不是孤立的 user message。request card 不显示 Copy
图标；running、failed、canceled response 及外部 channel 不显示。点击后以上述 response
携带的稳定 turn identity 创建新的 Chat 并导航过去。

Copy 只包含可见对话 lineage 和各 turn 已提交的模型 context revision，不创建或
复制 workspace、project directory、terminal session、artifact 或物理附件文件。祖先消息中
已有的附件仍作为 transcript 引用可见，但 Copy 不复制文件正文，也不把文件状态做成
目标 Chat 私有快照。

### 8.4 `/clear`

`/clear` 是当前 Chat 上的 control operation，不是 transcript 消息：

1. 与 queued send 共用当前 Chat 的 durable operation order；
2. 它在前序操作结束后执行，后续 send 只能看到 clear 后 context；
3. 事务创建 `kind=clear` 的 context revision，并更新 `chat_meta.current_context_revision_id`；
4. 不改 head、不删除 turn、不清空 UI、不重置 usage 历史；
5. API 返回 control terminal event，前端只更新 context indicator。

从 clear 之后完成的 turn 创建 Copy 会继承 clear；从 clear 之前的 turn 创建 Copy 使用该
turn 原有 context revision，不受后来 clear 影响。当前实现不清空 MessageList，也不删除
任何可见 transcript。

## 9. 写入链路与事务

### 9.1 新 Chat

创建采用可恢复状态机而不是跨文件假事务：

1. 目录库插入 `creating` reservation 和 UUID；
2. 在临时路径创建并校验正文库；
3. `fsync` 后原子 rename 到规范路径；
4. 目录行标记 `ready`；
5. 启动修复器会删除超时且没有正文库的 reservation，或接管已有正文库的 orphan。

只有 `ready` Chat 可以接收 send。

### 9.2 Send acceptance

Console 和外部入口最终调用同一个 `accept_input()`：

1. 解析 alias 得到 conversation；
2. 通过 migration barrier；
3. 以 `(conversation_id, client_request_id)` 做幂等查询；
4. 在 catalog 以相同幂等键登记 `chat_runtime_intent=starting`；
5. 若该 Chat 正在运行，在同一正文事务内插入 `accepted` turn、durable user
   message/block 和引用该 turn 的 `pending` queue item，并推进当前 timeline head；
6. 否则一个正文事务内插入 turn、user message/block、run accepted 和 active run；
7. 正文 commit 后把 intent 更新为对应 run/queue 摘要，再返回 accepted 并启动 runtime。

intent 不是 transcript 或 run 的第二事实源：它只保证崩溃后能定位需要打开的正文库。
queue worker claim 时复用第 5 步已经创建的 turn/user message，只创建对应 run，并在同一
事务内把 queue item 从 `pending` 推进为 `completed`，禁止再创建第二份用户输入。如果
崩溃发生在第 4、5/6 或 7 步
之间，启动恢复读取正文库的实际状态，清理空 intent，或把真实非终态 turn/run/queue
校准为 interrupted。因此 accepted 的排队输入也始终具有可见、可恢复的 durable user
turn，而不是只有一条无法渲染的队列记录。

这会消除“前端先把 pending user message 放 localStorage，后端结束时才保存”的可靠性
缺口。localStorage 在过渡期只保留未被服务器 accepted 的 composer draft，不再充当
transcript 恢复源。

### 9.3 流式物化

每个 active run 有一个 materializer：

- 文本 delta 先追加到内存 segment builder；
- 距上次 durable flush 达 250ms 或新增 64 KiB 时，以先到者 flush；
- tool call/result、错误、取消、状态转换经 envelope materializer 立即 flush；
- approval 不经过 runtime envelope；`ApprovalService` 创建和决议边界直接向当前 run
  追加结构化 request/result block，同时保留现有 polling/card 行为；
- flush 事务同时更新 block bytes/version、run durable position 和 outbox；
- 失败采用有界重试；超过写入 deadline 时停止模型消费并将 run 置 failed，不能继续产生
  无法保存的不可见结果。

### 9.4 Terminal commit

runtime 结束时在一个正文库事务内：

1. flush 所有内存 segment；
2. 完成 block hash 和 tool link；
3. 写 usage/context usage；
4. 创建 terminal context revision；
5. 更新 turn/run terminal 和 `chat_meta` 的 head/current context；
6. 清 active run，调度下一 queue item；
7. 写 catalog/recall outbox。

正文提交成功后尝试清理对应 catalog runtime intent；intent 清理失败由启动/维护任务
修复，不回滚 terminal，也不阻塞已经 durable 的 terminal event。前端收到 terminal 后
再调用 tail calibration，返回的 durable 状态必须等于或新于 terminal event。

### 9.5 并发与 fencing

- 同一进程用 per-conversation async lock 保证短事务顺序。
- 跨进程依赖 SQLite transaction 和 `chat_runs` 的 active unique constraint。
- run 持有 `process_instance_id + fencing_token`；每次 materialization 校验 token。失去
  lease 的旧进程不能继续写。
- 数据库约束以 conversation 为边界限制 active run；每个 Chat 最多一个 active run。
  状态、queue 和 live hub key 都只使用 conversation identity。
- workspace 文件本身仍是共享资源，工具对文件的并发语义与不同 Chat 并发一致，不由
  Chat Copy 虚构文件隔离。

## 10. 三级分页协议

### 10.1 API

新增 V2 路径，不静默改变旧 detail API 的响应形状：

```text
GET /api/chats/{conversation_id}/view
POST /api/chats/{conversation_id}/copies

GET /api/chats/{conversation_id}/live
GET /api/chats/{conversation_id}/turns
GET /api/chats/{conversation_id}/turns/{turn_id}/messages
GET /api/chats/{conversation_id}/messages/{message_id}/blocks
GET /api/chats/{conversation_id}/blocks/{block_id}/content
```

`view` 返回 metadata、capabilities、migration 状态和 active run 摘要，不
返回完整 messages。

兼容窗口内，旧 detail API 仍可从 V2 正文库组装完整历史，供旧 Console/调用方回退；
新 Console 禁止调用它。该兼容路径可以慢，但必须正确、可观测且不能重新依赖 session
JSON。待 bundled frontend 和已知调用方全部迁移后，只能通过明确 API version/deprecation
流程移除，不能把同一路径悄悄改成尾页造成旧客户端静默缺历史。

### 10.2 Page envelope

每一级返回统一 envelope：

```json
{
  "items": [],
  "older_cursor": "opaque-or-null",
  "newer_cursor": "opaque-or-null",
  "view": {
    "conversation_id": "...",
    "head_turn_id": "...",
    "view_version": "..."
  },
  "bytes": 0,
  "truncated_entity": false
}
```

父级页面采用渐进展开，而不是只返回无法直接渲染的 ID shell：

- turn item 默认内嵌按原序排列的 message item；
- message item 默认内嵌按原序排列的 block item；
- 可安全放入当前页面预算的 block 内嵌解码后的 `content`；
- 父实体无法完整放入剩余预算时，仍返回父实体 shell，并在该实体上返回
  `child_page`、`child_older_cursor` 和 `children_complete=false`；
- block 无法完整放入预算时，只返回 metadata、有限 preview、
  `content_cursor` 和 `content_loaded=false`，正文必须经 fragment 接口读取；
- `truncated_entity=true` 只表示本页最后一个实体需要继续下钻，不表示数据缺失。

因此普通尾页一次请求即可渲染，超长 turn/message/block 才进入下一层；独立的
message、block 和 fragment API 使用相同 item 结构，供继续下钻和局部重试。所有
`content`、preview、cursor 和 envelope 字段都计入序列化字节门禁。

cursor 是 HMAC 签名的不透明 payload，至少绑定 conversation、固定 head、
`view_version`、方向、下一 parent/source position、接口层级、schema version 和权限主体。
服务端重新校验权限，
不能只信 cursor。下钻接口也必须携带父页返回的 view/cursor scope，并验证目标 turn、
message 或 block 在固定 Chat lineage 内可达，不能只凭 entity ID 读取。

### 10.3 稳定视图

`chat_meta.head_turn_id` 可以指向最新 accepted/running turn；历史 API 先从它沿 parent
回溯，派生最近的 terminal turn 作为 `view.head_turn_id`。active/running turn
只进入 live overlay，不能作为历史快照 head。terminal turn 和 parent 不可变，因此第一
页固定 `head_turn_id` 即得到稳定
读取链，不需要持有长事务或服务端 snapshot lease。后续新 turn 不会插进旧链；客户端
通过 tail endpoint 或 live stream 单独获得。

`view_version` 由固定 head 的持久版本字段计算。若固定 head 被 checkpoint restore、
删除替换，或同名 head 的持久版本发生变化，返回 `409 view_invalidated`，携带新的 head
提示；不能返回空页伪装成结束。普通新 turn 到达不会改变旧 head 的 `view_version`。

### 10.4 下钻规则

默认初始限制：

| 层级         |     数量上限 | 序列化字节上限 |
| ------------ | -----------: | -------------: |
| turn page    |      5 turns |          2 MiB |
| message page | 100 messages |          2 MiB |
| block page   |   200 blocks |          2 MiB |
| fragment     |   1 fragment |        256 KiB |

限制是双门禁，先达到者停止。若第一个 turn 就超过页字节预算，返回 turn shell、首个可容纳
的 message 子页和 message cursor；message/block 同理。任何实体都不能因为“必须返回一
条”而突破字节上限。

fragment 返回：block ID、entity version、start/end byte offset、total bytes、fragment
hash 和 terminal 时的 whole-block hash。正文使用 Base64 编码，避免 fragment 边界切断
UTF-8 多字节字符而破坏逐字节重建；默认原始 payload 上限为 180 KiB，确保 Base64、
cursor 和 envelope 的完整 JSON 响应仍不超过 256 KiB。版本变化时旧 fragment cursor
返回 409；final block 不再变化，可长期缓存。

### 10.5 Tool 完整性

普通 turn 页尽量让 tool call/result 同页；若受字节限制拆开，call block 带
`result_ref` 和 result load state，UI 显示“结果未加载”，不能显示成“没有结果”。

## 11. 实时流与断线重连

### 11.1 Durable 与 ephemeral 边界

- 正文库是 materialized transcript 权威源。
- SSE 只负责当前 run 的低延迟变化和状态。
- event sequence 只在一个 `run_epoch` 内有效；不能跨服务重启解释。
- block `entity_version` 和 durable bytes 用于 history/live 去重。

### 11.2 事件类型

最小协议：

- `run_accepted`：conversation/turn/run 和已 durable user message；
- `sync`：边界时刻 active turn 的合并快照与 barrier event sequence；
- `block_append`：block、expected start offset、payload、entity version；
- `entity_state`：tool/approval/message/run 状态；
- `usage`：turn usage/context usage；
- `resync_required`：cursor 缺口、慢订阅者、epoch 失效；
- `terminal`：仅在 terminal commit 后发送。

事件必须带 conversation/run/turn 身份。客户端发现身份与当前 path 不同，直接
丢弃并记录 metric，不能尝试猜测归属。

### 11.3 先订阅再同步

`LiveHub.subscribe()` 在同一锁内：

1. 注册一个有界 subscriber queue；
2. 捕获当前 event barrier B；
3. 捕获 copy-on-write active materialized view 引用；
4. 释放锁后序列化并发送 `sync(B)`；
5. queue 只接收 B 之后的事件。

同步期间不阻塞 runtime。active view 的 segment 是不可变引用，新增 delta 创建新尾段，
因此序列化无需持有 hub 锁。

`sync` 必须带当前 active turn 的 durable user envelope，以及每个 active partial block 的
有界内容窗口；不能只发送“durable tail 之后的增量”。原因是固定 terminal history view
可能不包含刚接受的 running turn，刷新或切回 Chat 后，前端没有可供 tail delta 拼接的
baseline。当前实现对每个 active partial block 最多保留 2 MiB UTF-8 边界安全窗口，并
携带起始 byte offset、durable/total bytes 和 entity version。前端只把该快照合并到身份
完全一致的 conversation/run/turn；baseline 缺失、offset 有洞或版本回退时重新 resync，
不得展示不完整的伪流式结果。该快照是重连状态，不是第二份 transcript 事实源。

### 11.4 有界资源

初始硬上限：

- 每 run replay/event buffer：最多 2048 个合批事件且最多 8 MiB；
- 每 subscriber queue：最多 512 个事件且最多 2 MiB；
- 前端一次 apply batch：最多 50ms 或 32 KiB，结构/terminal 立即应用；
- terminal 后保留 live summary 60 秒，随后只走 durable history。

任一上限先达到即触发策略。慢 subscriber 被关闭并收到/下次连接获得
`resync_required`，不能反压模型和主 materializer。metrics 同时记录事件数和字节数。

### 11.5 重连流程

1. 客户端携带 conversation path、run ID、run epoch 和 last event cursor 连接；
2. 若 run 仍 active，服务端按 11.3 返回合并 `sync`，再接 live；
3. 若 cursor 仍在 buffer 中，可只补 barrier 后的合批事件；否则 sync 已覆盖旧变化，不
   逐 token 重放；
4. 若 run 已 terminal，返回 terminal hint，客户端读取 durable tail 校准；
5. 若 epoch 不存在或服务已重启，返回 `resync_required(reason=epoch_lost)`，客户端读取
   durable tail；
6. 所有 apply 按 entity version 和 byte offset 幂等；offset 缺口立即 resync。

## 12. 服务重启恢复

启动只做有界 control/metadata 扫描，不解析所有 transcript：

1. 从 `chat_runtime_intents` 和目录中 `accepted/running/stopping` 摘要定位候选正文库；
2. 获取 conversation lock；
3. 以正文库实际状态为准；空/已 terminal intent 直接清理；
4. flush 已不存在，因此以最后 durable bytes 为准；
5. 将 run/turn 置 `interrupted`，未完成 block/tool 同步置 interrupted；
6. pending queue 置 `interrupted`，不自动发起；
7. 清 active lease/intent，更新 conversation head/catalog projection；
8. 用户刷新后看到最后物化内容和明确中断状态，可重新发送或显式重试。

不恢复模型调用、工具进程、Goal/Mission 执行或 queue 自动调度。启动扫描只查看目录中
非 terminal 摘要；目录不一致时由后台修复任务继续，不阻塞所有 Agent 启动。

## 13. Scroll manager 统一

### 13.1 写入

Scroll 不再把同一消息写进 `conversation_history`。context manager 只提交 context
revision/eviction state；可见正文已经由 Chat materializer 写入正文库。

### 13.2 稳定 recall sequence

当前 Scroll 的 `expand(lo, hi)` 和 eviction index 使用全局整数 sequence。为保持功能，
目录库维护 workspace 级 recall sequence allocator：

- migration 尽量保留旧 `conversation_history.seq`；
- allocator high-water 从旧最大 seq 之后开始；
- 新正文写入从目录库预留小段 sequence lease，再把 sequence 写入正文实体；
- lease 可产生空洞，但绝不复用；
- checkpoint restore 恢复旧实体及其原 sequence，目录 high-water 不回退；
- recall 库重建时复用正文中的 sequence，因此 context revision 中旧范围仍有效。

sequence lease 和正文不是跨库原子事务；预留后崩溃只留下空洞，不会冲突或丢正文。

### 13.3 独立 Chat Copy 可见性

每个 Copy 结果都是独立 conversation，Projector 只投影各 Chat 当前 head
可达的 turn。复制时为目标 blocks 分配独立 recall sequence，后续 source/target 的投影、
删除和修复互不依赖；来源 metadata 不参与召回过滤。

### 13.4 查询兼容

现有 `sessions()`、`search()`、`expand()` 和 recall tool 的响应形状保持兼容。内部查询
按以下两路合并：

1. 当前 conversation 使用本地 FTS + 当前 lineage；
2. 其他 conversation 使用 `chat_recall.db` 的 conversation 投影。

源内容需要展开时按 source coordinate 读取正文库。跨 Chat FTS 先取有界候选，再批量按
conversation 打开正文，禁止 N 行 N 次开库。两路结果按既有 rank/time 契约稳定合并并
去重。

## 14. 迁移设计

### 14.1 状态机

```text
legacy_only -> queued -> snapshotting -> normalizing -> validating
            -> ready_v2
            -> failed_retryable
```

同一 Chat 由 migration lock 和目录唯一状态串行。`ready_v2` 是不可逆写入门槛；失败前
legacy 保持唯一可写，成功后 V2 保持唯一可写。

### 14.2 启动与活跃分级

启动只读取 `chats.json` metadata、session 文件路径/stat 和 history schema，不反序列化
全部大 JSON。

优先级：

1. 正在运行的 legacy Chat（升级前先让其结束，或启动时标 interruption 后迁移）；
2. 当前打开；
3. pinned；
4. 最近更新且未归档；
5. 其他 active；
6. archived/cold，仅在访问、发送、导出或 checkpoint restore 时迁移。

后台默认单 workspace 并发 1、全进程并发 2，并受 I/O token bucket 限制。用户访问触发
的迁移可提升优先级但不启动第二个同 Chat 任务。

### 14.3 来源与合并

每次迁移先对 session JSON 取得 path lock 和稳定快照；对 `history.db` 使用 read
transaction。进入 snapshotting 前，migration barrier 停止该 Chat 接收新 legacy send，
并等待当前 legacy run 结束；若进程重启后只残留 running 标志，则先按已确认契约标记
interrupted，再开始迁移。来源职责：

- session JSON 提供当前 AgentState、可见尾部、mode/provider/runtime 信息；
- `conversation_history` 补充已经从 JSON live context 移出的旧正文；
- `chats.json` 提供 metadata、owner 和规范 session mapping。

合并沿用并强化现有
`src/qwenpaw/agents/context/scroll/sync.py:302` 的稳定 dedup 规则：message ID、tool call
ID、Scroll dedup key 优先；缺失 ID 时使用来源内稳定位置键。规则如下：

1. 同稳定键且 canonical payload hash 相同：合并一份；
2. 同稳定键但 payload 不同：迁移失败并报告冲突，不猜测覆盖；
3. history 独有行作为历史正文保留；JSON 独有行作为尾部和 runtime 保留；
4. tool result 必须能关联 call；无法关联则保留为 orphan-visible block 并标 migration
   diagnostic，不能丢弃；
5. 无法解析的原始块保存为 `legacy_unknown` block，带原始 JSON bytes 和来源，不能因
   模型校验失败静默跳过。

旧版本已经从 `history.db` 按 retention 删除、同时又不在 session JSON 中的正文无法从
现有来源恢复。迁移器必须检查 Scroll eviction/index 引用的缺失 sequence，记录
`legacy_source_gap`、缺失范围和证据，并在 UI/诊断中明确展示；不能伪造内容，也不能把
有缺口解释成迁移丢失。V2 的“不自动过期、用户删除前全量保存”从 per-Chat cutover
成功时开始保证。

### 14.4 幂等与校验

fingerprint 包含 source path、size、mtime、SHA-256、该 session history 行的
count/max-seq/canonical digest 和迁移 schema version，避免其他 Chat 写入导致全局
fingerprint 无意义变化。正文库在临时路径完整构建；通过以下校验后才原子 rename：

- `quick_check` 和 foreign key check；
- 每来源数量/字节/hash 对账；
- turn/message/block 顺序；
- tool call/result 关联；
- runtime/context 可加载；
- 随机和边界 block 逐字节对比；
- 再次执行得到相同规范 hash。

迁移进度显示在 Chat 页面独立状态区，不写入 transcript。失败保持旧页面数据可读和重试
入口，绝不显示空 Chat。

### 14.5 回退

不双写旧 JSON。升级后新消息只在 V2；旧二进制只能看到升级前历史。重新升级后 V2
仍可见。删除 Chat 时例外：同时删除旧 ChatSpec、对应 session JSON/legacy alias，避免
回退或再次迁移复活。

## 15. Checkpoint、应用备份与导入

### 15.1 创建 Checkpoint

1. 获取目标 conversation 的 checkpoint barrier；active run 不必停止，但先强制 partial
   flush，并固定已提交 generation；
2. SQLite backup 到临时单文件；
3. 执行 schema、quick check、foreign key 和 conversation identity 校验；
4. 生成 manifest：conversation ID、generation、schema、bytes、SHA-256；
5. 将正文库 artifact 和 manifest 写入现有 shadow Git checkpoint；
6. memory/files 继续遵循当前选项。

catalog 和 recall 库不进入单 Chat checkpoint：catalog 的 workspace 展示/控制数据不属于
单 Chat restore，目标 Chat 的运行态摘要可从正文修复；recall 则是完全可重建投影。内部
lineage 行、正文、context revision、runtime blob、provider alias 都在正文 artifact 内，
不得遗漏。

V2 Checkpoint 的物理粒度是整个目标 Chat，而不是一段 lineage：snapshot 包含该 Chat
正文库中的 transcript、head/context 与 runtime state；restore 也将目标 Chat 整体恢复到
该物理状态。Checkpoint preview 必须明确列出受影响 Chat；其他 Chat 不受影响。

### 15.2 恢复 Checkpoint

1. preview 验证目标、schema 和选项；
2. 要求目标 Chat 无 active run/pending queue，或由用户先停止/处理；
3. 按现有行为创建 safety checkpoint；
4. 从 Git blob 解出临时 DB，验证 identity/integrity；
5. 关闭目标连接，原子替换正文 DB，清理旧 `-wal/-shm`，fsync parent directory；
6. 重新打开并执行向前 schema migration；
7. 只重建目标目录行、alias 和 recall rows；alias 先在 catalog 做 owner 冲突预检，冲突时
   中止提交并保持当前 Chat 不变；
8. 保留 catalog 当前 name/pin/archive/group，并将其重新写入正文 mirror；
9. 失效该 conversation 的前端 cache/live epoch，并返回 `view_invalidated`；
10. 对 artifact 中遗留的 accepted/running/stopping run 执行恢复扫描并置 interrupted；
11. memory/files 只按用户选择恢复。

其他 Chat 文件和目录行不被替换。恢复不删除未选择的 workspace 文件。

### 15.3 旧 Checkpoint 兼容

refactor 前创建的 Checkpoint 只有目标 session JSON。新版本 preview 通过 artifact manifest
或内容探测区分 `legacy_session_json` 与 `conversation_sqlite`：

1. 先按现有规则创建当前 V2 safety checkpoint；
2. 把旧 JSON blob 恢复到 staging，而不是直接覆盖 live V2；
3. 只使用该 checkpoint 自带 JSON 运行 legacy normalizer，不从当前 `history.db` 拼入
   checkpoint 之后的数据；
4. 构建并校验临时 V2 正文库；
5. 按 15.2 的原子替换流程提交，并将非终态 run 标 interrupted；
6. 旧 JSON 可按现有物理路径保留兼容副本，但不重新成为可写事实源。

这属于旧物理格式的 schema upgrade，不改变“恢复目标 Chat 到 checkpoint 当时保存内容”
的语义。无法解析或校验时保持当前 Chat 不变，并保留 safety checkpoint。

### 15.4 应用级 backup/export/import

workspace backup 对 catalog 和每个正文库分别执行 SQLite backup，禁止 `rglob` 直接复制
live WAL 数据库。归档增加 `chat-storage-manifest.json`，列出 schema、每个文件 hash、
conversation identity 和是否为 derived。

- 每次尝试先固定 catalog 中所有 `ready` Chat 的初始 inventory，再依次 online backup
  对应正文库，最后 online backup catalog；
- 从 catalog snapshot 重新读取 captured inventory，只有与初始 inventory 完全相同才允许
  生成归档；漂移则丢弃本次 staging 并有界重试，连续漂移必须明确失败，不能产出看似成功
  但缺库或多库的备份；
- 正文库和 catalog 必须包含，manifest 必须覆盖每个文件的 hash、identity、schema 和引用；
- recall 库默认不包含，导入后重建；
- 旧 JSON/history 在兼容期仍随 workspace 归档；
- restore 先在 staging 校验全量 hash、identity、schema 与跨文件引用，再使用现有安全
  swap 提交；
- 冲突导入必须显式选择替换/新 Agent，不按文件时间猜测。

正文库的 runtime generation 可能在逐库 snapshot 期间前进，但 runtime/catalog 摘要和
recall 都是可从正文重建的派生状态；导入后统一以各正文库权威状态修复。inventory 稳定
门禁保证备份至少是引用闭合的 workspace 集合，不把多文件 backup 伪装成全局同一时刻
事务快照。

## 16. 删除语义

文件、数据库和 checkpoint refs 无法放进单 SQLite 事务，因此删除使用可恢复 saga：

1. 目录事务把 Chat 标记 `deleting` 并写 deletion job；之后所有 send/open/Copy 写操作
   返回明确 deleting 状态；
2. 停止 active run，关闭 subscriber，把 run/queue 标 terminal；
3. 删除 Chat 关联的 checkpoint refs、provider aliases、usage/run ownership；
4. 从 recall 投影删除源坐标；
5. 删除正文 DB 及 sidecar；
6. 删除对应 legacy session JSON 和 `chats.json` 行；
7. 目录保留最小 tombstone 防止旧 orphan 被重新迁移；完成保留期后可压缩 tombstone。

任一步崩溃由 deletion worker 从 job 续跑，操作全部幂等。删除不递归删除附件路径、
workspace 文件或 artifact；只移除 block 引用。物理安全擦除和孤儿附件 GC 不在本次范围。

## 17. 前端状态与渲染

### 17.1 模块边界

从当前 Chat 大文件中逐步抽出：

```text
ChatRouteController       路由、Agent、conversation path 生命周期
ChatSessionAdapter        SDK identity 与 V2 view/tail 适配
ChatHistoryStore          normalized entities、page/cursor、缓存预算
ChatLiveController        subscribe/sync/dedup/resync/terminal calibration
ChatCopyController        capability、completed-turn copy、列表刷新与跳转
ChatTimelineWindow        有界窗口、锚点、历史 sentinel
ChatBlockRenderer         lazy block/fragment、Markdown/tool/card
ChatComposerController    send/queue/stop/clear
```

Zustand store 按 `(agent_id, conversation_id)` 分区。切换 path 时增加 owner epoch；
所有异步响应在写 store 前校验 epoch，保留现有防串线机制。

### 17.2 Normalized store

store 使用 ID map 和 order arrays，不把一个 turn 多次复制进 SDK session cache：

- `turnsById/messagesById/blocksById`；
- `historyPages` 保存 cursor、固定 head 和 entity IDs；
- `liveOverlay` 只保存 active turn 可变尾部；
- finalized entity 使用结构共享；
- active block 只有尾 segment 改变；
- cache 同时按 entity 数和 UTF-8 bytes 驱逐。

当前 active path 最多保留 14 个 normalized page ref、12 MiB decoded payload：其中预算
覆盖 6 个 turn page，以及最多 8 个 message/block continuation 请求。后台 path 最多保留
2 个 page ref、4 MiB；active live turn 不因历史驱逐丢失。数量与字节任一先到即驱逐，
正式阈值只能依据基准调整。

### 17.3 与 Runtime WebUI 的兼容

保留 SDK 的 composer、发送生命周期、现有 cardConfig、approval、附件和 imperative API。
但不再把全量历史交给 SDK：

- 初始只转换尾页；
- 用 UI-only `HistoryBoundary` sentinel 触发真实后端 cursor 加载；sentinel 不进入请求、
  transcript 或模型上下文；
- normalized store 的 decoded cache 与 SDK 挂载窗口是两层状态：前者服务前后翻页和
  快速返回，后者只保存视口附近的完整 turn；已加载但离窗的 entity 不转换成 SDK
  message，也不得形成 Bubble DOM；
- 挂载窗口以完整 turn 为移动和驱逐单位，禁止按持久化 message 条数截断。工具密集
  turn 可能包含数十条 user/assistant/tool message，但转换后通常仍只有一个 request
  Bubble 和一个 response Bubble；按 message 截断会破坏 tool call/result 配对和完整
  turn 完整性语义；
- 候选初始预算为最多 24 个完整 turn、通常最多 48 个 Bubble、最多 2 MiB 已转换 payload，
  三者先达到者触发驱逐。任何预算都不得切开 turn；单个 turn 自身超限时允许只挂载该
  turn，并继续依赖 block preview/fragment 下钻限制正文；
- 后端 turn page 和窗口移动量固定为 5 个完整 turn（在当前转换不变量下通常为 10 个
  Bubble），保留前后重叠以减少边界抖动。首个 5-turn 页渲染后，如果实际消息高度不足以
  覆盖 MessageList 视口及 overscan，前端继续串行请求更旧的 5-turn 页，直到填满、到达
  历史边界或触发挂载/bytes 上限；同一 cursor 只能有一个 in-flight 请求，连续无新增 turn
  时必须停止，避免大窗口、短 turn 场景留白或形成自动加载死循环；
- 向上到达本地窗口边界时先在 normalized cache 内移动，仅在 cache 无更旧 turn 时使用
  `older_cursor` 拉取；向下同理使用 `newer_cursor`。深度向上浏览导致尾页被 cache 驱逐后，
  “回到最新”必须重新拉尾页并原子重置窗口，不能要求刷新整个 Chat；
- live 状态只在尾部窗口合并。用户阅读旧历史时，不把 live tail 注入当前 SDK message
  数组、不抢滚动，只更新“有新内容/回到最新”状态；用户回到尾部后再校准 durable tail；
- SDK 当前“内存内 10 条分页”关闭，避免双重分页；
- SDK 自带 `UserMessageAnchors` 只索引已挂载 message，无法导航离窗 turn。当前实现以
  `searchChats` 生成轻量 user-turn 索引，点击后由 `loadHistoryAtTurn` 请求以目标 turn
  为中心的后端页，再由 `createTimelineWindowAtTurn` 构造最多 5-turn 的本地窗口并滚动到
  稳定 user Bubble ID；目标已经在 normalized cache 时只移动窗口，不发重复请求。503
  legacy 回退继续使用 SDK 原行为；
- 在切换 SDK 版本前增加 contract test，验证 card、approval、scroll、session activation
  和 history 标记行为。

当前有界 adapter 已通过冻结数据集的 DOM、React commit、long-task 和 heap 门槛，因此
不引入第二套 Island/Gap 或 variable-size virtualizer。借鉴桌面长历史产品的原则仅限于
“数据分页、局部挂载、稳定锚点、重内容懒化、离窗精确 seek”这些不变量，不照抄其组件
层级或缓存架构。只有后续真实分布再次突破门槛，才评估自有 virtual list。无论采用何种
渲染器，“全量消息仍在 JS 内存”都不算窗口化完成。

### 17.4 滚动锚点

窗口移动或补页前记录第一个可见 user Bubble 对应的稳定 turn ID 与 viewport top。历史
response card ID 会在转换时重建，不能作为 anchor。原子替换 SDK message 后在下一
layout frame 找到同一 user Bubble，按 top 差值修正滚动；不能保存旧 `scrollTop`。

- history owner ready 后关闭浏览器原生 `overflow-anchor`，避免浏览器与应用双重补偿；
- bridge 在 MessageList root 出现时即建立观察，owner 尚未 ready 时等待，ready 后再绑定
  分页状态，不能因 root 早于 owner 挂载而永久漏绑；
- 首屏不足视口时以 160px overscan 串行补旧页；距底部 96px 内视为 live edge；
- 用户离开底部后不抢滚动，显示“有新内容”；
- 向上、向下反复移动均须保持 anchor 偏移不超过 2px，Bubble roots 不随累计翻页单调
  增长；
- 旧页加载失败保留原窗口和重试 sentinel；
- Chat/path 切换保存各自 anchor，不共享可变尾页；分页 loading lock 必须绑定本次
  effect/owner epoch，旧 Chat 的 `finally` 不能解锁新 Chat 的请求；
- “回到最新”先重取 view：若 `view_version` 或 Chat head identity 变化则重建
  session；否则重新请求 tail，并原子替换 normalized pages、cursor 与挂载窗口。

### 17.5 超大 block 与重渲染

- collapsed reasoning/tool result 只取 metadata 和 preview，不请求完整 fragment；
- 展开时按 fragment 拉取，并允许取消；
- finalized Markdown 按 `(block_id, hash)` memoize；
- active Markdown 把稳定前缀和尾部拆开，最多每 50ms 或 32 KiB 更新一次；
- 代码高亮、Mermaid、大 JSON viewer 在可见/展开后懒加载；
- 大量转换每批后通过 scheduler 让出主线程；不在 render 中对多 MiB payload 做
  `JSON.parse/stringify`、深拷贝或全文 hash。

## 18. Capability 与入口行为

后端 `view.capabilities` 至少返回：

```json
{
  "turn_pagination": true,
  "message_pagination": true,
  "block_pagination": true,
  "block_fragments": true,
  "live_resync": true,
  "chat_copy": true,
  "edit_last_user_message": false
}
```

- QwenPaw 原生 backend：`chat_copy=true`；Copy 结果是独立 Chat。
- Harness 和 Feishu/DingTalk/QQ 等外部入口不开放 Copy；它们仍使用同一单 Chat
  时间线、持久化、分页与实时链路。Console 打开这些 Chat 时不显示 Copy 操作。
- Console 逻辑只看 capability，不散落 provider/channel 名称判断。

所有入口使用同一个 application service、acceptance 事务、materializer 和 recovery；渠道
adapter 只负责输入/输出格式及 alias 解析。

## 19. 错误模型与可观测性

### 19.1 API 错误

必须区分：

- `migration_pending/failed`；
- `view_invalidated`；
- `invalid_cursor`、`invalid_live_cursor`；
- `entity_version_changed`、`history_entity_not_found`；
- `run_conflict/queue_conflict`；
- `copy_unsupported`、`copy_turn_unreachable`、`copy_turn_not_completed`、
  `copy_context_missing`、`chat_busy`；
- `resync_required`；
- `conversation_deleting/deleted`；
- `storage_unavailable/corrupt`。

不得把数据库错误转换成空历史或 no-more。

### 19.2 Metrics

至少记录：

- page query/serialize bytes、P50/P95、下钻率；
- migration queue time、duration、source bytes、dedup/conflict/失败；
- materialization interval/bytes、flush latency/retry/failure；
- active run event buffer/subscriber queue 当前值和峰值；
- reconnect reason、sync bytes/time、resync 次数；
- projector lag（generation/seconds）、rebuild duration；
- open SQLite connections、busy duration、WAL bytes；
- frontend fetch/decode/convert/React commit、DOM、heap、long task；
- checkpoint snapshot/restore bytes、duration、validation failure。

日志使用 conversation/run 的截短或 hash 关联键，不记录正文、token、附件内容和凭据。

## 20. 性能验收设计

### 20.1 正确性 oracle 先行

基准数据生成器同时生成规范 manifest：每层数量、顺序、每 block SHA-256、tool link、
turn state、usage 和 head path。重构前后先通过 manifest 等价，再比较性能；少加载、
截断或漏卡片会直接判失败。

### 20.2 冻结数据集

实施 M0 先采集脱敏分布并调整下列最低覆盖；调整只能扩大真实高分位覆盖，不能降低最低
压力：

| 数据集          | 最低规模                                        | 目的                  |
| --------------- | ----------------------------------------------- | --------------------- |
| Typical         | 100 turns / 500 blocks / 5 MiB                  | 回归与低延迟          |
| Long            | 2,000 turns / 10,000 messages / 100 MiB         | 正式 30 样本          |
| Extreme history | 10,000 turns / 50,000 messages / 1 GiB logical  | 资源压力，至少 5 样本 |
| Tool dense      | 500 turns / 10,000 tool pairs / 256 MiB         | 关联与折叠渲染        |
| Huge block      | 单 block 64 MiB                                 | fragment 与展开取消   |
| Chat Copy       | 2,000-turn source + 20 copies + 200-turn suffix | 复制成本与相互隔离    |
| Long run        | 2 小时、20 合批事件/秒、周期 tool block         | 稳态资源与重连        |
| Concurrent      | 8 active Chats、每 run 2 subscribers            | contention 与慢订阅者 |

正式场景冷/热缓存各至少 30 个样本；Extreme/soak 单独报告持续时间和峰值，不用 30 次
掩盖资源成本。

### 20.3 门槛

除 Goal V2 既有门槛外，增加：

- 冷打开 Long：P95 在 1.5 秒内出现可阅读尾部；页面控制在此期间可交互；
- warm 切换 Long：P95 ≤ 500ms；
- 2 MiB turn page 后端 P95 ≤ 300ms；
- 单次 page 的服务端峰值额外内存 ≤ 返回 payload 的 3 倍；
- active timeline 挂载窗口默认 ≤24 turns、≤48 Bubble roots、≤2 MiB converted payload，
  三者先到者为准；单个超限 turn 允许独占窗口；
- 30 分钟稳态后，排除明确 cache 后的 server/frontend retained heap 增长 ≤ 20%；
- subscriber queue 和 replay buffer 永不超过 11.4 的事件/字节硬上限；
- 前端不得出现 >1 秒 long task；P95 React commit 目标 ≤ 50ms；
- 向上补页后 anchor 偏移 ≤ 2px（动态媒体加载造成的后续变化单独补偿）；
- 64 MiB block 可逐字节重建，折叠状态不得下载完整正文；
- 相对 main 的核心 P50 目标提升 ≥20%；原本 <200ms 的行为回退 ≤10%。

### 20.4 测量方法

- 固定机器、浏览器版本、viewport、CPU/网络 throttling、并发、数据库状态；
- server 使用 monotonic clock 分解 query/encode/write；前端使用 Performance marks、React
  Profiler、Long Tasks 和 heap snapshot；
- 冷缓存重启目标服务并清应用 cache，但不改变数据；热缓存执行固定预热脚本；
- 报 P50/P95/max、标准差或 IQR、样本数、失败数和 payload；
- 当前 main、Research Watch 共同链路和新 Chat 使用同一 manifest/操作脚本；
- 原始结果保存为机器可读 JSON，报告只引用聚合值。

## 21. 回归基线

### 21.1 自动化基线

实施前固定并定点运行现有相关测试，不执行未经授权的全量 npm test：

- `tests/integration/test_chat_history_conversion.py`
- `tests/integration/test_console_chat_task.py`
- `tests/integration/test_chat_archive.py`
- `tests/integration/test_checkpoints_router.py`
- `tests/unit/app/routers/test_console_chat_reconnect.py`
- `tests/unit/app/routers/test_console_chat_stop.py`
- `tests/unit/agents/context/test_scroll_manager.py`
- `tests/unit/checkpoints/test_checkpoint_restore.py`
- `console/src/pages/Chat/tests/testLargeSession.test.ts`
- `console/src/pages/Chat/sdkSessionLifecycle.integration.test.tsx`

每个里程碑再增加对应 V2 contract/fault/performance test。现有测试通过只代表部分基线，
不能替代用户行为矩阵。

### 21.2 人工交互矩阵

逐项录制预期：发送、排队、停止、取消、失败、刷新、切 Chat、后台运行、reasoning、
并行 tool、审批、附件、插件卡、usage、compact、clear、archive、delete、checkpoint、
Harness、外部渠道。对视觉、滚动和错误文案保留截图/录屏基线。

明确批准的行为变化只有：

- `/clear` 不再清空可见 transcript；
- 回退旧版本看不到升级后的新消息；
- Copy 只在 QwenPaw 的 completed response card 出现，结果是独立 Chat；
- 服务重启显示 interrupted，不自动续执行。

其他变化必须在实施前新增决策记录。

## 22. 分阶段实施与发布

### M0：基线与脚手架

- 冻结正确性 manifest、功能矩阵和性能数据集；
- 记录 main 基线；
- 建 schema validator、fault injection 和 metrics 骨架。

门禁：数据集可重复、oracle 能发现缺失/重复/乱序/tool 错配。

### M1：存储与迁移

- catalog/conversation/recall schema；
- repository、connection LRU、outbox/projector；
- legacy migrator 和只读校验工具；
- checkpoint/backup 所需 online snapshot primitive。

门禁：幂等迁移、冲突失败、冷 Chat 不启动全量解析、单库恢复实验通过。

### M2：统一写链路

- QwenPaw、Harness、外部 channel 接入同一 acceptance/materializer；
- Chat-scoped queue、runtime context、Scroll adapter；
- legacy per-Chat cutover barrier，无双写。

门禁：现有发送/工具/审批/usage/取消回归通过，进程 kill 不丢已物化数据。

### M3：真实分页与有界前端（已完成）

- V2 view/turn/message/block/fragment API；
- normalized store、5-turn 双向分页、视口不足自动补页、回到最新、稳定锚点、lazy block；
- 移除 full AgentState detail 依赖和 pending transcript localStorage。

门禁：Long/Huge/Tool dense 正确性和性能门槛，以及离窗 user-turn 精确导航、稳定锚点和
目标中心 5-turn 窗口均已通过定点验证。

### M4：实时与重连（已完成）

- LiveHub、subscribe-before-sync、bounded queue/buffer；
- durable terminal、gap/resync、restart interrupted；
- 前端 live overlay 和最终校准。

门禁：断网、刷新、慢 subscriber、service kill、迟到事件故障注入通过。

### M5：独立 Chat Copy 与 `/clear`（已完成）

- context revision、Chat Copy API/capability/response-card UI；
- 完整 turn 复制成独立 Chat、侧边栏刷新和导航；
- clear control operation 和前后 Copy 用例。

门禁：source/target 相互隔离、queue/run 冲突、Scroll visibility、Checkpoint 前后全部通过。

### M6：Checkpoint、删除与应用备份闭环（已完成）

- checkpoint artifact/restore；
- backup/import manifest；
- delete saga 和 legacy 防复活；
- catalog/recall repair tooling。

门禁：单 Chat restore 不影响其他 Chat，删除/恢复/导入崩溃恢复通过。

### M7：灰度与默认开启（已完成）

per-Chat 状态为 `legacy_only`、`migrating`、`ready_v2`。先内部 Agent、新 Chat、活跃 Chat，
再按需迁移冷 Chat。灰度期允许 shadow-read 对账，但禁止双写。渠道定点范围按用户确认
收敛为 Console；用户于 2026-10-23 确认正式默认开启。

回滚分两类：

- 新二进制内回滚：未 cutover Chat 可继续 legacy；已 `ready_v2` Chat 不能切回 legacy 写，
  只能关闭新 UI 功能并继续 V2；
- 旧二进制回滚：按已确认契约只能看到升级前 JSON，升级后的消息不丢失但暂不可见。

## 23. 发布阻断条件

以下任一成立均不能默认开启：

- migration 校验存在未解释的缺失、重复或冲突；
- user accepted 后仍可能没有 durable user turn；
- terminal event 可先于 terminal transaction；
- checkpoint 恢复需要覆盖共享 catalog/recall 或会影响其他 Chat；
- source/target Chat 的 Scroll、上下文、run 或 suffix 发生串线；
- 删除后 legacy 数据可再次迁移复活；
- 页面仍在打开时下载/转换全量历史；
- buffer、queue、DOM、decoded cache 或连接数缺少字节/数量硬上限；
- 离窗 user-turn 精确导航被声明完成但不能稳定 seek 并恢复锚点；
- 当前适用功能矩阵出现未批准回退；
- Long/Tool dense/Long run 没达到冻结门槛或没有可靠原始数据。

## 24. 已落地参数与可调边界

以下当前值已经落地并纳入定点/性能验证：

- turn page 与窗口移动量：5 turns；page 字节上限：2 MiB；
- MessageList 挂载窗口：24 turns / 48 Bubble roots / 2 MiB converted payload；
- active normalized cache：14 page refs / 12 MiB；background：2 page refs / 4 MiB；
- partial block：250ms / 64 KiB，先到者 flush；
- live replay：2,048 events / 8 MiB；subscriber：512 events / 2 MiB。

以下仍是可通过基准调整的工程参数，不是产品范围问题：

- connection LRU 数量；
- migration 并发和 I/O token；
- runtime blob 压缩算法与 snapshot/delta 间隔；
- tombstone 保留时间。

调整这些参数必须有 benchmark 或真实分布数据，并更新本设计和测试；不得改变全文中的
事实源、隔离、恢复、无双写和数据不丢失不变量。

## 25. 需求、证据与验收追踪矩阵

本节用于防止维护阶段只修改 schema 或 UI，却遗漏链路闭环。第二列指向当前 working tree 的
实现所有者；验证结果与剩余项以 `chat-refactor-m3-m7-completion-audit.md` 为准。

| 已确认需求                                   | 当前实现证据                                                                            | 设计落点       | 验收/Checklist                                                         |
| -------------------------------------------- | --------------------------------------------------------------------------------------- | -------------- | ---------------------------------------------------------------------- |
| JSON 与 Scroll history 统一为新事实源        | `storage/conversation.py`、`application.py`、`scroll_bridge.py`                         | 1、6、9、13    | M1/M2 storage、migration、Scroll、workspace 定点测试                   |
| 不复用 30 天清理的 `conversation_history`    | 每 Chat DB 保存正文；`chat_recall.db` 仅为可重建投影                                    | 2.1、13、14    | retention/compact/clear/rebuild 均不删除正文                           |
| 所有入口统一，Copy 只开放 QwenPaw Console    | `application.py` 统一 acceptance；`view.capabilities` 返回 backend-scoped `chat_copy`   | 5、9.2、18     | Console 完整验收；Feishu 真机收发补充验证后恢复关闭                    |
| 全量历史保存到用户删除 Chat                  | materializer/outbox 写入正文库；自动 retention 不作用于 transcript                      | 7.10、13、16   | 仅 delete saga 删除正文；source gap 单独诊断                           |
| completed turn 复制独立 Chat，不做编辑       | `storage/copy_repository.py`、`POST /api/chats/{id}/copies`、response-card Copy UI      | 6.2、7、8      | source/target 隔离、数字标题、真实 continuation；无 edit API/UI/schema |
| `/clear` 只清模型上下文                      | builtin command 标记 `chat_context_revision_kind=clear`；materializer 写 clear revision | 8.2、8.4       | clear 前后 Copy、可见 transcript 不变                                  |
| turn/message/block 三级分页与超大 block      | `pagination_repository.py`、`pagination_cursor.py`、`chatHistoryStore.ts`               | 10、17         | 5-turn/2 MiB 双门禁、64 MiB block、双向 cursor、anchor                 |
| MessageList 只挂载小窗口                     | `chatTimelineWindow.ts`、`ChatHistoryPaginationBridge.tsx`                              | 17、20         | 24 turns / 48 roots / 2 MiB；视口不足自动补页                          |
| 稳定实时流和断线重连                         | `event_materializer.py`、`live.py`、`chatLiveController.ts`                             | 9、11          | kill、sync、duplicate/gap/overflow、2 小时 soak                        |
| partial block 250ms 或 64 KiB 先到           | `event_materializer.py` 与 `storage/materializer.py`                                    | 6.3、9.3       | 阈值前后 kill，校验 durable byte boundary                              |
| 服务重启只恢复历史，不恢复执行               | `application.py` recovery 将非终态 run/queue/tool 校准为 interrupted                    | 12             | 无模型、工具或 queue 自动重启                                          |
| Checkpoint 保持物理恢复并支持新数据          | `checkpoints/chat_artifact.py`、`checkpoints/restore.py`                                | 4、15          | 整个目标 Chat DB 恢复；其他 Chat hash/head 不变                        |
| 保留旧 JSON、不双写、旧版只见升级前历史      | `migration/` 与 rollout/cutover 写门禁                                                  | 14.5、22       | `ready_v2` 后 legacy 不再写；旧版回退演练                              |
| 活跃分级迁移，冷/归档按需                    | `migration/inventory.py`、`migration/orchestrator.py`                                   | 14.1、14.2     | startup 不解析全量 JSON；访问 barrier 与 kill/retry                    |
| 删除 Chat 清运行数据但不删附件/工作区文件    | `deletion_service.py`、`storage/deletion.py`                                            | 6.3、7.12、16  | delete saga crash-resume；legacy 防复活；物理文件保留                  |
| Scroll manager 改读写新数据并支持 Chat 隔离  | `scroll_bridge.py`、`storage/recall.py`、projection outbox                              | 13             | source/target 独立投影；无跨 Chat suffix 泄漏                          |
| 长历史、长任务、工具密集性能改善且功能不回退 | normalized cache、有界窗口、lazy block 与 M7 benchmark 原始 JSON                        | 17、20、21、23 | cold/warm、heap、long task、Extreme、8 GiB VM 门禁                     |
| 离窗用户消息精确导航                         | `searchChats`、`loadHistoryAtTurn`、`createTimelineWindowAtTurn`、pagination bridge     | 17.3、22、23   | target-turn page + 5-turn 本地窗口 + 稳定 user Bubble 定位             |

## 26. 已确认的产品与架构决策

以下选择已经确认并在当前实现中落地。若后续改变，应先更新决策记录、兼容合同和回归
矩阵，再修改业务代码：

1. 采用独立 `chat_catalog.db` 作为 workspace 控制面；catalog 逻辑能力必需，但文件名和
   未来物理实现不是永久公共契约。
2. Chat Copy 只允许从 `completed` response turn 创建；失败、取消和中断 turn 不可复制。
3. Copy 结果是独立 Chat，删除 source 或 target 不影响另一方，也不级联删除。
4. Checkpoint 恢复目标 Chat 的完整 conversation DB，包括 transcript 与 head/context。
5. 前端保留 Runtime WebUI 的 composer/card/approval 能力，并以 normalized cache + 有界
   adapter 控制挂载窗口；当前冻结性能门槛已通过，不新增第二套 virtualizer。
6. 本阶段不提供最后一条用户消息编辑；`/clear` 只切换 context revision，不改变可见历史。

## 27. 最小 Schema 与状态机合同

本节给出实现和 migration validator 必须识别的最小合同。允许调整物理列名、拆表或增加
索引，但必须证明等价地满足约束；不能把下列规范数据降级为无法校验的 JSON 大字段。
ID 使用 16-byte UUID/BLOB 或等价的 128-bit 随机值，时间统一保存 UTC 整数微秒。

### 27.1 Catalog 最小合同

| 逻辑表                  | 最小字段                                                                                                                                                       | 关键约束/索引                                                                    |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `chat_catalog_entries`  | conversation、agent/owner、display metadata、physical path、lifecycle、active run projection、Copy source metadata、source/catalog generation、created/updated | conversation PK；规范化路径唯一；`(agent, archived, pinned, updated)` 列表索引   |
| `chat_alias_index`      | agent、namespace、owner scope、external key、conversation、source generation                                                                                   | alias 四元组 workspace 唯一；既有 owner 不可被投影覆盖；conversation FK/删除级联 |
| `chat_groups`           | group、agent、name、ordinal、created/updated                                                                                                                   | `(agent, ordinal)`、`(agent, normalized name)` 唯一                              |
| `chat_migrations`       | conversation/source locator、state、priority、fingerprint、schema version、progress、attempt、lease、error digest                                              | 每 source/conversation 一个 active migration；lease 可过期接管                   |
| `chat_deletion_jobs`    | conversation、state、step bitmap、attempt、lease、last error、timestamps                                                                                       | conversation 唯一；completed 前 tombstone 不移除                                 |
| `chat_runtime_intents`  | conversation、client request、kind、run/queue、state、created/updated                                                                                          | `(conversation, client request)` 唯一；非终态扫描索引                            |
| `chat_recall_sequences` | workspace、high water、lease owner/end/expiry                                                                                                                  | workspace 单行；high water 只增不减                                              |
| `chat_catalog_repairs`  | conversation、kind、expected generation、state、lease、attempt                                                                                                 | `(conversation, kind, expected generation)` 幂等唯一                             |

catalog 不保存 message/block 正文。`external_key` 如含用户标识，日志只写 hash；数据库中
保存路由所需原值时必须沿用 workspace 权限和本地数据保护边界。

### 27.2 每 Chat 正文库最小合同

| 逻辑表                   | 最小字段                                                                                                                  | 关键约束/索引                                                            |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `chat_meta`              | conversation、agent/owner、backend、head turn、current context、next write seq、source/catalog generation、schema version | 严格一行；conversation 与 manifest/文件 identity 相同                    |
| `chat_aliases`           | alias identity、provider metadata、created/updated                                                                        | alias identity 唯一                                                      |
| `chat_turns`             | turn、parent turn、lineage depth、run、state、context result、usage、write seq、timestamps                                | parent 不可变；write seq 唯一；`(parent, depth)` 校验由服务完成          |
| `chat_messages`          | message、turn、role、ordinal、state、write seq、metadata                                                                  | `(turn, ordinal)` 唯一；role/state CHECK                                 |
| `chat_blocks`            | block、message、kind、ordinal、state、total/durable bytes、whole hash、entity version、write seq、preview/metadata        | `(message, ordinal)` 唯一；bytes 非负；version 单调                      |
| `chat_block_segments`    | block、ordinal、start/end offset、payload、segment hash、write seq                                                        | `(block, ordinal)`、`(block, start)` 唯一；`end > start`；连续性终态校验 |
| `chat_tool_links`        | provider call、call block、result block、state、write seq                                                                 | call block/provider call 唯一；result block 最多关联一次                 |
| `chat_runs`              | run、turn、state、process epoch、fencing token、event/durable progress、error、timestamps                                 | 每 Chat 非终态 partial unique；turn/run 一对一                           |
| `chat_queue_items`       | queue item、turn、client request、ordinal、serialized input、attachment refs、state、timestamps                           | turn 非空；`(client request)` 和 `(ordinal)` 在 Chat 内唯一              |
| `chat_context_revisions` | context、parent context、kind、anchor turn、runtime blob、write seq、created                                              | immutable；parent/anchor/blob FK                                         |
| `chat_runtime_blobs`     | content hash、codec、schema、raw/stored bytes、payload、created                                                           | content hash PK；长度与 hash 校验                                        |
| `chat_projection_outbox` | outbox、kind、source coordinate、source generation、payload、state、attempt                                               | source generation 幂等唯一；pending 索引                                 |
| `chat_migration_record`  | source fingerprint、migration version、canonical hash/counts、diagnostics、completed                                      | 正文库只保存最后成功记录和来源证明                                       |

全文索引仅保存可搜索文本及 source coordinate；不是 FK 事实源。所有业务查询必须先由
关系表验证 conversation lineage，再读取 FTS 命中，不能只信 FTS 行。

### 27.3 必需状态迁移

状态变化只允许沿下列边前进；校准可把遗留非终态转为 `interrupted`，不能把 terminal
状态改回 running：

```text
Chat      creating -> ready|quarantined
          ready|quarantined -> deleting -> deleted
Turn      accepted -> running|failed|canceled|interrupted
          running -> completed|failed|canceled|interrupted
Run       accepted -> running|failed|canceled|interrupted
          running -> stopping|completed|failed|canceled|interrupted
          stopping -> completed|failed|canceled|interrupted
Queue     pending -> completed|canceled|interrupted
          starting -> completed|canceled|interrupted
Block     (create completed) | partial -> completed|failed|interrupted
Migration legacy_only -> queued -> snapshotting -> normalizing -> validating
          -> ready_v2 | failed_retryable | failed_terminal
```

规则：

- `completed/failed/canceled/interrupted/deleted/ready_v2/failed_terminal` 是相应领域的
  终态；
- Queue 的 `completed` 表示该项已被原子提升为 run；其 durable turn/user message 在
  `pending` 创建时已经存在，claim 不能重复创建；
- `starting` 是 schema 允许的非终态校准输入；正常 claim 在单事务内直接从 `pending`
  进入 `completed`，启动恢复遇到 `starting` 时将其校准为 `interrupted`；
- `stopping` 只是请求状态，只有 terminal transaction 才释放 active run；
- migration retry 从 `failed_retryable` 创建新 attempt，不能篡改上一次证据；
  `failed_terminal` 只允许人工修复来源或显式重置后重新进入；
- restore 通过增加 conversation generation 使旧 cursor/live/cache 失效，而不是回退
  `write_seq` 或复用 ID。

### 27.4 写入顺序与数据库约束的分工

数据库直接保证外键、唯一性、枚举和 active partial unique；application transaction 负责
DAG 可达性、terminal immutable 和 segment 连续性。每个复杂校验都必须
有一条“名称/字段完整但约束错误”的反例测试，例如：

- head 与 parent chain 的最新 turn/lineage depth 不一致；
- tool result 指向另一个 call；
- segment offset 有一字节空洞；
- queue item 被错误 claim 到另一个 Chat；
- schema 表名齐全，但缺少 active run partial unique index。

schema validator 必须验证列类型、PK、FK、unique/check/index 和 schema version，不能只验证
表名存在。

## 28. 单 Chat 时间线与 Chat Copy 最终合同

产品不再提供“同一 Chat 内分支”。此前用于表达分支树的 `branch_id`、
`parent_branch_id`、`fork_turn_id` 以及 branch 创建、切换、删除接口全部移除，
不能改名后继续保留等价概念。每个 Chat 是一个独立的事实源和上下文边界，只有一条
由不可变 `parent_turn_id` 串联的时间线。

### 28.1 状态归属

| 状态                   | 最终归属与约束                                                          |
| ---------------------- | ----------------------------------------------------------------------- |
| head turn              | `chat_meta.head_turn_id`；只能指向本 Chat 可达 turn                     |
| current context        | `chat_meta.current_context_revision_id`；`/clear` 只推进该指针          |
| run                    | `chat_runs`；每个 Chat 最多一个非终态 run                               |
| queue                  | `chat_queue_items`；`client_request_id`、`queue_ordinal` 在 Chat 内唯一 |
| alias/provider mapping | alias 直接解析到 Chat，不再解析到 branch                                |
| cursor/live            | 作用域为 conversation、固定 head/view、层级和 subject，不含 branch      |
| catalog/recall         | 以 conversation 为隔离和投影边界，不保存 branch 字段                    |

`chat_branches` 表整体删除；`chat_turns.origin_branch_id`、
`chat_runs.branch_id`、`chat_queue_items.branch_id`、`chat_aliases.branch_id`
以及 catalog、recall 中所有 branch 字段同步删除。不能用常量 branch 或隐式默认
branch 兼容新代码，因为这会继续维持错误合同。

### 28.2 Copy Chat

Copy 操作接收源 `chat_id` 与一个已完成、从源 head 可达的 `source_turn_id`。
服务端在一个新的 `conversation_id` 和独立正文库中复制该 turn 及其祖先的
turn/message/block/tool/context 数据，重写实体 ID，并将副本 head/context 指向
复制后的对应实体。工作区、项目目录、附件文件和其他外部可变资源不复制。

复制谱系属于 Chat 级 catalog 元数据：

- `source_conversation_id`：直接来源 Chat；
- `source_turn_id`：源 Chat 中的复制锚点；
- `root_conversation_id`：连续 Copy 链的根 Chat；
- `copied_at_us`：创建时间。

谱系只用于标题、来源展示和审计，不建立生命周期外键；删除源 Chat 不级联删除
副本。接口只使用 `/copies` 和 `copy_chat_at_turn` 术语，不保留实验期旧路由适配。

### 28.3 开发期 schema 收敛

当前仍处于开发阶段，不把曾含分支字段的实验数据库视为受支持的旧库，也不在正式代码中
保留其 reader、migrator 或专用隔离逻辑。无分支结构是唯一受支持的当前 schema；数据库
版本号只用于严格校验当前契约，不代表产品需要维护多版本升级链。开发机残留的实验数据库
直接清理并按当前 schema 重建，不进入产品运行路径。
旧 JSON 仍按既定 legacy migration 进入当前单时间线 schema。这样正式 schema、repository、
API、基准和测试只有一套 Chat Copy 合同，不会因常量默认分支或升级适配器继续固化错误模型。
