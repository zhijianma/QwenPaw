# Chat 重构基准 Runbook

> 适用阶段：M0–M7
> 原则：先校验 correctness manifest，再采集性能；禁止通过少加载或截断提高指标

## 1. 环境记录

每次正式采集必须记录：

- Git commit 和未提交变更；
- OS、CPU、内存、Python、Node、浏览器版本；
- viewport、CPU/network throttling；
- cold/warm 定义、预热次数和 cache 清理方法；
- profile、payload、样本数、失败数和并发度。

同一报告中的 before/after 必须使用同一台机器、相同电源状态和相同设置。运行期间不并行
执行全量测试、构建、索引或其他高负载任务。

## 2. 生成固定 Transcript 数据

在仓库根目录使用 `qwenpaw` Conda 环境：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/generate_datasets.py typical long \
  --output-dir /tmp/qwenpaw-chat-refactor-datasets
```

大数据按需单独生成，避免误占工作区：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/generate_datasets.py extreme \
  --output-dir /tmp/qwenpaw-chat-refactor-extreme
```

默认拒绝覆盖；只有确认目标目录是可丢弃基准数据时才加 `--overwrite`。生成后检查
`*.manifest.json` 的 message/block 数、tool diagnostics 和 transcript hash。基准失败时保留
manifest 和日志，正文数据仍放 `/tmp`，不要提交 GiB 级 JSONL。

## 3. 旧后端完整读取基线

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/benchmark_legacy_backend.py typical long \
  --samples 30 \
  --git-commit "$(git rev-parse --short=10 HEAD)" \
  --output /tmp/chat-refactor-backend.json
```

边界是完整 session JSON 读取/解析、`AgentState` 校验和 Chat history 转换；不含 HTTP、网络
或前端。输出为全部原始样本及 P50/P95/max，不能只复制最好结果。

## 4. 旧前端完整转换基线

在 `console/` 目录执行单文件 Vitest：

```bash
CHAT_REFACTOR_BASELINE=1 \
CHAT_REFACTOR_BASELINE_SAMPLES=30 \
CHAT_REFACTOR_BASELINE_OUT=/tmp/chat-refactor-frontend.json \
CHAT_REFACTOR_GIT_COMMIT="$(git rev-parse --short=10 HEAD)" \
node --max-old-space-size=4096 ./node_modules/vitest/vitest.mjs run \
  src/pages/Chat/tests/chatRefactorBaseline.test.ts
```

边界是完整 JSON string 的 `JSON.parse` 和 `convertMessages`；不含 fetch、React、Markdown、
SDK card、DOM 和浏览器 painting。该数字只能和相同边界比较。

## 4.1 V2 正文库完整读取

M2 在分页 API 实施前使用相同 logical transcript 验证 normalized source 的完整读取成本：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/benchmark_v2_backend.py typical long \
  --samples 30 \
  --git-commit "$(git rev-parse --short=10 HEAD)+working-tree" \
  --output /tmp/chat-refactor-v2-backend.json
```

脚本先 materialize 正文库并比较完整 correctness manifest，失败时不采纳性能结果；随后
预热一次并保留全部 30 个 raw sample。SQLite database bytes 与旧 JSON serialized bytes
不是同一物理口径，只比较相同 logical transcript 的耗时。

## 4.2 V2 分页读取

M3 以后使用 byte-bounded 页面脚本验收真实读路径。脚本先完整 materialize 并校验
correctness manifest，再预热一次 OS page cache，最后保留每项 30 个原始样本：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/benchmark_v2_pages.py \
  typical long tool_dense huge_block \
  --samples 30 \
  --git-commit "$(git rev-parse --short=10 HEAD)+working-tree" \
  --output docs/architecture/benchmarks/chat-refactor-m7-pages.json
```

该脚本测量 SQLite repository 的 turn tail page；Huge block 另测一个 byte fragment。
它不包含 HTTP、网络、React、Markdown 或浏览器 paint，因此不能用来宣称浏览器 cold/warm
门槛已通过。

## 5. Correctness 与定点回归

新增 M0 资产：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python -m pytest -q \
  tests/unit/app/chats/test_integrity.py \
  tests/unit/app/chats/test_refactor_datasets.py
```

Chat/Scroll/Checkpoint 和入口回归命令以
`docs/architecture/chat-refactor-m0-baseline.md` 记录的文件集合为准。禁止把这组定点测试
描述为全量测试。

## 6. 新链路验收顺序

每个 profile 必须按以下顺序：

1. 校验 manifest 数量、顺序、hash、tool link、usage 和 branch lineage；
2. 校验输出没有截断、隐藏或错误的 unloaded 状态；
3. 执行至少一次 correctness 失败注入，确认 oracle 能发现问题；
4. 再执行冷/热正式样本；
5. 保存 raw JSON、失败日志和聚合报告；
6. 和相同 commit/环境的 baseline 比较。

任何 correctness 失败都会使对应性能样本无效。Extreme、Long run 和 soak 可以少于 30
次，但必须报告持续时间、峰值、buffer/queue/connection/DOM/heap 上限和失败数。

## 7. Long run 与并发实时流

先运行加速事件量检查，覆盖冻结的两小时 20 events/s（144,000 events）和 8 个并发
Chat、每 Chat 2 个 subscriber：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/benchmark_v2_live.py \
  --git-commit "$(git rev-parse --short=10 HEAD)+working-tree" \
  --output docs/architecture/benchmarks/chat-refactor-m7-live.json
```

该命令验证事件序列、replay 的 count/bytes 上限、subscriber 隔离，并记录 Python traced
heap。它是事件量加速实验，不等价于两小时 wall-clock soak，也不能替代 30 分钟浏览器
retained heap、DOM、React commit 和 main-thread long-task 采样。

两小时 wall-clock 候选使用相同冻结事件率，并每 60 秒断开/续接一次 fast subscriber：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/benchmark_v2_live.py \
  --wall-clock-seconds 7200 \
  --git-commit "$(git rev-parse --short=10 HEAD)+working-tree" \
  --output docs/architecture/benchmarks/chat-refactor-m7-live-2h.json
```

必须等待输出 `correctness=passed` 和最终 JSON，启动进程或中途进度不能算通过。

发布候选还必须在真实 Console 运行以下浏览器采集，cold/warm 各 30 次：

1. 固定浏览器版本、viewport、电源和 CPU/network throttling；
2. cold 样本每次使用全新页面上下文并清缓存，warm 样本先完成一次打开；
3. 从导航开始，到尾页文本和 composer 可交互为止；
4. 记录 Performance trace、React Profiler commit、默认 timeline root 数；
5. Long、Tool dense 各持续 30 分钟读取 retained heap；
6. Long run 保持真实连接两小时，记录 reconnect、buffer、queue、DOM、heap 峰值。

没有保存原始 trace 和环境元数据时，不得勾选对应浏览器门禁。

Long 的冻结浏览器采集命令：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/benchmark_v2_browser.py \
  --samples 30 \
  --git-commit "$(git rev-parse --short=10 HEAD)+working-tree" \
  --output docs/architecture/benchmarks/chat-refactor-m7-browser-v2.json \
  --trace-directory docs/architecture/benchmarks/chat-refactor-m7-browser-v2-traces
```

脚本使用真实 Chrome 和 Console UI，但通过进程内 route mock 固定 V2 API 数据，排除网络
抖动。cold 每个样本使用新 browser context；warm 在同一页面中对两个已预热 Chat 交替
切换。该轮历史产物使用首屏 3 turn/旧页 30 turn；当前实现已统一为每页 5 个完整 turn，
并在大视口内容不足时串行自动补页。计时边界为 SPA 导航
到尾部文本和 composer 可交互；cold/warm trace 分别在独立的非计时 browser context
采集，禁止让 trace 停止/写盘污染任何正式计时样本。

2026-10-23 最终 5-turn 实现的 cold/warm 各 30 样本产物为
`chat-refactor-m7-working-tree-browser-v2-five-turn.json`，trace 位于
`chat-refactor-m7-browser-v2-five-turn-traces/`。结果：cold navigation P95
359.5 ms、warm navigation P95 412.8 ms，React commit P95 分别为 12.1/17.4 ms，
最大 long task 162 ms，cold/warm 最大 timeline roots 均为 10；correctness、HTTP、
page error 与 console error 门禁全部通过。

浏览器 retained-heap 串行采集，不能与测试、构建或另一个浏览器基准并行：

```bash
/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/soak_v2_browser_heap.py \
  --scenario long \
  --duration-seconds 1800 \
  --sample-interval-seconds 30 \
  --git-commit "$(git rev-parse --short=10 HEAD)+working-tree" \
  --output docs/architecture/benchmarks/chat-refactor-m7-heap-long.json

/Users/mazhijian/miniconda3/envs/qwenpaw/bin/python \
  scripts/chat_refactor/soak_v2_browser_heap.py \
  --scenario tool-dense \
  --duration-seconds 1800 \
  --sample-interval-seconds 30 \
  --git-commit "$(git rev-parse --short=10 HEAD)+working-tree" \
  --output docs/architecture/benchmarks/chat-refactor-m7-heap-tool-dense.json
```

每次采样先调用 Chromium GC，再记录 used/total heap、DOM nodes、documents、event
listeners、timeline roots 和导航耗时。最终增长率以强制 GC 后的首尾 used heap 计算。
`timeline_roots` 必须使用 `.qwenpaw-bubble-list > .qwenpaw-bubble`，只统计 BubbleList
的直接 message 子节点；全局 `.qwenpaw-bubble` 会重复计入用户卡片内部嵌套节点，禁止
用于挂载量门禁。产物必须写入 `metric_contract.timeline_roots` 以便审计口径。
等待条件只能返回布尔值，并显式 dispose Playwright `JSHandle`；不要用长期循环中的
Locator waiter。后者在本机实验证实会让每次切换额外保留 147 DOM nodes 和约 14 个
listeners，形成 harness 自身的假泄漏。修正前结果必须标记 `locator-contaminated`，不得
用于 DOM/listener 稳态裁决。
