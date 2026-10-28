# 通用 Chat 重构详细设计评审

> 评审日期：2026-10-22
> 评审对象：`docs/architecture/chat-refactor-detailed-design.md`
> 评审方式：静态设计与契约对侧取证；未执行实现测试

## 结论

**REQUEST CHANGES**。总体架构选择合理，特别是正文事实源、可重建投影、不可变 turn
DAG、三级分页、有界实时状态和单 Chat 物理恢复的边界已经基本闭合。但开发前仍需修正
3 个 Important 问题：排队发送未满足 durable user turn 契约、alias 的全局唯一性无法由
per-Chat 规范副本保证、应用级 backup 缺少跨库一致性协议。

## Strengths

- 事实源所有权清晰地区分为正文、workspace 控制数据和可重建 recall 投影。
- history/live 合并、terminal commit、慢订阅者和大 payload 均同时设置了数量与字节边界。
- 迁移、删除和投影修复采用可恢复状态机，并明确禁止新旧双写。
- Schema validator 明确校验类型、主键、外键、唯一性和索引，而不只检查表名。
- 性能验收先验证正确性 oracle，再比较性能，避免通过少加载或漏内容获得虚假提升。

## Issues

### Important

#### I1：排队发送返回 accepted 时没有 durable user turn

**确定程度：** definitely a design bug
**位置：** `chat-refactor-detailed-design.md:416`
**违反契约：** 设计结论要求发送先持久化 user turn/run（第 43 行），不变量 7.6 要求
user turn durable commit 后才能返回 accepted（第 318 行），发布阻断条件也明确禁止
“user accepted 后仍可能没有 durable user turn”（第 1130 行）。但 branch 忙时仅插入
`chat_queue_items`，随后仍返回 accepted；turn/user message/block 只在非排队分支创建。

**触达路径：** 当前 Chat 前端已有真实的 busy-run queue 路径；设计也保留 queued send、
服务重启将 pending queue 标记为 interrupted（第 618 行）。因此用户在已有 run 执行时发送
第二条输入即可触达，不是理论输入。

**后果：** 服务在 queue item 被 claim 前退出时，accepted 输入没有进入 transcript；当前
V2 history API 只围绕 turn/message/block，下文也没有定义 interrupted queue 的历史读取
与重试协议。这样既可能让输入在刷新后不可见，也使幂等重试无法依赖已承诺的 user turn。

**测试缺口：** 缺少“active run → 第二条 send accepted → 进程退出 → 重启”的 oracle，
验证输入内容、状态、幂等键和用户可见恢复结果。

**最小修复：** 在开发前二选一并统一全文合同：

1. queue admission 原子创建可恢复的 accepted turn/user message，并定义 queued turn 在前序
   turn 未终态时的不可变 lineage 规则；或
2. 明确 queue item 是 accepted input 的规范实体，返回独立的 `queued` receipt，补充
   queue/interrupted 的读取、展示和显式重试 API，同时把“durable user turn”相关不变量
   与发布门禁改为与该模型一致的表述。

无论选择哪种，都应增加上述 kill/restart 回归测试，并覆盖 attachment-only queued input。

#### I2：alias 的全局唯一契约无法由 per-Chat 规范副本闭合

**确定程度：** definitely a design bug
**位置：** `chat-refactor-detailed-design.md:245`
**违反契约：** alias 唯一键包含 agent/namespace/external key/owner scope（第 236–240 行），
但规范副本被放在每个 Chat 的独立正文库，workspace 唯一索引只存在于被定义为“投影”的
`chat_alias_index`。单个正文库的 unique constraint 只能保证 Chat 内唯一，不能阻止两个
正文库同时声明同一 alias。

**触达路径：** QQ、DingTalk、Discord 等现有 channel 都会从外部 thread/conversation
解析 `session_id`；新设计要求这些入口通过 alias 路由。并发创建两个 Chat、迁移冲突，
或 Checkpoint restore 把旧 provider alias 恢复回来时，都可能让两个正文库声明同一键。

**后果：** catalog upsert 只能拒绝其中一个投影，却不能修复两个“规范事实”的冲突；路由
可能长期指向错误 Chat，Checkpoint restore 的第 7 步也没有定义 alias 已被另一 Chat 占用
时的裁决。该问题跨越会话归属边界，不能依赖 last-write-wins。

**测试缺口：** 缺少两个 Chat 并发 claim 同一 alias、迁移发现重复 alias、恢复旧快照时
alias 已重新绑定三种冲突测试。

**最小修复：** 将 alias ownership/claim 定义为 catalog 中的规范控制数据，由 catalog
事务先 reserve/compare-and-swap，再 best-effort 写正文 mirror；或者引入等价的 workspace
级唯一 allocator。Checkpoint restore 必须在替换正文库前预检 alias 冲突，并明确
abort/rebind 的产品语义，禁止静默覆盖。正文 artifact 仍可携带 alias 历史，但不能单独
获得 workspace 路由所有权。

#### I3：应用级 backup 没有定义跨 catalog 与正文库的一致快照

**确定程度：** likely an issue
**位置：** `chat-refactor-detailed-design.md:812`
**违反契约：** D10 和 Goal 要求应用级 backup/export/import 包含统一数据文件并验证引用
完整性；第 15.4 节只规定“分别执行 SQLite backup”，没有固定 workspace inventory、
lifecycle 或 generation 的协议。

**触达路径：** backup 期间正常的新建、删除、alias 变更或展示元数据更新会分别提交到
catalog 和 per-Chat DB；这些操作在设计中明确没有跨库事务。因而 catalog 可以在 t1 被
备份，某个正文库在 t2 被备份，中间状态对真实用户路径可达。

**后果：** 归档可能包含 catalog 已删除但正文仍存在的 Chat、catalog 指向却缺少正文的
Chat，或 alias/generation 不匹配。导入阶段才发现引用错误只能拒绝整个备份，不能把这个
已经交付的归档视为有效恢复点。

**测试缺口：** 缺少 backup 与 create/delete/alias mutation 并发的 fault-injection 测试，
以及归档完成前的全包引用校验。

**最小修复：** 定义 workspace backup epoch：先在 catalog 固定 inventory/generation，
短暂阻止生命周期与 alias ownership 变更，或在逐库快照后比较 generation 并仅重试发生
变化的项；归档发布前必须在 staging 对 manifest、catalog 和所有规范正文库做全量引用
校验。补充并发 create/delete/alias 测试，证明得到有效一致快照或明确失败且不发布归档。

## Design Questions

1. **Checkpoint barrier 的锁语义需要写实。** 第 15.1 节允许 active run 继续，仅写“固定
   generation 后 SQLite backup”。需明确 barrier 是否在整个 backup 期间阻止 materializer
   写入，或通过稳定 read snapshot 固定版本，并验证 artifact 内 generation 与 manifest
   完全一致；否则“固定 generation”只是标签，不是快照边界。
2. **跨进程承诺与当前部署模型不一致。** 第 9.5 节声明依赖 SQLite 支持跨进程运行，
   restore 又只写“关闭目标连接后原子替换”。当前 CLI/Tauri 明确固定 `workers=1`，所以
   首期可以收敛为单进程合同；若仍保留跨进程承诺，则 restore/delete 需要 workspace 级
   lease、跨进程连接失效与 Windows 文件句柄测试，不能只关闭本进程 LRU。

## Suggestions

- 将 I1、I2、I3 各增加一条明确的发布阻断条件，避免里程碑实现只验证 schema happy path。
- `chat_projection_outbox` 的唯一键建议写成明确列组合；当前“source generation 幂等唯一”
  容易被实现成每 generation 只能有一条记录，而一个提交通常同时产生 catalog 与 recall
  命令。

## Synchronous I/O & Event Loop

静态设计未发现要求在浏览器 render 路径执行同步文件/网络 I/O；前端也明确禁止对多 MiB
payload 在 render 中做 `JSON.parse/stringify`、深拷贝或全文 hash。SQLite backup、hash、
fsync 和大包校验必须在实现阶段保持在线程/worker I/O 边界内。未在本地执行性能验证。

## Cross-Platform Compatibility

| Platform | Status | Notes |
|---|---|---|
| macOS | ⚠️ | 静态方案可行；需补原子替换、fsync 和 open-connection 测试 |
| Linux | ⚠️ | 静态方案可行；需防止 rename 后旧 inode 连接继续写 |
| Windows | ⚠️ | open SQLite 句柄会阻止 replace/delete，必须验证 LRU 全部关闭与 sidecar 清理 |

以上均为静态推演，未实际执行跨平台验证。

## 裁决

**REQUEST CHANGES**：先闭合 I1–I3，再进入业务代码实施。其余参数可按现有 M0 benchmark
计划校准，不阻塞设计确认。
