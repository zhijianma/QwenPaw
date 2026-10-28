# Chat 重构 M0 基线报告

> 日期：2026-10-22
> 基线提交：`27d22cd18`
> 状态：进行中；自动化与数据链路已建立，人工交互和浏览器渲染基线待补

## 1. 测量环境

| 项目 | 值 |
|---|---|
| OS | macOS 26.3.2（Darwin 25.3.0，arm64） |
| CPU | Apple M4 |
| 内存 | 16 GiB |
| Python | 3.12.0，`qwenpaw` Conda 环境 |
| Node.js | v24.22.1 |
| 仓库 | `main@27d22cd18`，存在本次未提交文档/测试资产 |

所有正式数值均保存原始样本，不把静态推演表述为执行通过。当前基准使用本机 warm OS
page cache；浏览器冷打开、网络、React commit、DOM、Long Task 和 heap soak 尚未包含，
不能用本报告中的纯转换耗时替代 M3/M7 用户体验门禁。

## 2. Correctness Oracle 与固定数据集

`src/qwenpaw/app/chats/integrity.py` 提供流式 transcript manifest：

- 规范消息和逐消息 SHA-256；
- 顺序敏感的长度前缀 transcript SHA-256；
- role/type/block 数量；
- tool call/result 的位置、名称、孤儿、重复和错配诊断；
- 排除 legacy history 每次读取新生成的展示 Message ID，但保留
  `metadata.original_id`。

测试已证明 oracle 能发现消息缺失、重复、乱序、block 内容变化、tool orphan、重复和名称
错配，并且只迭代输入一次，不保留完整 payload。

固定生成规格：

| Profile | 固定规模 | 当前状态 |
|---|---:|---|
| Typical | 100 turns / 500 messages/blocks / 5 MiB payload | 已实际生成验证 |
| Long | 2,000 turns / 10,000 messages/blocks / 100 MiB payload | 规格与基准已验证 |
| Extreme | 10,000 turns / 50,000 messages/blocks / 1 GiB payload | 规格冻结，未在本轮落盘 |
| Tool dense | 500 turns / 10,000 tool pairs / 256 MiB result payload | 规格冻结，生成器单测通过 |
| Huge block | 单 block / 64 MiB | 规格冻结，生成器单测通过 |

Branch、Long run 和 Concurrent 属于带状态/事件的数据集，已分别冻结为 2,000-turn trunk +
20 branches × 200-turn suffix、2 小时 × 20 合批事件/秒（144,000 基础事件，30 秒一次
tool）以及 8 active Chats × 2 subscribers。实际数据只能在对应 DAG/LiveHub harness 建成后
生成，不能用静态 transcript 伪装运行结果。

Typical CLI 实际结果：500 messages、500 blocks、5,332,830 serialized bytes，manifest hash：

```text
2ad7c3f61715dc5701dc70aceef3b844059fe40655e79a984f4c77fa227f6d07
```

## 3. 当前功能回归基线

### 3.1 后端定点测试

执行范围：

- Chat history conversion、Console task、archive；
- Checkpoint router/restore；
- Console reconnect/stop；
- Scroll manager。

结果：`174 passed, 1 skipped, 1 warning in 58.59s`。warning 是 FastAPI TestClient 的
Starlette/httpx deprecation，不是本轮新增失败。

### 3.2 前端定点测试

执行：

- `testLargeSession.test.ts`；
- `sdkSessionLifecycle.integration.test.tsx`。

结果：`2 test files passed, 60 tests passed in 10.25s`。

### 3.3 新增 M0 资产测试

manifest、数据集和关联 Chat block conversion 合计执行结果：

- manifest/dataset：`13 passed`；
- manifest + 既有 block conversion：`104 passed`；
- 新增 Python 文件定点 pre-commit：AST、mypy、black、flake8、pylint 全部通过；
- 新增前端基准：Prettier、ESLint 和 1-sample smoke 通过。

### 3.4 入口能力基线

现有实现不是多套完全独立的 Chat：外部渠道最终经 `BaseChannel._process(request)` 进入
workspace runtime，但各渠道负责自己的 `session_id` 和输入/输出适配；Harness 通过
`HarnessSessionBridge` 另行保存 session JSON。M2 应替换共享持久化边界，不能重写各渠道
协议实现。

| 入口 | 当前身份/写入边界 | Branch 基线 | M2 必须保持 |
|---|---|---:|---|
| Console + QwenPaw | ChatSpec → ConsoleChannel → runtime/session hook | 无 | task/queue/stop/tool/approval/attachment/usage |
| Console + Harness | ChatSpec → HarnessRuntime → HarnessSessionBridge | 无 | provider history hydrate、reasoning/tool stream、附件 |
| Feishu | 渠道解析 chat/sender → BaseChannel → runtime | 无 | 群聊/私聊身份、卡片和媒体行为 |
| DingTalk | conversation/sender → BaseChannel → runtime | 无 | stream/card、媒体、mention/access control |
| QQ | c2c/group session → BaseChannel → runtime | 无 | session resume、媒体和发送目标 |
| 其他 BaseChannel 渠道 | 各自 alias → BaseChannel → runtime | 无 | 不因共享 acceptance 改造而绕过原 adapter |

已定点执行 Harness、Console、Feishu、DingTalk 和 QQ 测试：`598 passed, 28 warnings in
35.15s`。warning 包含现有 DingTalk asyncio marker 错配和两个未 awaited coroutine；本轮
未把 warning 误报为通过项，也未在基线任务中修改无关渠道测试。

## 4. 旧链路性能基线

### 4.1 后端完整 session 读取链路

测量边界：读取并解析完整 session JSON → `AgentState.model_validate` →
`agentscope_msg_to_message`。每个 profile 预热一次，正式 30 次。

| Profile | Serialized | P50 | P95 | Max |
|---|---:|---:|---:|---:|
| Typical | 5,416,805 B | 92.58ms | 105.63ms | 108.93ms |
| Long | 108,312,522 B | 2,010.55ms | 2,195.83ms | 2,222.26ms |

原始数据：
[backend baseline](benchmarks/chat-refactor-main-27d22cd18-backend.json)。

### 4.2 前端完整历史纯转换

测量边界：内存中的完整 JSON string → `JSON.parse` → `convertMessages`。每个 profile
预热三次，正式 30 次；不包含 fetch 和 React render。

| Profile | Serialized | P50 | P95 | Max |
|---|---:|---:|---:|---:|
| Typical | 5,307,831 B | 2.55ms | 9.88ms | 10.50ms |
| Long | 106,142,051 B | 70.36ms | 106.79ms | 125.76ms |

原始数据：
[frontend baseline](benchmarks/chat-refactor-main-27d22cd18-frontend.json)。

### 4.3 当前结论

事实：Long 的旧后端完整读取 P95 已超过 2 秒，且成本随完整 session 大小增长；前端纯
parse/convert 明显更小，但该测量尚未包含网络传输、SDK 全量缓存、React/Markdown/tool
card 渲染和 DOM 成本。

因此后续优化不能只改 `convertMessages`：必须通过后端分页和有界前端同时消除完整 session
读取、传输、校验和渲染。该判断由本地合成基准支持，不代表真实用户分布已采集。

## 5. M0 剩余门禁

- 完成人工交互矩阵并记录预期/实际；
- 建立浏览器级冷/热打开、DOM、React commit、Long Task 和 heap 原始采集；
- 建立 Long run/Concurrent 事件 harness；
- 对固定数据生成和基准命令补 runbook；
- 全部完成后才允许把 M0 标记完成并进入 M1 schema 实施。
