# Goal V2：通用 Chat 全量历史、复制、分页与实时链路重构

> 日期：2026-10-22
> 状态：范围基线已确认，详细设计与实施前置文档
> 上游：`chat_refactor_goal.md`、`research_watch_history_live_reconnect.md`
> 决策：`docs/architecture/chat-refactor-decisions.md`

本文约束功能结果、用户行为和系统不变量，不指定字段名、组合主键或内部协议结构。
snapshot、watermark、event sequence、context boundary 等术语只用于说明问题；实现可以
采用任何满足隔离、一致性、恢复和性能验收的等价设计。

## 1. 目标

在保持现有功能、用户行为和稳定性不回退的前提下，重构 Console Chat 及其后端
链路，实现：

- 用户删除 Chat 前，全量 transcript 不自动过期；
- 基于完整 turn 的独立 Chat Copy；
- turn/message/block 三级分页及超大 block fragment；
- 历史与实时职责分离、稳定流式合并和断线重连；
- 服务重启后恢复 durable 历史，不恢复中断的模型调用或工具执行；
- 长对话、超大消息和工具密集场景下有界的加载、内存和渲染成本；
- 用统一 `chat_*` schema 替代 session JSON 与 Scroll history 的双事实源。

本次不实现最后一条用户消息改写，不提供 API、UI、专用字段或验收承诺。

Research Watch 只作为历史/实时职责、同步时序和性能方法的参考，不复制其只读页面
限制、API、状态机或数据结构，也不修改 Research 页面。

## 2. 范围与能力矩阵

### 2.1 本次范围

- QwenPaw 原生 backend：全量历史、分页、实时重连和完整 turn Chat Copy。
- Harness backend：全量历史、分页、实时合并和断线重连；首期不提供 Copy。
- 所有入口都把历史读写切换到 `chat_*`。Console 发起的请求统一走同一套 Chat 链路，
  不按 legacy `channel` 字段分叉；手机飞书、钉钉、QQ 等外部入口也使用重构后的
  conversation/turn/message/block、持久化和恢复链路，不暴露 Copy。
- 前端必须读取 backend capability 决定是否展示 Copy，禁止散落 backend 名称
  判断。
- 保持发送、队列、停止、取消、失败、工具调用、附件、审批、压缩、usage、会话
  切换、插件卡片、Checkpoint 和 Runtime WebUI 兼容行为。

### 2.2 非目标

- 不恢复中断的模型请求、工具进程、`/mission` 或 `/goal` 等进程内执行状态。
- 不从半个 turn、reasoning、tool call 或 tool result 中间状态分支。
- 本次不支持用户、assistant、reasoning 或工具 block 编辑。
- 不要求新旧版本双写。
- 不自动删除或回收 workspace 文件、上传附件和生成 artifact。
- 不强制移除 Runtime WebUI，不修改 Research 页面。

## 3. 统一数据事实源

### 3.1 独立 `chat_*` schema

建立独立的 `chat_*` schema，不复用带 30 天清理策略的
`conversation_history`。它是以下数据的唯一规范化事实源：

- Chat metadata、head turn 与当前 context revision；
- turn lineage；
- turn、message、block 与大 block 引用；
- tool call/result 关联；
- usage 与 terminal 状态；
- runtime context/provider mapping；
- `/clear` 后的模型上下文重置状态；
- live run 的 durable materialization；
- checkpoint 所需的完整 Chat 物理快照。

新链路不得依赖完整 Session AgentState 才能展示历史，也不得为读取少量 usage 或首屏
消息反序列化完整运行时状态。Scroll manager 通过同一数据源的查询投影或索引完成
历史召回，不再维护第二份 transcript 事实。Console 请求不按 channel 分叉存储逻辑；
外部入口同样使用该模型，但不提供 Chat Copy 操作。

SQL、迁移、分页和事务应集中管理；是否使用 repository 及其具体边界由详细设计决定，
不得为了假想的多存储实现建立跨业务通用框架。

### 3.2 Transcript 保留

- 保存全部用户可见的 user、assistant、reasoning、tool call/result、应用可见消息、
  terminal 状态、时间戳和 usage。
- 取消或失败前已经 materialized 的内容必须保存。
- 不持久化逐 token delta，但必须定期物化活跃 partial block。
- partial block 在距离上次 materialization 达到 250ms 或新增 64 KiB 时落盘，以先
  达到者为准；工具结果和 terminal 状态立即冲刷。
- 上下文压缩只影响模型输入，不覆盖或删除 transcript。
- transcript 不设置自动 retention；只有用户删除 Chat 才删除其逻辑历史。
- replay event、传输缓存和临时快照可按策略清理，但不得破坏 transcript、分页、
  Copy 或 checkpoint restore。
- 超大 payload 可以外置到 blob/object storage，由 block 保存稳定引用、长度和完整性
  信息；不得通过截断、摘要替代或隐藏内容满足性能指标。

## 4. 存量迁移、升级与回退

### 4.1 数据来源

- `sessions/*.json` 是当前运行状态与可见历史的主要迁移来源。
- `conversation_history` 只作为补充迁移来源，用于找回已被 Scroll 从 session JSON
  压缩移除的旧 turn。
- 迁移完成后的读写、分页和恢复链路不得依赖上述旧结构。

### 4.2 迁移时机

禁止在服务启动时扫描和解析全部历史：

1. 启动只执行 schema upgrade 和轻量来源索引。
2. `running`、当前打开、pinned、最近更新的未归档 Chat 按顺序进入预迁移队列。
3. 服务空闲时在有界并发和 I/O 预算内执行预迁移。
4. 长期未访问或已归档 Chat 只在打开、继续发送、导出或 checkpoint restore 时迁移。
5. 首次新写入前必须通过 per-Chat migration barrier；同一 Chat 不允许 legacy 和
   `chat_*` 同时成为可写事实源。

用户先于后台迁移打开或发送时，页面显示独立迁移状态，允许离开或切换 Chat；发送
等待迁移成功后再执行。迁移状态不能作为 user/assistant/system 消息写入 transcript，
也不能进入模型上下文。失败时保留旧数据并提供重试，不得展示为空历史。

迁移必须按 Chat 分批、幂等、可中断续跑，并持久化：

- source fingerprint；
- migration version；
- cursor/progress；
- pending/running/completed/failed 状态；
- 脱敏错误摘要和重试次数。

完成后校验 turn/message/block 数量、顺序、tool call/result 关联、终态和关键内容
hash。校验失败保留原始数据并允许重试，不得把失败静默解释为空历史。

### 4.3 回退

- 不向旧 JSON 双写新 turn。
- 未删除 Chat 的旧 JSON 保留，供旧版本回退时读取升级前历史。
- 回退旧版本只能看到升级前消息；升级期间的新消息保存在 `chat_*`，重新升级后恢复。
- 用户删除 Chat 是保留旧源的例外：必须同时删除其 legacy Chat/session 数据，避免
  回退或再次升级后复活。

## 5. 身份、不变量与状态

- 每条 Chat lineage 在前端、分页、实时、缓存和后端中必须可稳定识别。
- 每个复制结果都是独立 Chat；历史、runtime context、队列、usage 和 live state 必须隔离。
- legacy/provider `session_id` 不能导致不同 Chat 串线。
- turn、message、block 和 tool call 必须能够稳定关联、排序、去重和恢复；是否分别使用
  ID、复合键或内部句柄由详细设计决定。
- default lineage 必须不可歧义地还原；复制后 source/target 不共享可变生命周期。
- 增量物化和实时传输必须能够判断新旧、重复和缺口；具体版本或序列机制不限。
- 新 Chat 必须先 durable create，再进入可写状态；创建和首条 turn 不得形成跨旧、新
  存储的半提交状态。

## 6. 从完整 Turn 复制 Chat

- 只允许从 terminal 的完整 turn 边界创建副本。
- 副本是新的独立 Chat，不是 source Chat 内可切换的 branch。
- 复制该点可达的 transcript 与已提交模型上下文，并重新生成目标 Chat 的实体 ID、
  session、default lineage 和 recall sequence。
- 不复制 workspace、文件、terminal、checkpoint、pending queue、active run 或工具执行态。
- source Chat 保持不变；target 后续写入、删除、归档和 checkpoint 生命周期完全独立。
- Console 只在 completed response card 暴露 Copy 入口；不提供 branch selector、activate
  或 leaf-delete 用户模型，也不公开对应的可变 branch API。
- 目标 Chat 默认标题为原始家族标题加递增数字，如 `Research (2)`、`Research (3)`。
- 正在运行或排队时不自动取消任务；必须先停止运行并处理队列，再允许复制。
- 模型上下文从目标 turn 的 source lineage 重建，不能通过重放有副作用的工具调用模拟。

## 7. `/clear` 上下文重置

`/clear` 只改变当前 Chat 后续模型上下文，不删除或隐藏 transcript，也不创建/删除 Chat。

- 上下文构造只包含本次 `/clear` 之后的有效内容。
- 从清理点之后复制 Chat 继承清理结果；从清理点之前的历史 turn 复制 Chat，不受后来发生的
  `/clear` 影响。
- Checkpoint 保存并恢复该上下文状态。
- `/clear` 与 active/queued turn 具有确定顺序，清理范围不受竞态影响。
- 刷新、服务重启和重新进入 Chat 后边界仍然有效。
- 实现可使用 marker、cutoff、version 或其他等价结构，不作限定。

## 8. 三级分页

### 8.1 Turn、Message 与 Block

- 普通历史优先按完整 turn 分页，尽量保持 user/assistant 语义完整。
- 超长 turn 继续按 message 分页，保持原始 block 顺序。
- 超大 message 继续按 block 分页。
- 单个 block 超限时必须按 fragment 读取，fragment 携带稳定 block ID、起止 offset、
  完整长度和完整性信息，连续读取后可逐字节重建。

每级分页同时受 turn/message/block 数和序列化字节数限制。任何超大实体都不能绕过
字节上限。

### 8.2 Cursor 与一致性

- cursor 是不透明、签名或防篡改的协议，不暴露 rowid、表名、路径或物理 schema。
- cursor 必须限制在原权限、原 conversation path 和原读取视图内，不能跨作用域复用；
  具体编码和组成不限。
- 同一分页链基于稳定的一致性视图；新 turn 不插入旧链中间。
- 一致性视图失效时返回明确 resync 信号，禁止静默返回空页。
- 同方向、同 cursor 的并发请求合并。
- 插入旧页后保持可见内容锚点，不直接恢复旧 `scrollTop`。
- tool call/result 必须稳定关联，不能出现孤立或重复结果；关联字段名不限。

## 9. 历史、实时与重连

- 持久化历史是 materialized transcript 的权威来源。
- SSE 只发送 active run 的增量和状态。
- 前端先订阅再同步历史；服务端原子固定同步边界并注册后续订阅。
- 同步视图构造期间的新事件进入有界订阅队列。
- history、同步视图和 live 必须可以稳定合并、去重和排序，具体 ID/sequence 结构不限。
- 已 durable 内容不能被旧同步视图回退，terminal 状态不能被迟到的 running 覆盖。
- 重连不逐 token replay；先批量合并 materialized state，再接收新事件。
- replay buffer 同时限制事件数和字节数；subscriber queue 有界。
- 慢订阅者溢出时断开并要求 resync，不阻塞主任务。
- replay 缺口或服务重启导致 event cursor 无法续接时，显式要求读取持久化历史校准。
- 任务结束后必须用持久化历史做最终校准。
- unload、switch、stop 和 terminal 后清理 reader、request、timer 和 subscriber。

## 10. 服务重启

首期只保证历史恢复：

- 已 durable 的 user/message/block/tool result 和 terminal turn 保持可读。
- 启动恢复扫描把遗留 `running` run 原子转为 `interrupted`。
- 已开始但没有 durable result 的工具显示为 `interrupted/unknown`，不得自动重试。
- 展示最后一次 partial materialization；尚未达到下一次 250ms/64 KiB 阈值的尾部允许
  丢失。
- 用户可以基于恢复历史继续发送或显式重试，系统不得伪装成原执行自动续跑。

以后只有 provider 和具体工具具备可验证的 resume/idempotency 契约时，才能单独设计
执行恢复。

## 11. Checkpoint

保留 `/checkpoints` 页面、手动 snapshot、可选自动 snapshot、preview/restore、
memory/files 选项和 restore 前 safety checkpoint。

- Checkpoint 继续执行物理 snapshot/restore，不创建新 Chat，不改变现有恢复
  语义。
- 快照必须包含承载目标 conversation、turn lineage、head、context boundary、turn/
  message/block、runtime checkpoint 和 provider mapping 的全部物理文件。
- 若使用 SQLite，必须使用一致性 backup/quiesce，禁止只复制主文件而遗漏 WAL 中的已
  提交数据。
- 单 conversation restore 只能恢复目标 conversation；存储布局或 checkpoint artifact
  不得要求覆盖整个共享数据库并连带回滚其他 Chat。
- memory/files 仍按现有选项恢复；恢复 transcript 不得隐式删除未选择的 workspace
  文件。
- 应用级 backup/export/import 包含统一数据文件和 schema version，并验证引用完整性。

## 12. 删除语义

删除 Chat 必须在同一领域事务中删除或标记删除：

- Chat metadata、turn lineage 与 context revision；
- turn/message/block 及其数据库引用；
- runtime checkpoint；
- provider/session mapping；
- usage ownership；
- run、queue、subscriber 和 migration 状态；
- 对应 legacy Chat/session 数据，防止回退或迁移后复活。

不得删除：

- 工具创建或修改的 workspace 文件；
- 上传附件、生成附件或其他物理 artifact。

只移除 Chat 对附件的引用。孤儿 artifact 回收若以后需要，必须作为独立、可审计功能
设计，不能隐含在删除 Chat 中。

## 13. 前端性能与渲染

- 按稳定 turn/message/block ID 渲染，已完成实体保持引用稳定。
- delta 只修改 active message 的尾部 block，并按时间和字节阈值合批。
- 工具、审批、错误、取消和 terminal 状态立即 flush。
- Markdown 使用稳定前缀与活跃尾部或等价增量方案，不能每 token 重解析全文。
- 同步/replay/历史转换按批让出主线程。
- reasoning 和工具详情折叠时不提前构建全部重型 DOM。
- 消息列表窗口化；DOM、live state 和缓存受数量与总字节上限约束。
- 用户查看旧历史时不抢滚动位置；位于底部时继续跟随实时内容。
- 缓存必须按 conversation path 和一致性读取视图隔离，不同 Chat 不共享可变尾页；具体
  key 结构不限。

## 14. 验收

验收同时包含“现有能力不回退”“新增能力正确”和“长任务/长历史体验提升”三部分。
任一部分未通过，都不能以另外两部分通过替代。

### 14.1 当前版本回归基线

开发前基于当前 `main` 固化可重复的功能、交互和性能基线，至少记录：

- 发送、队列、停止、取消、失败和重试；
- reasoning、工具调用与结果、审批、附件和插件卡片；
- 上下文压缩、usage、`/clear`、会话创建/切换/归档/删除；
- QwenPaw、Harness 和外部入口的适用行为；
- Checkpoint、Runtime WebUI 和现有降级/错误反馈；
- 首屏、流式输出、滚动、会话切换和断线恢复的用户可见行为。

现有适用场景必须保持功能、数据、交互入口和错误语义不回退。因重构必须改变的行为，
必须在开发前单独列出、说明原因并获得确认，不能在实现或验收阶段静默改变。

回归验证以自动化测试、协议/数据 oracle 和必要的人工交互检查共同完成；不能只验证
接口成功或页面无报错。

### 14.2 新功能矩阵

定点覆盖新/存量/空/归档 Chat，QwenPaw 与 Harness，普通文本、reasoning、工具并发、
超长 turn、超大 message/block、取消、失败、压缩、`/clear`、usage、刷新、服务重启、
SSE 中断、buffer/queue 溢出、快速切换、多 Chat 并发、Chat Copy、删除和 Checkpoint。

每个适用场景验证数量、顺序、内容、tool 关联、状态、时间戳、usage、刷新一致性、无
重复、无遗漏、无跨 Chat 污染。Copy 用例只要求 QwenPaw backend；Harness 必须验证
capability 和降级 UI。

还必须覆盖：

- 迁移中断后续跑、重复迁移、源数据冲突和冷归档 Chat 首次打开；
- 删除未迁移 Chat 后不复活；
- `/clear` 前后 Copy 以及 Checkpoint restore 的上下文重置状态；
- 进程在 partial materialization 前后、terminal commit 前被终止；
- terminal 后迟到旧事件、history/同步视图/live 三方重复实体；
- 单一超大 block fragment 的逐字节重建；
- Copy 与 queued send 的并发顺序；
- 删除 Chat 与 active run、migration、checkpoint 的并发。

### 14.3 性能方法

使用相同逻辑数据对比重构前 `main`、Research Watch 的共同历史/实时链路和重构后
Chat。禁止用少加载、少渲染、隐藏历史或截断 payload 制造收益。

- 冒烟可执行 5 次；正式数据每场景至少 30 个样本。
- 冷/热缓存分别测试，固定机器、浏览器、viewport、并发度和压缩状态。
- 报告 P50、P95、最大值、离散程度、样本数和准确计时边界。
- 性能对比前先验证 transcript 数量、顺序和内容等价。
- 记录查询/解码、payload、首屏可用/完成、分页、切换、重连、React commit、long
  task、DOM、heap、流式更新频率及服务端 buffer/queue 峰值。

初始门槛：

- 2 MiB 历史页后端 P95 不超过 300ms；
- 相对 `main`，核心交互 P50 目标提升至少 20%；
- 已低于 200ms 的基线，回退不超过 10%；
- 前端无超过 1 秒的主线程 long task；
- commit、DOM、内存、buffer 和 queue 不随 token 数或会话时长无界增长；
- 慢订阅者不影响主任务生成；快速切换期间页面保持可交互。

长历史数据量、单 block 上限、工具密度、并发度和长程任务持续时间必须在开发前根据
当前真实数据分布与可重复的压力数据集冻结，覆盖常见规模和高分位规模。实现前后使用
同一数据集、同一操作脚本和同一正确性 oracle。

### 14.4 用户体感与长程稳定性

性能验收不能只看后端查询时间，还必须验证实际交互：

- 打开长历史 Chat 时尽快出现可阅读内容，不因加载全部历史长期空白或锁住整页；
- 历史分页、迁移和大型同步期间，输入、停止、切换 Chat 等控制保持可响应；
- 向上加载不跳动，不丢失阅读位置；用户查看旧历史时实时输出不抢滚动位置；
- 流式文本保持连续可感知，不因过度合批长时间停顿，也不因高频更新造成明显卡顿；
- 工具密集 turn 的折叠、展开和结果加载不一次性构建无关重型内容；
- 长程任务持续运行时，服务端 buffer、subscriber、数据库连接和前端 live state、DOM、
  heap 保持有界，不随运行时间持续泄漏；
- 断线、刷新和服务重启后给出明确状态，不出现永久 running、空白回复或重复工具结果；
- 快速切换和返回 Chat 时不串线、不闪回旧内容、不重复动画回放历史。

用户体感采用统一交互脚本、浏览器性能记录和人工走查共同验收。指标改善不能以减少
可见内容、延迟正确性校准或取消现有能力为代价。

### 14.5 发布门禁

- 当前版本回归矩阵全部通过，或仅存在已明确批准的行为变化；
- 新功能正确性矩阵和故障注入场景通过；
- 长历史、超大消息、工具密集和长程任务达到冻结的性能与资源门槛；
- 没有已知的数据丢失、跨 Chat 污染、不可恢复迁移或无限资源增长问题；
- 回滚说明、迁移观测和停止条件可执行，并能区分“旧版本不可见”与真实数据丢失。

## 15. 实施门禁与交付物

动代码前必须完成并评审：

1. 数据模型与不变量：ERD、状态机、索引、所有权和删除关系。
2. API 协议：capability、三级 cursor、snapshot、SSE event、错误与 resync。
3. 事务设计：send、terminal、Copy、`/clear`、delete、checkpoint 和 crash point。
4. 迁移设计：来源优先级、合并/去重、进度、写屏障、校验、失败恢复和回退。
5. 前端状态模型：history/live 合并、窗口化、缓存、锚点和资源清理。
6. 基准数据生成器、正确性 oracle、测量环境和报告模板。
7. 分阶段上线、观测、停止条件和回滚说明。

实施按可验证的垂直切片推进，每个主要功能完成后进行定点测试和 Code Review。除非
用户另行明确要求，不执行全量 `npm run build`、`npm run format` 或
`npm run test`。
