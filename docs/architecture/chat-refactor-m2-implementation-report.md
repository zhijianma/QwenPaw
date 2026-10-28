# Chat 重构 M1–M2 实施与验收报告

> 日期：2026-10-22
> 基线：`main@27d22cd18`
> 实施状态：feature flag 默认关闭；M1–M2 核心链路已实现，发布级人工/浏览器门禁未执行

## 1. 本阶段交付

- 新增 workspace `chat_catalog.db`、每 Chat 正文数据库和可重建
  `chat_recall.db`。正文库是 transcript、turn/run、context 和 provider alias mirror 的
  权威源；catalog 的 alias claim 是 workspace 路由权威源。
- `chats.json` 与 `history.db` 只作为 legacy migration 输入。V2 cutover 后不双写，旧文件
  保持原状，因此回退旧版本只能看到升级前历史。
- 启动只 inventory legacy metadata；非归档 Chat 按 pinned/活跃度后台迁移，归档 Chat 在
  实际读取或发送时通过 per-Chat migration barrier 延迟迁移。
- Console、Harness 和所有经 `Workspace.stream_query()` 的外部渠道共享 alias resolve、
  durable acceptance、event materializer、terminal commit 和 restart recovery。
- queued send 在 admission 事务内创建完整 turn/user message/block；前序 run 终态后按
  branch FIFO 原子 claim。等待任务被取消时，pending queue 或并发 claim 的 accepted run
  原子转为 canceled，不阻塞后续队列。服务重启只把 run/queue/tool 转为 interrupted，
  不恢复执行。
- partial text 在 250ms 或 64KiB 阈值先到时写入；单个超大 delta、completed block 和
  UTF-8 block 都拆为不超过 64KiB 的 segment。tool result/error/cancel/terminal 为立即
  边界。审批 UI 仍沿用现有 `ApprovalService` 与 Console polling，但 request/result 会在
  创建和决议时直接写入当前 run 正文库，不依赖 envelope 才能进入完整 transcript。
- `/clear` 的 V2 command adapter 只提交空 runtime context；已落盘 transcript 不删除、不
  隐藏，legacy session JSON 不再写入。
- Scroll 的读写兼容层指向 `chat_recall.db`，不创建或更新旧 `history.db`；recall 投影失败
  不回滚正文 terminal，并写入 durable repair queue。

## 2. 可靠性证据

已覆盖以下故障和边界：

- schema 的类型、PK/FK、unique/check/partial unique/index 校验和 corruption quarantine；
- 创建 reservation、临时库校验、WAL checkpoint、fsync、原子 rename 与幂等恢复；
- migration 在 snapshot/normalize/validate/rename/catalog publish 各边界失败后重试；
- legacy stable key 冲突拒绝、unknown block 保留、Scroll eviction index source gap 诊断；
- catalog alias 不允许被另一个 Chat 或 stale legacy inventory 静默抢占；
- user acceptance 幂等、active-run queue、FIFO claim、restart interrupted；
- catalog intent 在 source commit 前后 crash 的恢复，以及 queue waiter cancel/claim 竞态；
- 250ms/64KiB partial、超大 UTF-8 segment、多 text block、text+image、tool call/result；
- governance/fallback 两条工具审批路径共享 approval request/result 正文持久化；
- terminal source commit 在 catalog/recall projection 和 repair enqueue 失败时仍保持成功；
- fresh workspace、active/cold migration、V2 API 禁止回读 legacy JSON、V2 `/clear` 不清
  transcript。

本轮定点执行结果：

- Chat/storage/workspace/session/command/approval 影响面：`629 passed, 1 warning`；
- Harness 与 unified queue 相邻路径：`27 passed`；
- Console Chat lifecycle/approval/attachment/large-session 定点：`123 passed, 1 skipped`；
- 全部改动文件 pre-commit：AST、JSON、mypy、black、flake8、pylint 等全部通过。

warning 是既有 FastAPI TestClient 的 Starlette/httpx deprecation。未执行全量
`npm run build/test/format`，也未把静态推演表述为执行通过。

## 3. M2 后端性能

边界与 M0 一致，均为完整 transcript 的 warm read，不含 HTTP、前端转换、React 或 DOM。
每个 profile 先通过 correctness manifest，再预热一次并采集 30 个样本。

| Profile | 逻辑内容 | 指标 | main JSON | M2 正文库 | 改善 |
|---|---:|---:|---:|---:|---:|
| Typical | 500 messages / 5MiB | P50 | 92.58ms | 11.32ms | 87.8% |
| Typical | 500 messages / 5MiB | P95 | 105.63ms | 19.64ms | 81.4% |
| Long | 10,000 messages / 100MiB | P50 | 2,010.55ms | 296.72ms | 85.2% |
| Long | 10,000 messages / 100MiB | P95 | 2,195.83ms | 436.51ms | 80.1% |

M2 数据库文件为 Typical 13,443,072 B、Long 258,269,184 B；logical manifest 分别为
500/500 和 10,000/10,000 messages/blocks。SQLite 物理文件大小不能与旧 JSON serialized
bytes 当作同一指标，性能比较只使用相同 logical transcript 和同一机器上的完整读取耗时。

原始数据：

- `benchmarks/chat-refactor-main-27d22cd18-backend.json`
- `benchmarks/chat-refactor-m2-working-tree-backend.json`

## 4. 阶段边界与剩余风险

- 当前 detail API 仍返回完整 transcript；M3 才引入 turn/message/block 分页、fragment 和
  前端窗口化。因此本阶段改善了存储读取，但未声称完成浏览器冷打开、DOM、React commit
  或 heap 门禁。
- M4 的 durable live cursor、subscribe-before-sync、有界 replay/subscriber queue 和断线
  resync 尚未实施；当前 TaskTracker 的既有实时行为由 feature flag 默认关闭保护。
- 相同 `client_request_id` 的 durable acceptance 会幂等返回，但 M2 明确拒绝启动第二次
  runtime；将重复请求附着到既有 live stream 属于 M4。这样在 M4 前优先保证不重复执行。
- Branch API/UI、Checkpoint 全量物理恢复、删除 saga 与默认开启分别属于 M5–M7。
- connection LRU/async lock primitive 已有边界测试；当前短事务仍使用按操作连接，热路径
  接入与分页访问模式一起在 M3 完成，避免在事件循环执行同步 SQLite I/O。
- 当前 V2 flag 默认关闭。进入灰度前仍需补浏览器人工矩阵、真实服务 cold/warm、
  Extreme/Tool-dense/Long-run/Concurrent 以及磁盘满/SQLite busy 的发布级故障注入。

