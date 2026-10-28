# 通用 Chat 重构实施 Checklist

## 架构修正：移除 branch，改为独立 Chat Copy

- [x] 用户确认移除 `branch_id`、`parent_branch_id` 和同 Chat 分支能力
- [x] 明确单 Chat 时间线状态归属与 Chat 级 Copy 谱系合同
- [x] conversation/catalog/recall 当前唯一 schema 不包含任何 branch 字段
- [x] 不为开发期实验多分支库保留 reader、migrator 或隔离逻辑
- [x] repository/application/API 改为 conversation-scoped
- [x] Copy API 只接收 source turn，创建独立 Chat
- [x] 前端分页、live、normalized store 去除 branch scope
- [x] 删除 branch API、类型、测试与失效设计描述
- [x] Copy、分页、实时、删除、Checkpoint 定点回归通过
- [x] 清理开发机两个 workspace 的实验 branch DB、孤儿 DB 和迁移 sidecar
- [x] 删除一次性实验 schema 转换脚本；正式代码只接受当前唯一 schema

> 对应设计：`docs/architecture/chat-refactor-detailed-design.md`
> 完成度审计：`docs/architecture/chat-refactor-m3-m7-completion-audit.md`
> 当前状态：M0–M6 已实现；M7 自动化性能与稳定性门禁已通过。本机已显式开启
> storage/UI V2 进行真实 Console 验收；8 GiB 受控 Linux VM 的 Extreme 容量门禁
> 已通过；MessageList 独立本地挂载窗口、双向窗口移动、回到最新和离窗 user-turn
> 精确导航已实现；真实 Console 渠道矩阵已完成。Feishu 真机结果仅作补充证据，
> DingTalk/QQ 不属于本次渠道验收范围；用户已确认正式默认开启，仅剩版本交付

## 0. 设计评审

- [x] 用户确认详细设计和分阶段边界
- [x] 确认每 Chat SQLite + 独立 catalog + recall projection 的物理布局
- [x] 确认 catalog 逻辑能力必需，独立 `chat_catalog.db` 是首期物理实现而非永久 API 契约
- [x] 确认保留目录使用 `<workspace>/.qwenpaw/chat/`，不占用用户 `chat/` 目录
- [x] 确认 Runtime WebUI 采用“有界 session adapter 优先，未达标再替换 timeline”
- [x] 最终确认从完整 turn 复制独立 Chat，不提供 Chat 内分支切换
- [x] 将评审产生的行为变化写入 decisions 文档

## 1. M0 基线与测试资产

- [x] 生成 transcript correctness manifest（数量、顺序、hash、tool link、usage）
- [x] 冻结 Typical/Long/Extreme/Tool dense/Huge block/Copy/Long run/Concurrent 数据规格
- [x] 保存当前 Python/Node 基准机器和 cache 条件；浏览器/viewport/网络条件待浏览器基准补齐
- [x] 定点执行后端 Chat/Scroll/Checkpoint 当前测试
- [x] 定点执行前端 large session/SDK lifecycle 当前测试
- [x] 发送/queue/stop/cancel/tool/approval/attachment/compact/clear 自动化基线
- [x] 隔离 working directory 的真实 Console V2 消息加载 smoke
- [x] 发布前真实 provider-backed Console 人工矩阵
      （Console 真实发送/stream/停止/主动刷新断线及 durable 内容已执行；停止 turn
      显示“已取消”，断线 run 恢复后完整保存 `RECONNECT-1` 至 `RECONNECT-1000`。
      重连实测暴露的 active user 缺失归组问题已修复并补定点回归；当前有效模型为
      DashScope `qwen3.8-max-0902`；最终独立 Chat Copy 的真实 provider continuation、
      source/target 隔离、数字标题和 UI 跳转已通过；现有 Feishu Chat 的只读预检确认
      `ready_v2`、唯一单时间线、
      16 个 completed turns 的 turn/message/block 真实分页与正文库 `quick_check=ok`；
      随后临时启用 Feishu 完成真机出站、手机入站、模型回复和 terminal 持久化，
      历史增加为 17 个 completed turns 后恢复关闭。Feishu 仅作补充证据；用户于
      2026-10-23 明确渠道定点只要求 Console，DingTalk/QQ 不作为发布门禁）
- [x] 完成 QwenPaw/Harness/Feishu/DingTalk/QQ 入口能力矩阵和定点基线
- [x] 保存 main warm 后端与前端转换性能原始 JSON
- [x] 浏览器 cold/warm 原始数据（随 M3 页面分页/窗口化一起验收）
- [x] 验证 oracle 能发现一条消息缺失、重复、乱序和 tool 错配

## 2. M1 存储与迁移

### Schema 与 repository

- [x] `chat_catalog.db` schema、migration、validator
- [x] validator 检查类型、PK/FK、unique/check/partial unique/index，不只检查表名
- [x] catalog 展示字段与正文运行字段所有权 contract
- [x] group/name/pin/archive 单库事务，checkpoint restore 不回滚这些字段
- [x] per-conversation schema、foreign key/check/unique/partial unique constraints
- [x] `chat_recall.db` schema 与 FTS
- [x] SQLite WAL/busy timeout/transaction policy
- [x] per-conversation async lock/connection LRU primitive 与边界测试（热路径接入随 M3 分页）
- [x] catalog generation/outbox/repair
- [x] catalog runtime recovery intent，覆盖 shard commit 前后进程崩溃
- [x] recall outbox/projector/rebuild
- [x] per-conversation FTS 持续维护，projection 按 Chat lineage 过滤
- [x] global recall 与当前 turn provisional row 按 stable key 合并
- [x] recall sequence lease，保留 legacy seq 且绝不复用
- [x] corruption quarantine 与只读错误反馈

### Legacy migration

- [x] 轻量 inventory，不在 startup 解析全部 session JSON
- [x] 活跃度优先队列和冷 Chat access barrier
- [x] session JSON 稳定快照
- [x] history DB read snapshot
- [x] ChatSpec/session/path alias 映射与歧义拒绝
- [x] message/tool stable dedup
- [x] legacy 重复 `Msg.id` 以内容证据对齐并保持 session occurrence 顺序
- [x] 附件原始输入与 history 模型展开表示的可证明等价合并
- [x] conflicting stable key 明确失败
- [x] unknown legacy block 原样保存
- [x] 已被 legacy retention 删除的 sequence gap 检测与明确诊断
- [x] 临时库 build、quick_check、FK、hash/count/order/tool 校验
- [x] 幂等 retry 和中断续跑
- [x] migration 状态只写 catalog/control plane，不写 transcript
- [x] `ready_v2` 后 legacy 写门禁

### 定点验证

- [x] 空/小/长/归档/无 ChatSpec/orphan/损坏 JSON
- [x] history-only prefix、JSON-only tail、重复、冲突、orphan tool result
- [x] 真实 background migration 失败 snapshot 只读重放、物化和在线恢复
- [x] 同 Chat 并发打开/发送只启动一个 migration
- [x] migration kill 后恢复
- [x] startup 只做 metadata inventory，批量 catalog 写且不解析冷 Chat transcript

## 3. M2 统一写链路

- [x] Chat Application Service 统一所有入口
- [x] alias resolver 直接解析到 conversation
- [x] idempotent `accept_input()`
- [x] durable queue item、FIFO claim、waiter cancel 和 Chat 隔离
- [x] user turn commit 后才启动 runtime
- [x] partial materializer：250ms 或 +64 KiB 先到
- [x] tool result/error/cancel/terminal immediate flush
- [x] approval request/result 直接写当前 run 正文库，既有 service/polling 行为不变
- [x] terminal atomic commit 后才发 terminal event
- [x] runtime context revision/blob adapter
- [x] run fencing 和 active unique constraint
- [x] restart 把 run/queue/tool 标 interrupted，不执行恢复
- [x] Scroll 停止写旧 `conversation_history`
- [x] 新 Scroll search/expand/sessions contract 兼容
- [x] Console/Harness/Feishu/DingTalk/QQ 定点 contract

## 4. M3 分页与前端窗口

### Backend API

- [x] view/capabilities/migration/live summary
- [x] Chat turn keyset page，固定 head
- [x] turn message page
- [x] message block page
- [x] block byte fragment
- [x] 2 MiB 双门禁和第一个实体下钻
- [x] signed scoped cursor 与 permission recheck
- [x] checkpoint/restore 后 `view_invalidated`
- [x] tool result 未加载状态而非假缺失

### Frontend

- [x] route/session/history/live/Copy/composer 职责具备独立可测边界
- [x] normalized ID store 和 owner epoch
- [x] 初始只拉尾页，不调用全量 detail
- [x] 后端 cursor history sentinel
- [x] 有界 decoded cache（entity + bytes）
- [x] stable anchor 补页
- [x] live tail 和旧历史阅读互不抢滚动
- [x] lazy reasoning/tool/block fragment
- [x] finalized Markdown memo 与 active tail 合批
- [x] V2 窗口关闭 SDK 内存模拟分页（503 legacy 回退保持旧行为）
- [x] MessageList 仅挂载独立的有界本地消息窗口，窗口移动时保留 DOM 锚点，
      normalized cache 中已加载但离窗的消息不得继续形成 Bubble DOM
- [x] Runtime WebUI card/approval/attachment contract test
- [x] pending localStorage 只保留未取得 durable receipt 的提交

当前 Runtime WebUI 只暴露 message store，没有把 `Bubble.List` 的后端
`onLoadMore` 回调透出。M3 用隔离的 pagination bridge 接入真实 cursor、窗口替换和
锚点补偿；检测到 V2 active path 后关闭 SDK 的 10 条内存 reveal。复核 SDK 源码后确认，
`pagination=false` 会把传入的全部 messages 直接映射为 Bubble；当前
`selectLoadedBackendMessages()` 会返回 normalized active cache 的全部 retained pages，
因此已新增独立 complete-turn 窗口，只把窗口消息交给 SDK；未依赖 CSS
`content-visibility`，也未重新启用只增不减的 SDK 10 条 reveal。只有
`chat_v2_unavailable` / `chat_v2_ui_disabled` 两个明确的 503 capability code 才允许兼容
读取；普通过载 503 不会触发全量 transcript fallback。

窗口设计已按冻结数据和真实库重新校准：真实 33 个 turn 的持久化 message/turn 为
`p50=3、p95=17、max=22`，tool-dense 冻结数据达到 41 message/turn，但现有完整 turn
转换后均为 2 个 SDK Bubble。因此不能按 backend message 数截断；候选窗口按完整 turn
移动，预算为 24 turns / 48 Bubble roots / 2 MiB converted payload 先到者为准。后端分页和
窗口移动量按用户确认改为 5 turns（通常 10 Bubble）；首屏不足以覆盖实际 MessageList
视口及 overscan 时串行自动补拉更旧的 5-turn 页，直到填满、历史耗尽或命中窗口上限，
并以单 cursor in-flight 和“无新增 turn 即停止”防止重复请求。单个超限 turn 允许独占窗口
且正文继续走 lazy fragment。
同时补齐 newer cursor、回到最新和离窗 user-turn navigator，防止深度向上浏览后无法返回
已驱逐尾页，或因 SDK `UserMessageAnchors` 只看本地 message 而造成功能回退。窗口常量、
5-turn 首屏/补页、基于实际视口的串行自动补页、newer cursor、回到最新和离窗 user-turn
navigator 均已实现。目标导航通过后端 `target_turn_id` 定位页，并使用 target-centric
window 强制挂载目标完整 turn；不会在合并目标页后误选缓存尾页。深度向上导致尾页被
normalized cache 驱逐
时，“回到最新”会以当前 view 重新拉取尾页并原子重置本地窗口，不要求刷新整个 Chat。

2026-10-23 复核《Codex桌面版长历史消息渲染与导航机制分析报告》后，采用其可验证的
通用原则而不复制具体 Island 实现：保持 durable normalized data、SDK 挂载窗口和 DOM
三层分离；以完整 turn 为窗口/驱逐单位；关闭浏览器原生 `overflow-anchor`，由稳定 Bubble
锚点补偿窗口替换；导航目标若未挂载，必须先在数据层补齐再滚动。当前不增加第二套
variable-size virtual list，因为现有 adapter 已将 SDK 输入限制为 24 turns / 48 Bubble
roots / 2 MiB，超大正文另由 block preview/fragment 限制。只有冻结 Long/Tool dense 数据
仍无法满足 DOM/long-task 门槛时，才升级为自有 virtual list。

真实 Console V2 页面复核还发现并修复了 history owner 的可观察时序：`path.view` 先于
session owner map 就绪时，pagination bridge 不能提前退出并永远丢失 scroller 绑定；现在
bridge 先观察 Chat 根节点，owner 可用后才绑定 V2 scroller。对应回归覆盖“owner 延迟就绪”
和 SDK 替换 scroller 场景；真实页面确认 `overflow-anchor: none`，4-turn/大响应样本只挂载
9 个 Bubble roots；17-turn Feishu 历史从 20 roots 导航补齐到 34 roots，回到最新后不重复
增长，仍低于 48-root 上限。

超大 block 默认只渲染有界 preview，用户显式展开后逐片校验 scope、view、entity
version、连续 offset 和 SHA-256；前端 fragment 窗口上限 1 MiB，切 Chat/卸载会取消
请求。稳定历史内容保持对象引用并由 memoized host message 跳过重复 Markdown 解析，
active tail 继续通过 deferred source 合批。反向列表在用户阅读旧历史时保存可见 DOM
锚点，可抵消 live tail 更新触发的 SDK 自动回底；位于尾部时仍正常跟随。队列持久层
写入和旧数据读取都过滤 `sent`。V2 接收输入时在同一 SQLite 事务内写入 user message；
其唯一 client ID 一旦由分页结果返回，即清除 `setLastUserMessage` 本地项。本地项仅覆盖
客户端尚未观察到 durable receipt 的断线窗口，从不作为 transcript 事实源。不能以 HTTP
响应头代替 durable receipt：StreamingResponse 的响应头可能早于 producer 的事务提交，
提前清除会使旧链路和进程崩溃窗口重新出现末条用户消息不可见。

本轮不把六项职责机械拆成六个类：`sessionApi` 保留 SDK route/session 适配，normalized
history store、pagination bridge、live controller、Copy control、composer/queue lifecycle
分别已有独立模块和定点测试。继续拆分只改变文件形态，不改善本次功能或性能门禁，并会
扩大现有行为回归面。

### 定点验证

- [x] 同 cursor 并发请求合并
- [x] cursor scope/tamper/permission/head invalidation
- [x] 64 MiB block 完整重建和取消
- [x] 向上补页 anchor ≤2px
- [x] cached newer cursor、尾页重新拉取和原子“回到最新”
- [x] 离窗 user-turn 精确导航先补齐 target-centric 数据窗口、再滚到稳定 user Bubble
- [x] 快速切 Chat 无串线
- [x] Long/Tool dense 页面 DOM、heap、long task 有界
- [x] 1,000+ loaded messages 下 SDK message store、Bubble roots 与 UserMessageAnchors
      均受本地窗口上限约束；30 × 5 个 tool-dense turns 累计装载 6,150 条 backend
      messages，active cache 保持 14 页，SDK messages/roots 输入最多 48，anchors 最多 24

## 5. M4 实时与重连

- [x] run epoch/event cursor/entity version 协议
- [x] `subscribe()` 原子注册 + barrier + immutable live snapshot
- [x] sync 后只接 barrier 之后事件
- [x] replay/event buffer 同时按 count/bytes 限制
- [x] subscriber queue 同时按 count/bytes 限制
- [x] slow subscriber 断开并 resync，不反压 runtime
- [x] offset/version 幂等 merge
- [x] history/sync/live 只向前覆盖
- [x] terminal 后 durable tail calibration
- [x] live snapshot 携带 accepted user envelope，重连 turn 不继承前序 canceled 状态
- [x] active turn 已跨 partial flush 时，sync snapshot 仍可恢复有界文本窗口并继续 SSE
- [x] SDK running response 使用稳定 turn identity，切换/刷新不创建空白 response card
- [x] unload/switch/stop/terminal 资源清理
- [x] service restart epoch lost -> history resync/interrupted

### 故障注入

- [x] accepted commit 前断开
- [x] accepted commit 后、首 event 前断开
- [x] partial threshold 前后 kill
- [x] terminal commit 前后 kill
- [x] sync 构造期间继续 streaming
- [x] event duplicate/reorder/gap
- [x] subscriber overflow
- [x] terminal 后迟到 running/delta
- [x] 多 tab 同 Chat 和快速 owner epoch 切换

兼容 SSE `TaskTracker` 与 normalized `ChatLiveHub` 均采用 replay 2048 events/8 MiB、
subscriber 512 events/2 MiB 双上限。Hub 由 agent 级 TaskTracker 持有，Workspace 热重载
复用、服务重启自然丢失 epoch；materializer 在持久化阈值内暴露 immutable active overlay，
V2 live endpoint 原子 subscribe 后先发 sync barrier，再发新事件。前端认证 fetch controller
按 50ms/32 KiB 合批，使用 byte offset/entity version 幂等合并；gap、overflow、epoch lost 和
terminal 均读取 durable tail 校准，切 Chat/Agent 会主动取消 reader。完成消息仅在持久化后
进入有界 active-message overlay（单条 1 MiB、总计 2 MiB/256 条）；超限消息标记 deferred，
终态后从 durable history 加载。`test_live_fault_injection.py` 使用独立 spawn 子进程在 accepted、
partial threshold、terminal commit 前后执行真实 `os._exit`，再由 replacement service 完成
restart reconciliation，验证不恢复执行且只暴露已提交数据。

## 6. M5 独立 Chat Copy 与 `/clear`

- [x] backend capability only，禁止 channel 名称散落判断
- [x] 只允许从 source current head 可达的 completed turn 创建 Copy
- [x] Copy 创建新的 catalog entry、conversation DB 和 ChatSpec
- [x] 复制目标 turn 及之前的 transcript/context，source Chat 保持不变
- [x] create 后刷新侧边栏并导航到新 Chat
- [x] active run/pending queue 返回 409，不自动取消
- [x] source/target queue、run、usage、live、cache 与删除生命周期隔离
- [x] 目标 Chat 独立 Scroll visibility、recall sequence 和 projection barrier
- [x] `/clear` 创建 clear context revision，不清 transcript/UI
- [x] clear 前 turn Copy 不继承后来 clear；clear 后 turn Copy 继承 clear
- [x] 外部入口不暴露 Copy；Console 只依据 capability 渲染
- [x] Copy icon 位于 completed response card；其他 card 不显示
- [x] Copy 不复制 workspace、terminal、checkpoint 或物理附件文件
- [x] 默认标题按原始家族标题递增为 `(2)`、`(3)`，显式名称仍优先

API 使用 `POST /api/chats/{chat_id}/copies`。目标内部 turn/message/block/context ID 与
recall sequence 独立，run/queue/provider runtime 状态不复制；source head/context 不变，
目标 projection 在 API 返回前完成。Copy 创建失败与创建后的 UI 收尾失败使用不同错误边界，
避免 durable 创建成功后误报失败。

### M5 定点验证（2026-10-23）

- Backend storage/application/API Copy、数字标题和 source/target 隔离回归通过。
- 真实 Console completed response Copy 返回 HTTP 200；目标使用新的 conversation、实体 ID
  和 recall sequence，provider continuation 成功且 source/target 后续写入隔离。
- Console DOM 确认入口只位于 completed response card；创建成功后新 Chat 出现在侧栏。
- Frontend Chat API/Page/5-turn pagination/local-window/session 定点回归、TypeScript、ESLint、
  定点 Prettier、Python pre-commit 与 `git diff --check` 均通过。

## 7. M6 Checkpoint、删除、备份

### Checkpoint

- [x] active partial 强制 flush 后 SQLite online backup
- [x] artifact manifest/schema/hash/identity 校验
- [x] preview 明示恢复整个目标 Chat 的 transcript、context 和 runtime state
- [x] safety checkpoint
- [x] close connection + atomic replace + sidecar cleanup + fsync
- [x] restore 后严格校验当前 schema，不迁移实验多分支库
- [x] 只修复目标 Chat catalog/alias/recall
- [x] restore 使旧 cursor/live/cache 明确失效
- [x] restore artifact 中非终态 run/queue/tool 转 interrupted，不恢复执行
- [x] legacy session JSON checkpoint 在 staging 升级后原子恢复
- [x] memory/files 选项保持现有语义
- [x] 单 Chat restore 不改变其他 Chat hash/head

验证（M6 checkpoint）：

- normalized/legacy artifact、完整 Chat 恢复、损坏拒绝、失败回滚与 partial
  flush，加既有 conversation/memory/files 回归：105 passed，1 skipped
- Console `tsc --noEmit` 与 Checkpoint 页面定点 ESLint：passed
- 相关 Python/JSON 文件 pre-commit
  （AST/JSON/mypy/black/flake8/pylint）：passed

### Application backup/import

- [x] catalog 和每个正文库 online backup
- [x] 不直接归档 live WAL/SHM
- [x] storage manifest 与全量 hash/reference validation
- [x] recall 作为 derived，导入后重建
- [x] staging 验证后安全 swap
- [x] 兼容期保留 legacy 文件

验证（M6 application backup/import）：

- normalized Chat snapshot/restore 与 legacy backup 兼容：11 passed
- 相关 Python 文件 pre-commit（AST/mypy/black/flake8/pylint）：passed

### Delete saga

- [x] catalog deleting tombstone 先阻止新写
- [x] stop run/queue/subscriber
- [x] 删除 checkpoint refs/runtime/provider/usage ownership
- [x] 删除 recall rows 和正文 DB/sidecar
- [x] 删除对应 legacy ChatSpec/session，防止复活
- [x] 附件/workspace/artifact 物理文件保持不变
- [x] 每一步幂等，crash 后续跑

验证（M6 delete saga）：

- Chat/PawApp 定点回归与 crash-resume：552 passed
- 相关 Python 文件 pre-commit（AST/mypy/black/flake8/pylint）：passed

## 8. 性能与稳定性门禁

- [x] 所有性能样本先通过 correctness manifest
- [x] 正式 Long 浏览器 cold/warm 各 30 样本
- [x] 2 MiB page backend P95 ≤300ms
- [x] Long 冷打开可读尾部 P95 ≤1.5s
- [x] Long warm 切换 P95 ≤500ms
- [x] 相对 main 核心 P50 改善 ≥20%
- [x] 原 baseline <200ms 的场景回退 ≤10%
- [x] 无 >1s main-thread long task
- [x] React commit P95 目标 ≤50ms
- [x] timeline DOM 默认 ≤300 roots
- [x] 30min 稳态 retained heap 增长 ≤20%
- [x] 2h long run buffer/queue/connection/DOM/heap 不无界增长
- [x] slow subscriber 不影响 model/tool 主任务
- [x] Extreme/Concurrent 压测完成并保留原始 JSON
- [x] 8 GiB 受控 Linux VM Extreme 容量、swap 与 OOM 门禁

M7 当前证据（2026-10-22）：

- main Typical 完整读取 P50 92.58 ms（<200 ms），V2 同边界 P50 11.32 ms，改善
  87.8%，不存在 >10% 回退；
- Typical/Long/Tool dense/Huge block 的 warm repository page 各 30 样本，P95
  分别为 83.36/65.17/50.89/15.30 ms；Huge fragment P95 5.65 ms；
- 冻结的两小时事件量 144,000 events 已做加速测试，replay 稳定在 2,048 events /
  638,976 bytes，Python traced retained delta 966,992 bytes；
- 两小时 wall-clock live/reconnect soak 正式完成：144,000 events、119 次重连，
  replay 2,048 events / 647,168 bytes，Python traced retained delta 971,752 bytes，
  correctness 通过；
- 8 Chat × 2 subscribers 并发检查通过，slow subscriber 全部隔离，fast subscriber
  顺序完整；
- Extreme 1 GiB / 50,000 messages 完整 manifest 通过，turn tail 3 样本 P50
  51.25 ms、P95/max 56.94 ms；构造过程最大 RSS 2.47 GB、peak footprint
  3.82 GB、无 swap；在冻结的 16 GiB 验收机上分别占 15.4%/23.9%，分页读取达标；
- 同一 Extreme manifest 又在 Colima macOS Virtualization.Framework 的 8 GiB Linux VM
  （实际 `MemTotal` 8,307,167,232 bytes、4 vCPU、无 swap）完整执行。50,000 messages /
  10,000 turns / 50,000 blocks 全部相等，5 次 turn tail P50 98.21 ms、P95/max
  119.66 ms，response 2,074,100 bytes，仍低于 2 MiB 与 300 ms 门禁；进程峰值 RSS
  3,606,712,320 bytes，VM 最低可用内存 3,882,176,512 bytes，swap/oom kill 均为 0，
  总耗时 535.57 s；这是真实受控 VM 内存上限，不使用 macOS `RLIMIT_AS` 代替；
- 5-turn 最终实现的真实 Chrome/Console UI 各 30 样本：cold 可读尾部 P95 359.50 ms，
  warm 切换 P95 412.80 ms，React commit P95 分别为 12.10/17.40 ms，最大 long
  task 162 ms；correctness 和浏览器 fault 检查通过；
- 浏览器首屏和后续 cursor 请求均为 5 个完整 turn；短 turn 无法铺满视口时会自动
  串行补拉，MessageList 本地窗口仍受 24 turns / 48 roots / 2 MiB 预算约束；
- Long 浏览器 30 分钟 retained heap 从 71.23 MiB 增至 74.10 MiB（+4.03%），
  DOM nodes 3,430 → 3,417，listeners 451 → 463；
- Tool dense 浏览器 30 分钟 retained heap 从 74.60 MiB 增至 77.48 MiB
  （+3.86%），DOM nodes 3,744 → 3,735，listeners 457 → 469；
- 旧脚本全局选择器会计入嵌套节点，已改为
  `.qwenpaw-bubble-list > .qwenpaw-bubble`。最终 5-turn cold/warm 正式样本最大均为
  10 个顶层 roots，低于 48 上限；原始数据保存在
  `chat-refactor-m7-working-tree-browser-v2-five-turn.json` 及对应 traces；
- 修正前 Locator waiter 会让每次切换假保留 147 nodes / 约 14 listeners；污染结果已
  单独保存，正式结果使用布尔 waiter 并显式释放 JSHandle；
- Long/Tool dense 30 分钟 retained heap 均已通过。

最终扩展回归（2026-10-22）：

- Console Chat、SDK lifecycle、normalized history、分页、live、Copy、lazy block、
  queue、session conversion 共 11 个定点测试文件：400 passed；jsdom 仅输出已知
  pseudo-element
  `getComputedStyle` 提示，不是测试失败；
- Console/Feishu/DingTalk/QQ contracts 与 integration、V2 API/application/delete/live/
  fault/scroll/workspace、backup/checkpoint/session hook/Scroll command 共 19 个后端
  测试文件：195 passed，1 skipped；唯一 warning 为第三方 Feishu protobuf
  `utcfromtimestamp` deprecation；
- 当前运行服务的只读 `GET /api/chats/rollout-status` 返回
  `enabled=false, ui_enabled=false, healthy=true`，未触发迁移或写入；
- 真实 `localhost:5173` Console 在上述关闭态可列出 5 个既有 Chat，并只读打开一个
  legacy Chat，标题、用户消息和 assistant 回复可见；浏览器日志没有请求/渲染异常，
  仅有既有 React/Ant Design deprecation warnings。该 smoke 不替代 V2/外部渠道人工矩阵。
- 临时 working directory + 8011/5177 的真实 V2 Console smoke 验证了 ready_v2、消息
  渲染和 healthy rollout；最终独立 Chat Copy 语义已经完成真实 UI 验收。
- 最终 Copy 路径保留 response card 的 normalized `chat_turn_id`；Console 与公开 API
  只暴露独立 Chat Copy。

开发期单时间线收口与离窗导航回归（2026-10-23）：

- catalog/conversation/recall 使用无 branch 字段的当前唯一 schema；不包含实验
  多分支数据库的 reader、migrator 或专用隔离路径；存储定点测试 32 passed；
- 搜索 recall hit 携带 `turn_id/message_id`，目标页使用以目标 turn 为中心的 5-turn
  MessageList 挂载窗口；相关前端 8 个测试文件 239 passed；
- TypeScript、定点 ESLint/Prettier、Python pre-commit 与 `git diff --check` 均通过。

Console 用户消息目录导航回归（2026-10-23）：

- SDK 的 `content-visibility:auto` 固定占位会在平滑导航穿过大型 assistant/tool
  Bubble 时反复改变滚动几何。Console 现于目录/前后消息导航后校正目标锚点，并仅让
  当前有上限的本地 MessageList 窗口保持真实布局；首次加载仍采用懒渲染，DOM 挂载
  上限不变；
- 原复现“继续创建 8月27日 10:04”连续观测约 3.5 秒，目标顶部 `79.33px`、可视
  顶部 `79px`，`scrollHeight=7196px` 全程稳定；相邻上一条/下一条导航均稳定，实测
  当前仅挂载 30 个顶层 Bubble；
- 现有分页与长对话性能定点测试 2 个文件 9 passed；相关 TypeScript ESLint、
  Prettier 与仓库 `git diff --check` 通过。未修改测试文件。

真实 Console 与 migration 增量验收（2026-10-23）：

- 本机显式开启 storage/UI V2 后，DashScope `qwen3.8-max-0902` 的真实 Console
  发送和 stream 完成；刷新后 durable assistant 内容保持一致。修复了 V2 history
  scalar block 被当作 JSON 字符串显示，以及 Scroll headline 刷新后重新出现的问题；
- 真实 Stop API 将运行中 turn 持久化为 `canceled`，保留 partial assistant block，
  `active_run` 清空；刷新后内容仍在。修复 normalized history 丢失所属 turn 终态后，
  canceled turn 显示“已取消 · 1 个步骤”，相邻 completed turn 仍显示“已完成”；
- CodingAgent background migration 暴露两类 legacy 合并缺陷：重复 `Msg.id`，以及
  附件原始输入与 history 展开文本双表示。修复后两个真实 immutable snapshot 分别
  生成 1,133 messages/13 turns 和 207 messages/4 turns，materialized manifest 相等；
- 热重载后两个 `failed_terminal` job 自动恢复为 `ready_v2`。CodingAgent rollout 从
  29 ready + 2 failed 变为 31 ready + 0 failed，recall projection 31 ready，
  `healthy=true`；全部 13 个 turn 通过 4 个受固定 view 保护的 cursor 页面可达；
- migration normalization/orchestrator/snapshot/inventory/rollout 定点回归 35 passed；
  两个改动 Python 文件的 AST、mypy、Black、flake8、Pylint 与 `git diff --check`
  全部通过。旧 session JSON 和 `history.db` 均仍保留。
- canceled/interrupted/failed history 状态映射及既有 message display 定点回归
  92 passed；相关 TypeScript ESLint、Prettier 和仓库 `git diff --check` 通过。
- migration/history 修复后的 M4 回归：后端 live、fault injection、materializer、
  cancel-save 28 passed；前端 live controller、SDK reconnect/lifecycle 137 passed。
- 真实 active run 中刷新页面后，live stream 从同一 run 恢复并完整持久化
  `RECONNECT-1` 至 `RECONNECT-1000`；实测发现并修复 active live snapshot 缺少
  accepted user envelope 导致继承上一 canceled turn 状态的问题。新增后端 12 项、
  前端 17 项定点回归均通过。
- 修复后的真实热重载复验通过：active sync 同时包含 user/assistant，终态无需二次刷新
  即显示“已完成”；`HOTFIX-1` 至 `HOTFIX-200` 共 200 行连续、唯一、无缺失，run/turn
  均 completed、`active_run=null`、SQLite `quick_check=ok`。
- 运行中切换与刷新复验发现 partial block 已落盘前缀不属于 completed-head 历史页，旧
  sync 仅携带未落盘尾部，前端因此拒绝 overlay 并只显示停止按钮。修复后 live snapshot
  保留每个活动 block 最多 2 MiB 的有界 UTF-8 文本窗口；运行中切出再切回时唯一标记从
  64 持续增长至 164，整页刷新时从 6 增长至 124，两个 run 均在无需二次刷新时清除停止
  状态并完整落盘，页面告警和 reconnect 错误均为 0。后端相关回归 27 passed，前端
  history/live/session/SDK 回归 196 passed；未修改测试文件。
- 最终独立 Chat Copy 已验证 target 上下文可继续，source/target 后续写入隔离。
- 真实 UI 还捕获并修复 Copy 成功后的假失败 toast：作用域外路由函数导致
  `ReferenceError`，宽泛 catch 错误地显示失败。改用 `buildChatPath` 并拆分错误边界后，
  页面只显示 `Chat copied`、正确跳转，标题按原始家族名生成 `(2)`；临时验收副本已清理。
- 后续 rollout 复核发现一个已发布 Chat 在 `/compact_str` 终态 usage 阶段仍回写 legacy
  session JSON，inventory 看到 source fingerprint 漂移后把 migration 从 `ready_v2` 降为
  `legacy_only`。修复后，V2 request 不再执行 legacy usage persistence；inventory 将
  `lifecycle=ready` 视为单向 cutover，保留 migration proof、恢复/保持
  `ready_v2`，且不会为原生 V2 Chat 生成伪 migration 行。原复现及相邻
  inventory/orchestrator/application/Console/legacy usage 回归 `93 passed`，四个改动文件
  pre-commit 全部通过；真实 catalog 已从 `4 ready_v2 + 1 legacy_only` 恢复为
  `5 ready_v2`，fingerprint 未被覆盖、正文库 `quick_check=ok`，rollout 仍无 warning 或
  stop reason。
- 临时开启 Feishu 真机验收时先保留原始 `enabled=false, domain=lark` 基线。现有应用凭据
  由中国区官方 token API 验证有效，`lark` 域名则在 Python/Lark SDK 中因证书链失败；
  验收期间安全切换为 `domain=feishu`，未关闭 TLS 校验。目标实例真实发送取得 Feishu
  message ID，手机回复唯一 token 后同一 conversation 出现 active run，并在终态由
  16 增至 17 个 completed turns；新增 turn 包含 1 条 user 和 2 条非空 assistant message，
  位于同一 Chat timeline，`migration_state=ready_v2`、`active_run=null`。
  用户随后明确确认手机端已收到该验收消息，主动下行的用户可见结果完成闭环。
  catalog、recall、conversation 三库 `quick_check=ok`。验收后已恢复
  `enabled=false, domain=lark`，运行态卸载，除这两个字段外配置哈希保持一致。

Console 最终功能回归（2026-10-23）：

- 后端 Chat V2、Console、分页、live、copy/delete、backup/checkpoint、Scroll 相邻
  链路定点回归 `674 passed`；前端 history/session/live/分页/本地窗口/导航/搜索/
  队列/SDK/Markdown 渲染共 22 个测试文件、`535 passed`。未执行全量 npm
  build/test/format；本轮未修改测试文件。
- 真实浏览器验证会话切换、侧栏搜索、用户消息目录定点跳转、短消息发送、完成后刷新
  持久化均正常，页面业务告警为 0。长历史首屏从 reload 到最新消息可见为
  `1986 ms`（本机开发服务），首次只挂载 5 个 turn；连续向旧消息导航触发分页后
  挂载 15 个 turn，未超过 24-turn 本地窗口上限。
- 真实 active run 在生成中刷新后仍显示停止态；刷新后的 partial response 从
  711 字符继续实时增长，最终 120 行和 `DONE` 全量落盘，turn 状态为 completed、
  `active_run=null`。浏览器日志中 `History owner changed`、`get session failed`、
  `Failed to calibrate`、`会话加载失败`、`Could not copy chat` 均为 0。
- 从最新 response card 创建独立 Chat Copy，标题按家族名生成 `你好a (2)`；副本可继续
  生成，返回源 Chat 后副本新增内容不可见。测试副本随后通过删除 API 清理（删除前
  200、删除后 404），源 Chat 仍为 200/ready_v2，历史内容完整。
- API 实测 turn 页严格返回 5 项，message/block 三级分页端点均返回 200；最新 head
  turn 为 completed，最终 assistant block 含连续 120 行并以 `DONE` 结束。日志仅有
  既有可选 `himalaya` CLI 缺失告警，以及 browser 子系统关闭 Chrome session 时的
  `BridgeNotReadyError`；二者与 Chat 请求、持久化和重连路径无关。

原始数据：

- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-pages.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-live.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-live-2h.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-extreme.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-extreme-8g-vm.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-browser-v2-final.json`
- `docs/architecture/benchmarks/chat-refactor-m7-browser-v2-final-traces/`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-browser-heap-long-30m.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-browser-heap-tool-dense-30m.json`

## 9. 每个里程碑的交付动作

- [x] 只改与里程碑直接相关代码
- [x] 后端新增/修改代码有单元测试
- [x] 运行定点测试，不擅自执行全量 npm build/test/format
- [x] 对改动文件执行定点 lint/pre-commit（适用时）
- [x] `git diff --check`
- [x] 按 AGENTS.md 完成主要功能 Code Review
- [ ] 一个独立功能一个 commit，message 符合规范
- [ ] commit/push/PR 前再次取得用户授权
- [x] 更新设计、决策、migration runbook 和 benchmark 原始结果索引
- [x] 汇报实际执行证据，静态推演不得写成验证通过

## 10. 默认开启前最终门禁

- [x] 当前适用功能/用户行为无未批准回退
- [x] 新功能矩阵全部通过（渠道定点范围为 Console）
- [x] 所有入口已统一，无 legacy/new 双写
- [x] checkpoint/backup/delete/migration 闭环通过
- [x] long history/huge block/tool dense/long run 达标
- [x] 没有跨 Chat 污染
- [x] 没有 transcript 自动过期路径
- [x] 没有 accepted user message 或 terminal data 丢失窗口
- [x] rollout dashboard、告警、回滚说明已就绪
- [x] 用户确认进入默认开启阶段（2026-10-23）
