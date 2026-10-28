# Chat 重构 M7 最终静态评审 — 2026-10-23

## Overview

本轮评审范围是当前 working tree 的灰度观测、UI kill switch、不可逆 cutover 防护、
三级分页、实时恢复、独立 Chat Copy、Checkpoint/备份/删除、性能门禁和实验 schema
收口；同时复核 Console 的 503 fallback 是否会放大过载。

## Strengths

- storage 路由与 UI 暴露拆成两个开关，UI 回滚不改变 normalized 数据事实源；
- 已迁移 workspace 在误关 storage V2 时 fail closed，startup Scroll backfill 和
  Chat repository 两个入口都有防护；
- rollout status 只读取 control plane，并把 quarantine、terminal migration failure、
  recall failure 和 running repair 作为停止条件；
- page/live benchmark 先通过 correctness，再保留全部原始样本；
- Long 浏览器 cold/warm 各 30 样本已通过时延、React commit、long task 和 DOM
  门禁，且保留 raw JSON 与代表性 trace；
- Long 30 分钟 GC 后 retained heap 增长 4.03%，DOM/listener/root 稳定；基准自身的
  Locator 句柄假泄漏已通过 A/B 定位并从正式 harness 移除；
- Tool dense 30 分钟 GC 后 retained heap 增长 3.86%，DOM/listener/root 稳定；
- 两小时 wall-clock live/reconnect soak 完成 144,000 events 和 119 次重连，
  correctness、replay 上限、subscriber 隔离和 Python retained heap 均通过；
- live hub 的资源观测返回 immutable counters，没有暴露内部可变队列。
- 最终扩展回归覆盖 11 个前端定点测试文件（400 passed）和 19 个后端定点测试文件
  （195 passed，1 skipped）；默认开启变更的 16 个直接影响测试文件又完成
  `381 passed, 1 skipped`。
- 真实 Console 关闭态只读 smoke 可列出既有 Chat 并正确显示所选 legacy Chat 的标题与
  双方消息；未发现请求/渲染异常，只有既有依赖 deprecation warnings。
- 最终数据模型只保留 Conversation/Turn/Message/Block 和独立 Chat Copy；生产代码、API、
  cursor、live、catalog 与 recall 均不含 Chat 内 branch identity 或兼容 adapter。
- 开发机两个 workspace 的 10 个现行 SQLite 数据库均为当前 schema、`quick_check=ok`，
  全库契约扫描未发现 branch 表或字段；实验 DB、孤儿 Copy DB 和迁移 sidecar 已移出
  运行路径。
- 最终存储定点回归 `85 passed`；6 个现有 Chat 的 view/5-turn 分页均为 200，17-turn
  Chat 的相邻两页各 5 turns、无重叠，rollout 为 `healthy=true`。
- Storage V2 无环境变量时默认开启，显式 `QWENPAW_CHAT_STORAGE_V2=0` 仍可验证未 cutover
  workspace 的 legacy 降级；当前服务为 `enabled=true, ui_enabled=true, healthy=true`，
  无 stop reason 或 warning。

## Issues

### Critical

无。

### Important

无遗留代码问题。评审中发现并修复：

1. storage flag 在 `ready_v2` 后仍可关闭并回到 legacy 写；现以 fail-closed guard 阻止；
2. Console 将任意 503 降级为全量 transcript 读取；现只允许两个 capability code；
3. recall projection 的 `failed` 状态只表现为 lag warning；现升级为 hard stop；
4. normalized `chat_turn_id` 在 SDK response card 转换中丢失，真实 Copy 点击无请求；
   现保留 turn 与 Chat Copy source identity，并用真实 V2 UI 复验；
5. 早期同 Chat branch selector 实现已被最终独立 Chat Copy 语义整体取代；Console 不再
   暴露 selector，公开 API 也不再提供 create/activate/delete branch。

### Minor

无。

## Design Questions

- pending localStorage 的边界按“尚未观察到 durable receipt”定义，而不是按 HTTP
  响应头定义。V2 的 user message 在 acceptance 事务内持久化，分页读到对应 client ID
  后立即清除；HTTP 响应头可能先于 producer 提交，不能安全地作为清理信号。

Extreme 另在 8 GiB/4 vCPU、无 swap 的受控 Linux VM 完整通过 correctness、P95、RSS、
swap 与 OOM 门禁；因此低内存容量不再是未决设计问题。

## Suggestions

- 后续将 rollout status 接入现有运维面板，并为 warning 持续 5 分钟增加外部告警；
- 若生产低内存设备覆盖面较大，为 legacy session JSON 引入流式解析/物化，而不是提高
  进程内存上限。

## Unverified Hypotheses

无。用户于 2026-10-23 明确渠道定点只要求 Console；真实 Console provider 的发送、
stream、停止、主动刷新断线恢复、durable 历史与独立 Chat Copy 均已执行。Feishu 真机
主链路只作补充证据；DingTalk/QQ 不属于本次发布矩阵，未完成的测试不计为通过。

## Synchronous I/O & Event Loop

rollout endpoint 通过 worker thread 读取 SQLite。页面读仍使用 byte-bounded API；普通 503
不再触发全量 transcript fallback。该评审完成后，MessageList 窗口实现已将初始页和
向上翻页统一调整为 5 个完整 turn；8 KiB 以上无富文本语义的已完成 assistant 正文走纯文本快路径。冻结 Long
浏览器样本最大 long task 152 ms，未发现 >1 秒主线程阻塞。

## Cross-Platform Compatibility

| Platform | Status | Notes                                            |
| -------- | ------ | ------------------------------------------------ |
| macOS    | ✅     | 定点测试、真实 Console/Feishu、基准和 lint 实际执行 |
| Linux    | ✅     | 8 GiB/4 vCPU 受控 VM 完整执行 Extreme 数据集       |
| Windows  | ⚠️     | 未新增 shell/path 假设；仅静态推演，未实际运行      |

## 裁决：APPROVE

当前 working tree 未发现通过证据门禁的 Critical、Important 或 Minor 代码问题；M3–M7
实现、自动化性能与稳定性门禁、真实 Console 和 Feishu 主链路均通过。该裁决只表示代码
评审与既定渠道矩阵通过。用户已于 2026-10-23 确认正式默认开启；授权后的版本提交仍是
独立发布 gate，Feishu 结果只作补充证据。
