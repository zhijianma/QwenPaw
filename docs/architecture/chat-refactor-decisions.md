# 通用 Chat 重构决策记录

> 状态：已按独立 Chat Copy 最终语义校准；实现与定点回归完成
> 对应设计：`docs/architecture/chat-refactor-detailed-design.md`

## D-001：正文存储统一为新的 `chat_*` 模型

- Console 发起的 Chat，以及手机飞书、钉钉、QQ 等外部入口，最终使用同一套 Chat
  acceptance、物化和恢复链路。
- Console/QwenPaw 后端支持从完整 turn 复制独立 Chat；外部入口和 Harness 首期不展示
  Copy，但继续复用同一正文模型。
- 不复用带 30 天清理语义的 `conversation_history`；Scroll manager 的历史读写最终切到
  新存储，`history.db` 只作为 legacy migration 输入。

## D-002：物理布局采用 catalog + 每 Chat 正文库 + recall 投影

- workspace 目录为 `<workspace>/.qwenpaw/chat/`。
- `chat_catalog.db` 承担 Chat 列表、展示字段、迁移/删除/recovery intent 等 workspace
  控制数据；它在能力上相当于将 `chats.json` 升级为可事务、可校验的目录，但不是简单把
  JSON 原样搬进 SQLite。
- `conversations/<conversation_id>.db` 是单 Chat 全量 transcript、turn lineage、run、context、
  provider alias 的权威源。
- `chat_recall.db` 只是可重建检索投影，不保存唯一正文。
- 具体表名和拆库方式是当前首期实现，不作为永久外部 API；外部依赖 application service
  契约。

## D-003：历史永久性与 legacy 回退边界

- 新正文不因 30 天 retention、compact、clear 或 Scroll eviction 自动删除；保留至用户主动
  删除 Chat。
- 切换成功后不双写旧 JSON 或 `history.db`；旧文件保留原状，以便旧版本回退时仍能看到
  升级前历史。
- 旧版本回退只能看到升级前历史，这是已接受限制。
- 删除 Chat 删除全部 turn/runtime/checkpoint/provider mapping 和正文引用；不删除用户
  修改的 workspace 文件、附件或 artifact 物理文件。

## D-004：legacy migration 按活跃度分层并带访问 barrier

- startup 只做轻量 inventory，不解析所有旧 session JSON。
- 活跃 Chat 优先后台迁移；归档/冷 Chat 延迟迁移。
- 用户实际访问或发送到冷 Chat 时进入 migration barrier，显示状态并完成迁移，不向
  transcript 插入“迁移消息”。
- 同一 Chat 一旦 `ready_v2`，legacy 和 V2 不允许同时可写。

## D-005：Chat Copy、编辑与 `/clear`

- Copy 基于完整 terminal turn 创建独立 Chat；创建后导航到副本，源 Chat 不变。
- 首期 Copy 只在 QwenPaw Console 开放；运行中或有 pending queue 时创建返回 409。
- 本阶段不实现用户消息编辑，也不为其增加专用表/API。
- `/clear` 只重置当前 Chat 后续模型上下文，不删除或隐藏 transcript。

## D-006：流式持久化与服务重启

- user turn durable commit 成功后，才返回 accepted 并启动模型/工具执行。
- partial block 在距上次 flush 达 250ms 或新增 64 KiB 时持久化，以先达到者为准；结构
  事件与 terminal 立即 flush。
- terminal event 只能在包含剩余 segment、usage、tool/context/run 状态的事务提交后发送。
- 服务重启只恢复最后 durable 历史并把非终态 run/queue/tool 标记为 interrupted；不恢复
  模型调用或工具执行。

## D-007：Checkpoint 保持物理恢复语义

- Checkpoint 是已有独立功能，不改产品语义。
- 新存储接入后，保存/恢复必须覆盖目标 Chat 正文库及其全部 turn、context、provider
  mapping；不得遗漏重构新增的规范文件。
- catalog 的 name/pin/archive/group 等 workspace 展示字段不随单 Chat checkpoint 回滚；
  restore 后从正文库修复运行态/alias/recall 投影。

## D-008：阶段范围与验收标准

- 第一阶段只实施 M0–M2：基线、存储/迁移、统一 acceptance/materialization/recovery。
- M3–M7 的分页前端、实时 UI、Chat Copy、Checkpoint 完整接入、删除 saga 和灰度默认
  开启按里程碑验收。
- 当前版本已有功能、用户行为和稳定性不得出现未批准回退；新能力用 correctness manifest、
  定点回归、故障注入及可重复性能原始数据验收。
- 性能目标同时看绝对门槛和相对 main 改善；样本必须先通过 transcript correctness 校验。
