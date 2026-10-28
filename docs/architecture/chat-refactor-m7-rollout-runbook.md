# Chat 重构 M7 灰度与回滚 Runbook

> 状态：工程灰度能力、自动化性能/稳定性门禁与真实 Console 渠道矩阵均已通过。
> 用户于 2026-10-23 确认正式默认开启 normalized Chat Storage/UI V2。

## 1. 两个开关的职责

| 开关                      | 默认 | 职责                                      | 允许的回退动作                          |
| ------------------------- | ---: | ----------------------------------------- | --------------------------------------- |
| `QWENPAW_CHAT_STORAGE_V2` |  `1` | 选择 normalized storage、迁移和统一写链路 | 可显式设为 `0`，但仅限尚未 cutover 的 workspace |
| `QWENPAW_CHAT_V2_UI`      |  `1` | 暴露分页、实时重连和 Chat Copy UI API     | 可随时关闭，不改变 V2 数据事实源        |

`QWENPAW_CHAT_V2_UI` 是 storage V2 的从属 opt-out gate，不是独立启用入口。只有 storage
V2 已开启且 UI gate 也开启时，V2 UI API 才有效；显式关闭 storage V2 时 rollout status
固定返回 `ui_enabled=false`。默认路由现为 normalized；UI gate 仍只负责界面能力降级，
不能单独触发迁移或恢复 legacy 写入。

`ready_v2` 是不可逆写门槛。任一 Chat 进入该状态后，当前新版本部署不得把
`QWENPAW_CHAT_STORAGE_V2` 改回 `0`。紧急降级必须保持 storage V2，改为设置
`QWENPAW_CHAT_V2_UI=0`；Console 收到明确的 503 capability response 后使用兼容读取，
不会恢复 legacy 写入。

新版本同时有 fail-closed 门禁：catalog 中存在 `ready`/`ready_v2` 后若关闭 storage V2，
Chat 服务拒绝选择 legacy repository，并提示只关闭 UI 开关。

回退旧二进制是已确认的兼容策略：旧版本只能看到升级前 JSON；升级后的消息仍保存在
`.qwenpaw/chat/`，但旧版本暂时不可见。禁止为让旧版本可见而反向双写或覆盖新库。

## 2. 灰度顺序

灰度发布单位是 workspace/process，workspace 内 transcript 仍按 Chat 独立迁移：

1. Gate 0：两个存储开关保持默认，执行备份、定点回归和基准；
2. 内部 workspace：开启 storage V2，先验证新 Chat 和 Console；
3. 活跃 legacy Chat：按 pinned、最近更新、未归档顺序后台迁移；
4. 渠道入口：按发布范围验证统一 Chat 写链路；本轮真实渠道定点仅要求 Console；
5. 冷/归档 Chat：只在打开、发送、导出或 checkpoint restore 时迁移；
6. 扩大 workspace 比例；每一级至少跨过一个完整观察窗口；
7. 全部门禁通过后，单独取得用户确认，再修改默认值。

禁止双写。迁移过程中可做 shadow-read hash/count/order/tool-link 对账，但对账结果不能
成为第二事实源。

## 3. 观测入口

`GET /api/chats/rollout-status` 返回不读取 transcript body 的轻量快照：

- `lifecycle`：creating/ready/quarantined/deleting/deleted 数量；
- `migrations`：迁移状态数量；
- `projection_repairs`：catalog/recall repair 状态；
- `deletion_jobs`：删除 saga 状态；
- `runtime_intents`：acceptance/terminal reconciliation 状态；
- `recall_projection_states` 和 `recall_projection_lag`：投影状态及 ready Chat 与
  recall generation 不一致数量；
- `healthy`、`stop_reasons`、`warnings`；
- `enabled` 与 `ui_enabled`：数据链路和新 UI 的独立状态。

状态接口只读 control plane，不解析消息正文。live hub 另提供只读 resource snapshot，供
进程内采集 `next_sequence`、replay events/bytes、subscriber count 和 terminal 状态。

## 4. 硬停止条件

出现任意一项，立即停止扩大灰度：

- `stop_reasons` 非空：quarantine、terminal migration failure、recall projection
  failure 或 repair running；
- accepted user turn、terminal transaction、tool link 或 Chat Copy 谱系任一对账失败；
- page response 超过 2 MiB，或 page backend P95 超过 300 ms；
- 任一跨 Chat 内容污染；
- 迁移后 legacy 路径仍可写，或删除 Chat 可被 legacy 数据复活；
- checkpoint/backup/import 改变非目标 Chat hash/head；
- live replay、subscriber queue、decoded cache、DOM 或连接数突破硬上限；
- 相比同环境基线出现未批准功能回退或错误率明显上升。

以下 warning 持续 5 分钟也停止扩大灰度并排查：

- retryable migration failure；
- pending projection repair；
- recall projection lag；
- acceptance reconciliation pending；
- 非预期长期 deletion in progress。

## 5. 回滚操作

### 5.1 新二进制内 UI 降级

1. 停止扩大灰度并保存 rollout status、服务日志和相关 conversation ID；
2. 保持 `QWENPAW_CHAT_STORAGE_V2=1`；
3. 设置 `QWENPAW_CHAT_V2_UI=0` 并按既有部署方式重启服务；
4. 确认 status 为 `enabled=true`、`ui_enabled=false`；
5. 验证现有 Chat 仍从 V2 读取/写入，Console 使用兼容 transcript 视图；
6. 修复后先恢复小范围 UI，再重新执行对应门禁。

### 5.2 旧二进制回退

1. 先完成应用备份，保留 `.qwenpaw/chat/` 全部 normalized 数据；
2. 明确通知：旧版只显示升级前历史，新消息不是丢失而是旧版不可见；
3. 禁止旧版对已迁移 Chat 做会造成认知冲突的继续发送；
4. 恢复新二进制后重新启用 V2，执行 migration/repair/reconciliation；
5. 用 transcript manifest 和目标 Chat hash 验证数据完整性。

当前没有 normalized-to-legacy 反向导出，也不允许用旧 JSON 覆盖 V2。

## 6. 当前 M7 证据（2026-10-22）

分页基准原始数据：
`benchmarks/chat-refactor-m7-working-tree-pages.json`。

| 场景       | 指标      |      P50 |      P95 |       Max |
| ---------- | --------- | -------: | -------: | --------: |
| Typical    | turn tail | 44.06 ms | 83.36 ms | 101.58 ms |
| Long       | turn tail | 38.67 ms | 65.17 ms | 219.73 ms |
| Tool dense | turn tail | 38.25 ms | 50.89 ms |  53.95 ms |
| Huge block | turn tail |  2.23 ms | 15.30 ms |  23.48 ms |
| Huge block | fragment  |  1.85 ms |  5.65 ms |   6.63 ms |

以上均为 correctness manifest 通过后的 warm OS page-cache repository 数据，每项 30
样本；全部满足 backend P95 ≤300 ms，但不代表浏览器 cold/warm 已通过。

实时流原始数据：`benchmarks/chat-refactor-m7-working-tree-live.json`。冻结的 144,000
事件在 9.77 秒内完成，最终 replay 为 2,048 events / 638,976 bytes，subscriber 为 0；
Python traced retained delta 为 966,992 bytes。8 个并发 Chat 的 fast subscriber 顺序完整，
8 个 slow subscriber 均被隔离并要求 resync。该结果是事件量加速实验，不是两小时墙钟
soak。

两小时 wall-clock 原始数据：
`benchmarks/chat-refactor-m7-working-tree-live-2h.json`。实际运行 7,200.01 秒，完成
144,000 events 和 119 次断线重连；最终 replay 为 2,048 events / 647,168 bytes，
Python traced retained delta 971,752 bytes。8 个并发 Chat 的慢订阅者均被隔离，
correctness 通过。

Extreme 原始数据：`benchmarks/chat-refactor-m7-working-tree-extreme.json`。1 GiB、
50,000 messages 的完整 manifest 通过，turn tail 3 样本 P50 51.25 ms、P95/max
56.94 ms；进程最大 RSS 2.47 GB、peak footprint 3.82 GB、无 swap。读路径满足门槛，
但该峰值主要来自冻结数据构造/全量 materialize，不能作为迁移内存已优化的证明。

浏览器 legacy smoke 原始数据：`benchmarks/chat-refactor-m7-browser-smoke.json`。采集时
rollout status 为 `enabled=false, ui_enabled=false`。在当前 IAB、
1099×1224、无网络限速下，对一个现有小 Chat 做 30 次 warm reload，P50/P95 为
1,205/1,624 ms；在两个已有小 Chat 间做 30 次侧栏切换，外部 AX 观测口径 P50/P95 为
502/610 ms。该口径包含自动化快照开销，且不是冻结 Long 数据，故只作用户体感诊断，
不能作为 V2 数据，也不能勾选 Long warm/cold 门禁。页面快照为 3 个 message roots、1,263 个全部 DOM
elements；“全部 DOM elements”也不能替代 timeline roots 门槛。

冻结 Long V2 浏览器原始数据：
`benchmarks/chat-refactor-m7-working-tree-browser-v2-final.json`；代表性 trace 位于
`benchmarks/chat-refactor-m7-browser-v2-final-traces/`。在真实 Chrome/Console UI、
1440×1000、无 CPU/network throttling、冻结 route mock 下，各 30 个样本结果如下：

| 指标         |  Cold P95 |  Warm P95 |                Max |
| ------------ | --------: | --------: | -----------------: |
| 可读尾部     | 311.70 ms | 374.80 ms | 335.60 / 430.50 ms |
| 完整首屏     | 395.40 ms | 435.50 ms | 451.50 / 518.40 ms |
| React commit |  12.00 ms |  13.80 ms |   21.80 / 25.90 ms |
| Long task    |  66.00 ms | 134.00 ms |  98.00 / 152.00 ms |

该轮历史产物首屏为 3 个完整 turn；当前实现已统一为 5-turn 页并增加有界视口补页。
当时全部 DOM elements 最大 1,873/1,775。cold/warm、React 和
long task 门禁已通过。原产物使用全局 `.qwenpaw-bubble` 得到最大 9，但该选择器会把
用户卡片内部嵌套 Bubble 重复计数，故旧 `timeline_roots` 字段已废弃，不能证明顶层挂载
量。修正后的直接子选择器在真实 Chrome 定点样本中得到 6 个顶层 roots；MessageList
窗口完成后再以新 metric contract 重跑正式门禁。环境为
Chrome 146.0.7680.80、arm64 10 核、16 GiB、Vite development service；CPU/network
未限速。`pmset` 确认为 AC Power，psutil 原始字段为 `no-battery-detected`，两者均如实
保留。早期 4-turn、Locator waiter 和 trace 写盘污染的结果均保留为失败证据，没有
覆盖或选择性删除。

Long 30 分钟 retained-heap 原始数据：
`benchmarks/chat-refactor-m7-working-tree-browser-heap-long-30m.json`。强制 GC 后 used heap
从 71.23 MiB 增至 74.10 MiB（+4.03%），峰值 74.30 MiB；DOM nodes 3,430 → 3,417，
listeners 451 → 463。该产物的 `timeline_roots=9` 使用已废弃的全局选择器，不参与
挂载量裁决。修正前的 Locator waiter 会在每次切换
保留远程 DOM 引用，污染结果已保存为 `locator-contaminated`，不用于裁决。

Tool dense 30 分钟 retained-heap 原始数据：
`benchmarks/chat-refactor-m7-working-tree-browser-heap-tool-dense-30m.json`。冻结逻辑规模为
500 turns / 10,000 tool pairs / 256 MiB；该轮首屏加载 3 个完整 turn，当前实现为 5-turn
有界窗口。强制 GC 后 used heap
从 74.60 MiB 增至 77.48 MiB（+3.86%），峰值 77.71 MiB；DOM nodes
3,744 → 3,735，listeners 457 → 469。该产物的 `timeline_roots=9` 使用已废弃的全局
选择器，不参与挂载量裁决。一次导航尖峰为 945 ms，
下一样本恢复且未超过 1 秒。

最终扩展回归：Console Chat/SDK lifecycle/normalized history/pagination/live/Copy/lazy
block/queue/session conversion 的 11 个前端定点测试文件共 400 passed；
Console/Feishu/DingTalk/QQ
contracts 与 integration、V2 API/application/delete/live/fault/scroll/workspace、backup、
checkpoint、session hook 和 Scroll command 的 19 个后端测试文件共 195 passed、
1 skipped。关闭态兼容 smoke 的只读 rollout status 为
`enabled=false, ui_enabled=false, healthy=true`，且未触发真实 workspace 迁移或写入。
真实 `localhost:5173` Console 在关闭态可列出 5 个既有 Chat，并只读打开 legacy Chat；
标题、用户消息与 assistant 回复正常可见，浏览器日志仅有既有 React/Ant Design
deprecation warnings。

隔离 working directory + 8011/5177 的真实 V2 Console smoke 验证了 ready_v2、normalized
消息渲染和 healthy rollout。最终产品能力已从早期同 Chat branch UI 收敛为 response card
上的独立 Chat Copy：SDK conversion 保留 `chat_turn_id` 与 Chat Copy identity，Console
不显示 branch selector，公开 API 不提供 create/activate/delete branch。真实 UI 与 provider
continuation 已另行确认，隔离服务已停止，临时目录已移入废纸篓。当前显式验收开启态为
`enabled=true, ui_enabled=true, healthy=true`，且无 stop reason 或 warning。

## 7. 默认开启门禁结论

真实 provider-backed Console 的发送、stream、停止、主动刷新断线恢复、durable 历史与
独立 Chat Copy 均已通过。用户于 2026-10-23 明确渠道定点只要求 Console；Feishu 真机
结果仅作补充证据，DingTalk/QQ 不作为发布门禁。Extreme 已在 8 GiB/4 vCPU、无 swap 的
受控 Linux VM 完整执行并通过 correctness、P95、RSS、swap 与 OOM 门禁。用户已于
2026-10-23 明确确认默认开启，因此默认开启门禁全部完成。
