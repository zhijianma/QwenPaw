# Chat 重构 M3–M7 完成度审计

> 审计日期：2026-10-23
> 审计对象：当前 working tree（`27d22cd18d+working-tree`）
> 结论：M3–M6 的实现与定点验证已完成；M3 包含三级分页、有界窗口、双向移动、
> 回到最新和离窗 user-turn 精确导航；M7 自动化性能、稳定性、隔离 V2 Console
> smoke 和关闭态兼容 smoke 已通过。本机已进入显式 V2 Console 验收并完成真实
> provider 发送/stream/停止/主动刷新断线、历史刷新和 background migration 恢复；
> 已修复 canceled turn 刷新状态，以及断线重连时 active user 缺失导致的跨 turn
> 错误归组；最终产品语义已收敛为从 completed response 复制独立 Chat，真实 provider
> continuation、source/target 隔离、数字标题与 Console 跳转均已通过。8 GiB 受控 Linux VM 的 Extreme 容量
> 门禁也已通过。用户于 2026-10-23 明确渠道定点只要求 Console，故渠道矩阵已经完成；
> 同日已确认正式默认开启 normalized Chat Storage/UI V2，仅剩版本交付。

## 1. 判定口径

本审计不把“代码存在”“测试 mock 通过”“真实链路验证通过”混为一谈：

- **已通过**：存在实现、定点测试或原始性能数据，且结果满足设计门槛；
- **隔离实测**：在独立 working directory 和独立端口运行真实前后端 UI，但不调用真实
  模型或外部渠道；
- **契约覆盖**：入口 adapter/application contract 已自动化验证，但没有真实账号或设备
  的端到端证据；
- **待发布验收**：需要真实 provider、真实渠道、低内存机器或用户发布决策，不能由本地
  mock、静态推演或地址空间限制实验替代。

本轮没有执行全量 `npm run build`、`npm run test` 或 `npm run format`，没有 commit、
push 或创建 PR。2026-10-23 用户显式开启本机 storage/UI V2 后，当前服务已迁移真实
活跃 workspace；旧 session JSON 和 `history.db` 保留且未双写。

## 2. 总体完成度

| 里程碑                    | 状态                   | 核心证据                                                                                                                                            | 剩余事项                                           |
| ------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| M3 三级分页与有界前端     | 已通过                 | 三级 keyset page、fragment、签名 cursor、normalized store、双向窗口、回到最新、离窗 seek、锚点、lazy block；Long/Huge/Tool dense 测试和浏览器基准通过 | 无实现阻断项                                       |
| M4 实时与断线重连         | 已通过                 | durable materializer、subscribe-before-sync、有界 replay/subscriber、幂等 merge、restart interrupted；真实 provider 主动刷新重连与 2 小时 soak 通过 | 无实现阻断项                                       |
| M5 Chat Copy 与 `/clear`  | 已通过                 | completed-turn 独立 Chat copy、source/target 隔离、数字标题、clear context revision；真实 Console provider continuation 通过                        | 无实现阻断项                                       |
| M6 Checkpoint、备份、删除 | 已通过                 | 整 Chat checkpoint、legacy upgrade、backup manifest/import、delete saga、防复活、不删附件                                                           | 发布环境恢复演练可作为灰度观察项，不是当前实现缺口 |
| M7 性能、灰度与默认开启   | 已通过，默认开启       | 30× cold/warm、30 分钟 Long/Tool dense heap、2 小时 live、16/8 GiB Extreme、rollout/rollback、静态评审、真实 Console 矩阵                           | 提交交付                                           |

## 3. 需求到证据追踪

### 3.1 全量历史、统一事实源与永久保存

**状态：已通过自动化与真实活跃 workspace cutover。**

- `src/qwenpaw/app/chats/application.py` 统一 acceptance、queue、runtime materialization 和
  recovery；Console、Harness 与外部 channel adapter 不再定义第二套正文写入语义。
- `src/qwenpaw/app/chats/storage/` 提供 catalog、每 Chat 正文库、recall projection、schema
  validator、connection LRU 和 repair/recovery primitive。
- `src/qwenpaw/app/chats/migration/` 实现轻量 inventory、活跃度优先、访问 barrier、稳定
  JSON/history snapshot、dedup/conflict 检查、临时库校验与幂等续跑。
- `src/qwenpaw/app/chats/scroll_bridge.py` 将 Scroll search/expand/session 读取切到新正文与
  recall projection；`conversation_history`/`history.db` 只作为 legacy migration 输入。
- `ready_v2` 后 legacy 写入被门禁；新 transcript 不受 retention、compact、clear 或 Scroll
  eviction 清理，只在删除 Chat 时删除。
- 旧 JSON 保留但不双写；旧二进制回退只能看到升级前历史，这是已确认契约。

证据：M1/M2 storage、migration、Scroll、workspace、application 定点测试均包含在后端
195 passed、1 skipped 的最终回归中。2026-10-23 真实 cutover 又发现并修复 duplicate
legacy `Msg.id` 和附件双表示两类 normalization 缺陷：两个失败 snapshot 分别完整物化为
1,133 messages/13 turns 和 207 messages/4 turns，source/stored manifest 相等。CodingAgent
rollout 从 29 ready + 2 failed 变为 31 ready + 0 failed，recall projection 31 ready，
`healthy=true`；旧源文件仍在。

后续 rollout 复核捕获到一次单向 cutover 违例：已发布 Chat 执行 `/compact_str` 后，
BaseChannel 的 turn usage 仍写回 legacy session JSON；inventory 随后因 fingerprint 漂移把
migration 行从 `ready_v2` 降为 `legacy_only`，尽管 catalog lifecycle 与正文库仍为 ready。
修复后，带 `chat_conversation_id` 的 V2 请求不再执行 legacy usage persistence；inventory
对 `lifecycle=ready` 只维护 V2 事实，不因保留文件变化降级、不覆盖原
migration proof，也不为原生 V2 Chat 创建 migration 行。真实 catalog 热重载后从
`4 ready_v2 + 1 legacy_only` 自愈为 `5 ready_v2`，原 fingerprint 保持不变，正文库
`quick_check=ok`，rollout 无 warning 或 stop reason。

### 3.2 Turn/message/block 三级分页和超大 block

**状态：已通过。**

- `src/qwenpaw/app/chats/api.py` 和 storage repository 提供 view、turn、message、block、
  fragment、tail 接口；父页渐进内嵌，超预算实体下钻。
- `src/qwenpaw/app/chats/pagination_cursor.py` 使用签名 cursor 绑定 conversation、
  固定 head、view version、层级、方向和权限主体；restore/替换后明确返回
  `view_invalidated`。
- 数量和序列化字节采用双门禁；2 MiB 页面不因首实体过大而突破限制；64 MiB block 通过
  Base64 fragment 逐字节重建并支持取消。
- `console/src/pages/Chat/chatHistoryStore.ts`、`chatHistoryPagination.ts`、
  `ChatHistoryPaginationBridge.tsx` 和 `LazyHistoryBlock.tsx` 实现 normalized store、尾页
  初载、历史 sentinel、稳定 anchor、decoded cache 上限和按需 fragment。
- 搜索结果携带 `turn_id/message_id`；`loadHistoryAtTurn` 使用服务端 target-turn page，并把
  MessageList 切换到以目标 turn 为中心的 5-turn 本地窗口，再定位稳定 user Bubble。
- 新 Console 的 V2 路径不读取全量 detail；503 只对明确 capability code 进入兼容读取，
  不把普通过载错误退化为全量下载。

验证：cursor scope/tamper/head invalidation、并发 cursor 合并、64 MiB block、anchor
偏移、快速切 Chat、Long/Tool dense DOM/heap 均通过。分页后端各场景 30 样本 P95
为 5.65–83.36 ms，低于 300 ms 门槛。

### 3.3 稳定实时流、断线重连和服务重启

**状态：协议、故障注入和长程稳定性已通过；真实 provider 链路主体通过。**

- `src/qwenpaw/app/chats/event_materializer.py` 在 user turn durable commit 后才允许执行；
  partial block 按 250 ms 或新增 64 KiB 先到者刷盘，tool/error/cancel/terminal 立即刷盘。
- terminal event 只在剩余内容、usage、tool/context/run 状态原子提交后发布。
- `src/qwenpaw/app/chats/live.py` 实现原子 subscribe-before-sync、barrier、immutable snapshot、
  2048 events/8 MiB replay 和 512 events/2 MiB subscriber 上限；慢订阅者被隔离并要求
  resync，不反压 runtime。
- `console/src/pages/Chat/chatLiveController.ts` 按 run epoch、entity version 和 byte offset
  幂等合并；gap、overflow、epoch lost 走 durable tail calibration。
- 服务重启只保留最后 durable 历史，并将 run、queue、tool 标为 interrupted；不恢复
  模型调用、工具执行或自动调度。

验证：acceptance/terminal 前后 kill、sync 时 streaming、duplicate/reorder/gap、overflow、
迟到事件、多 tab/owner epoch 测试通过。两小时 wall-clock soak 完成 144,000 events、119
次重连，最终 replay 保持 2,048 events/647,168 bytes，correctness 通过。

2026-10-23 真实 DashScope Console 发送和 stream 完成，刷新后 durable assistant 内容
一致；真实 stop 将 turn 持久化为 `canceled`、保存 partial block 并清空 `active_run`。
normalized history 原先没有把所属 turn 终态写入已有的 `qwenpaw_turn_state` envelope，
导致刷新后默认 completed；补齐该契约后真实页面显示“已取消 · 1 个步骤”，相邻正常
turn 仍为“已完成”。随后在新 run 流式输出期间主动刷新页面，后端从相同 run 继续推进并
最终完整持久化 `RECONNECT-1` 至 `RECONNECT-1000`，run/turn 均为 completed、
`active_run=null`、SQLite `quick_check=ok`。该实测同时暴露 live snapshot 只有 assistant、
没有 accepted user envelope，导致新响应暂时继承上一 canceled turn 的状态；
`ChatEventMaterializer` 现以有界 initial live message 携带本次 user envelope，前端重连窗口
按 user/assistant 完整归组，后端 12 项与前端 17 项定点回归通过。修复后的第二次真实
热重载复验中，active sync 明确包含当前 user/assistant；终态无需二次刷新即显示“已完成”，
`HOTFIX-1` 至 `HOTFIX-200` 共 200 行连续、唯一、无缺失，run/turn 均 completed、
`active_run=null`、SQLite `quick_check=ok`。

### 3.4 从完整 turn 复制 Chat 与 `/clear`

**状态：已通过自动化、真实 Console UI 和 provider-backed continuation 验收。**

- Copy 只从 source current head path 中的 completed turn 创建；结果是新的独立 Chat。
- target 物理复制目标 turn 及之前的 transcript/context，重新生成 conversation、session、
  turn/message/block/context ID 和 recall sequence；source 不变。
- target 不复制 workspace、文件、terminal、checkpoint、pending queue、active run 或工具
  执行态；source/target 的 run、queue、usage、live、cache、删除和归档生命周期独立。
- QwenPaw capability 为 `chat_copy=true`；Console 只在 completed response card 显示
  Copy icon。分页、实时、缓存和运行态均以 conversation 为唯一作用域。
- 默认标题按原始家族标题递增为 `(2)`、`(3)`；显式名称仍优先。
- `/clear` 创建当前 Chat 的 clear context revision，不改变可见 transcript 或历史 usage；
  clear 前 turn copy 不继承后来 clear，clear 后 turn copy 继承 clear。
- 本阶段没有用户消息编辑 API、UI 或专用 schema。

真实 Console 从 completed response 创建 target 后，target 精确复制 source head path 的
5 个 turns，不相关 turn 未混入；目标使用独立 session 调用 DashScope 并返回唯一标记
唯一 continuation 标记，target 从 5 增至 6 turns，source head 与 FTS
均未变化。随后修复成功后错误提示：根因是 Copy 成功后调用了作用域外的
`buildCurrentSessionPath`，抛出 `ReferenceError` 并被宽泛 catch 误报失败；现改用统一
`buildChatPath`，且只对创建 API 失败显示失败提示。真实 UI 再次验证只显示
`Chat copied`，导航成功，标题为原标题加 `(2)`；验收副本已按正式删除流程清理。

### 3.5 Checkpoint、应用备份/导入和删除 Chat

**状态：已通过自动化。**

- `src/qwenpaw/checkpoints/chat_artifact.py` 与 checkpoint service 在快照前强制 flush active
  partial，使用 SQLite online backup，校验 schema、identity、integrity 和 SHA-256。
- V2 checkpoint 保存整个目标 Chat 的 transcript、context revision、runtime blob 和
  provider alias；restore 保留 catalog 的 name/pin/archive/group，并只修复目标 Chat 的
  catalog/alias/recall。
- restore 先建 safety checkpoint，再 staging 验证、关闭连接、原子替换、清理 sidecar、
  fsync、向前迁移，并令旧 cursor/live/cache 失效；非终态执行只恢复为 interrupted。
- legacy JSON checkpoint 在 staging 升级为 V2，不拼入 checkpoint 之后的 history 数据。
- 应用 backup 对 catalog 和各正文库执行 SQLite backup，使用 storage manifest 和全量
  hash/reference validation；recall 作为 derived 在导入后重建，不直接复制 live WAL/SHM。
- `src/qwenpaw/app/chats/deletion_service.py` 以 tombstone + 可恢复 saga 阻止新写并删除全部
  runtime/checkpoint/provider/usage ownership、recall row、正文库和对应 legacy
  ChatSpec/session；不删除附件、workspace 文件或 artifact 物理文件。

验证覆盖 active partial checkpoint、整 Chat restore、其他 Chat hash/head 不变、旧 checkpoint
升级、backup/import staging、删除中断续跑、legacy 防复活和附件保留。

### 3.6 性能、长历史和工具密集场景

**状态：冻结验收机与 8 GiB 受控 Linux VM 的自动化门禁均已通过。**

| 指标                             |                         结果 |             门槛 | 判定       |
| -------------------------------- | ---------------------------: | ---------------: | ---------- |
| Long cold 可读尾部 P95           |                    359.50 ms |           ≤1.5 s | 通过       |
| Long warm 可读尾部 P95           |                    412.80 ms |          ≤500 ms | 通过       |
| 浏览器 React commit P95          |   cold 12.10 / warm 17.40 ms |           ≤50 ms | 通过       |
| 浏览器最大 long task             |                       162 ms |             <1 s | 通过       |
| timeline roots                   |                           10 |              ≤48 | 通过       |
| Long 30 分钟 retained heap       |                       +4.03% |             ≤20% | 通过       |
| Tool dense 30 分钟 retained heap |                       +3.86% |             ≤20% | 通过       |
| 2 小时 live replay               | 2,048 events / 647,168 bytes |  ≤2,048 / ≤8 MiB | 通过       |
| Extreme 1 GiB / 50k messages     |    P95 56.94 ms；RSS 2.47 GB | page P95 ≤300 ms | 读路径通过 |
| 8 GiB VM Extreme                 |  P95 119.66 ms；RSS 3.36 GiB | page P95 ≤300 ms | 通过       |

正式浏览器 cold/warm 各 30 样本，环境为 Chrome 146、1440×1000、16 GiB arm64、无
CPU/network throttling；早期受 locator 或 trace 写盘污染的数据保留但不参与裁决。

Extreme 在 16 GiB 验收机上的 peak footprint 为 3.82 GB、无 swap。随后在 Colima macOS
Virtualization.Framework 的 8 GiB Linux VM（实际 `MemTotal` 8,307,167,232 bytes、4 vCPU、
无 swap）使用相同 1 GiB logical manifest 完整复测：10,000 turns、50,000 messages 与
50,000 blocks 全部相等；5 次 turn tail P50 98.21 ms、P95/max 119.66 ms，response
2,074,100 bytes；进程峰值 RSS 3,606,712,320 bytes，VM 最低可用内存
3,882,176,512 bytes，swap/oom kill 均为 0，总耗时 535.57 s。该结果来自真实受控 VM
内存上限，不使用 macOS `RLIMIT_AS` 模拟，因此关闭原低内存容量 warning。

原始数据：

- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-pages.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-browser-v2-final.json`
- `docs/architecture/benchmarks/chat-refactor-m7-browser-v2-final-traces/`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-browser-v2-five-turn.json`
- `docs/architecture/benchmarks/chat-refactor-m7-browser-v2-five-turn-traces/`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-browser-heap-long-30m.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-browser-heap-tool-dense-30m.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-live-2h.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-extreme.json`
- `docs/architecture/benchmarks/chat-refactor-m7-working-tree-extreme-8g-vm.json`

## 4. 回归与质量证据

- 前端：Chat/SDK lifecycle/normalized history/pagination/live/Copy/lazy block/queue/session
  conversion 共 11 个定点测试文件，`400 passed`。
- 后端：Console/Feishu/DingTalk/QQ contract、integration、V2 API/application/delete/live/
  fault/scroll/workspace、backup、checkpoint、session hook 和 Scroll command 共 19 个文件，
  `195 passed, 1 skipped`。
- TypeScript：定点 `npx tsc --noEmit` 通过。
- ESLint：生产改动文件 0 error；`Chat/index.tsx` 保留 16 个既有 Hook warning。
- `ChatPage.coverage.test.tsx` 全文件仍有 26 个既有 `no-explicit-any` error，本轮没有新增
  `any`，未为本任务扩大修改范围。
- 改动源码和文档的定点 Prettier 通过；`git diff --check` 通过。
- 真实 `localhost:5173` 关闭态 Console 可列出 5 个既有 Chat 并只读打开消息，未发现
  请求/渲染失败；只有既有 React/Ant Design deprecation warning。
- 隔离 8011/5177 V2 Console 的早期 smoke 完成 normalized 消息显示；最终验收以独立
  Chat Copy 实现为准。
- 真实 `localhost:5173` V2 Console 完成 provider 发送、stream、stop 和刷新；发现并
  修复 scalar history block、Scroll headline 和 canceled turn 状态三项刷新渲染问题；
  canceled 与相邻 completed turn 的真实页面状态均已复验。
- background migration 增量回归：normalization/orchestrator/snapshot/inventory/rollout
  共 `35 passed`；两个真实失败 snapshot 只读 normalization、临时物化与 manifest
  校验通过；恢复后的 turn cursor 分页覆盖 13/13 与 4/4 turns，无重复或缺口。
- 本轮 migration 两个 Python 文件的 AST、mypy、Black、flake8、Pylint 和
  `git diff --check` 全部通过。
- canceled/interrupted/failed history 状态映射与 message display 定点回归
  `92 passed`；相关 TypeScript ESLint、Prettier 和仓库 `git diff --check` 通过。
- migration/history 修复后重新执行 M4 定点回归：后端 live、fault injection、
  materializer、cancel-save `28 passed`；前端 live controller、SDK reconnect/lifecycle
  `137 passed`。
- 真实主动刷新断线完成 1,000 行 provider 输出；修复 active user live snapshot 后，
  backend event/workspace/live 为 `29 passed`、`chatHistoryStore.test.ts` 为 `17 passed`，
  Python AST/mypy/Black/flake8/Pylint 与 TypeScript ESLint/Prettier 全部通过。
- 修复后真实热重载再次完成 200 行 provider 输出；页面终态、API、SQLite 与内容连续性
  四侧一致，未发现新的日志错误。
- 最终独立 Chat Copy 完成真实 provider continuation、source/target 数据与生命周期隔离；
  Copy 成功后的错误 toast 与数字标题又完成真实 UI 复验。分页、实时和上下文构造均以
  conversation 为边界。
- 单向 cutover 修复的 inventory/orchestrator/application/Console/legacy usage 定点回归
  `93 passed`；相关四个 Python 文件的 AST、mypy、Black、flake8、Pylint 与
  `git diff --check` 全部通过。真实状态修复后仅见既有 LM Studio discovery warning。
- 离窗导航与开发期单时间线 schema v2 收口后，后端存储 `32 passed`；前端 API、页面、
  normalized store、live、5-turn 窗口、精确 seek 共 8 个文件 `239 passed`；TypeScript、
  定点 ESLint/Prettier、Python pre-commit 与 `git diff --check` 均通过。

## 5. 发布阻断项与下一步

渠道验收已按用户于 2026-10-23 确认的范围完成：Console 是唯一渠道定点门禁，真实
provider 发送、stream、停止、主动刷新断线恢复、durable 历史与独立 Chat Copy 均已
通过。Feishu 真机收发只作补充证据；DingTalk/QQ 不纳入本次发布矩阵，未完成的测试不计
为通过，也不再构成阻断。

默认启用决策已完成：用户于 2026-10-23 确认正式默认开启 storage/UI V2；已有
`ready_v2` Chat 后只能关闭新 UI，不能恢复 legacy 写入。

以下项目不是已知代码缺陷，但仍需在版本交付前完成：

1. **版本交付**：当前改动尚未 commit/push/创建 PR。应先由用户授权，再按独立能力拆分
   commit，并在合入前执行规定的 Code Review。

因此，当前最准确状态是：**实现、自动化验收、真实 Console 主链路与 8 GiB 容量验收
完成，渠道矩阵全部通过，默认路由已经开启；仅剩版本交付。**
