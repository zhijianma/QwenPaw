# QwenPaw Agent OS 融合架构与模块组织

状态：Evidence-backed baseline

日期：2026-09-21

适用分支：`feat/lite-agent-os`

## 1. 目标与事实边界

本文把 OS 3.0 能力域、QwenPaw 现有系统、Lite / Workstation / Hub
产品形态和插件化方向统一到一套模块组织中。架构遵循四个约束：

1. 稳定 Kernel 只保存跨产品不变的领域契约。
2. 现有 Agent、Console、Chat、安全和渠道能力通过适配器复用。
3. 可替换能力通过 Contribution 和 Slot 扩展。
4. 产品差异由 Profile 和部署适配器表达，不复制领域代码。

### 1.1 3.0 产品定位与 OS 边界

结合 `aliyun/ai-agent-handbook` 对 Harness 与 Agentic OS 的责任划分，
QwenPaw 3.0 的准确定位是：

> **产品化 Harness + Agentic OS Substrate**。

产品化 Harness 决定目标如何推进：Agent Contract、Context Policy、Plan、
Prepare / Model / Act / Observe / Verify Loop、能力选择和完成验证。Agentic OS
Substrate 管理多个 Harness 都需要的公共运行对象与强制边界：Agent Identity、
Run、Conversation、Workspace、Capability、Budget Lease、Policy、Checkpoint、
Artifact 和 Evidence。

这一区分是逻辑责任边界，不要求现在移动大量文件。现有 `kernel/` 继续保存稳定
公共契约，但每个类型和 Port 必须能够归类为 Harness Contract 或 OS Contract。
任务目标、计划内容、业务终止条件、Verifier 规则和 Context 策展策略由应用 /
Harness 定义；OS 可以持久化和执行这些契约，但不决定其业务内容。

能力是否下沉到 OS，使用三条门禁：

1. 多个应用重复实现，且实现差异不产生业务价值；
2. 必须由被约束方之外的组件执行才真正有效；
3. 需要独立于执行方的证据来源才能验证。

详细逐项分析见
`docs/analysis/qwenpaw-ai-agent-handbook-absorption-2026-10-01.md`。

文中的状态含义：

| 状态 | 含义 |
|---|---|
| 已实现 | 当前分支已有源码和定点测试证据 |
| 已有能力待适配 | 主干能力存在，但尚未完全接入统一 Task/Contribution 契约 |
| 契约已定义 | API 或领域边界已确定，实现仍不完整 |
| 后续能力 | Lite 基线不实现，属于 Workstation 或 Hub 路线 |

## 2. 统一分层

```text
┌───────────────────────────────────────────────────────────────┐
│ Experience                                                   │
│ Console / Task Workbench / Chat / Channels / CLI / Hub Admin │
├───────────────────────────────────────────────────────────────┤
│ Application                                                  │
│ TaskService / ChatManager / CronManager / Approval adapters  │
│ Plugin lifecycle / Workspace assembly / Compatibility APIs   │
├───────────────────────────────────────────────────────────────┤
│ Runtime                                                      │
│ AgentScope / Harness / Tool Guard / Sandbox / MCP / Channels │
│ Local runner / Hub runtime provisioner / event live delivery │
├───────────────────────────────────────────────────────────────┤
│ Stable Kernel                                                │
│ Task / Plan / Run / Event / Approval / Artifact / Evidence   │
│ Capability / Contribution / state machines / ports           │
├───────────────────────────────────────────────────────────────┤
│ Infrastructure                                               │
│ SQLite / filesystem / object store / process / container     │
└───────────────────────────────────────────────────────────────┘
```

OS 3.0 的 P1–P6 是能力域，不是强制调用层级：

逻辑责任叠加如下，不能仅凭物理目录判断归属：

```text
Experience / Channel
        |
Productized Harness
  Agent Contract / Context Compiler / Plan / Loop / Verifier
        |
Agentic OS Substrate
  Run / Conversation / Workspace / Capability / Policy / Budget Lease
  Interaction / Queue / Checkpoint / Artifact / Evidence
        |
Runtime / Sandbox / Storage adapters
```

其中 Context Compiler、统一 Action Plane、Environment Contract、语义观测、
Model Call Plane 和可派生的 Budget Lease 是 Handbook 比对后确认的增量基础设施，
不由 Task Workbench 私有实现。

| 能力域 | 稳定契约 | 当前主要实现 |
|---|---|---|
| P1 Capability & Execution | `TaskRunner`、`CapabilityDescriptor`、`RunnerSignal` | `tasks/runner.py`、Agent Runtime、Harness |
| P2 Scheduling & Contracts | `Task`、`Plan`、`Run`、`ExecutionEvent` | `TaskService`、Ledger、Cron adapter |
| P3 Agent Runtime | `TaskOrder`、Runner port | Console Agent、Modes、Harness adapters |
| P4 Memory & Semantics | `memory.provider` invocation port | 现有 memory 已通过系统 Provider 适配 |
| P5 Cognition | `Proposal`，禁止直接执行 | proactive responder 已接 Proposal gate |
| P6 Evolution & Governance | Contribution、generation、approval policy | Plugin loader、Tool Guard、Sandbox、Hub policy |

## 3. 当前能力到目标模块的映射

| 当前能力 | 源码证据 | 目标归属 | 决策 | 当前状态 |
|---|---|---|---|---|
| Agent 请求与流式执行 | `app/channels/console/channel.py` | Runtime adapter | 保留，通过 `TaskRunner` 投影事件 | 已实现基础闭环 |
| Chat 与会话目录 | `app/chats/manager.py`、`app/chats/api.py` | Application | 保留，Task 只引用会话，不重写历史 | 已有能力待适配 |
| 消息对 Conversation Fork | `kernel/conversations.py`、`conversations/lite.py` | Kernel port + Application adapter | 以 `ChatSpec.id` 和已完成 Turn 边界创建隔离子会话；不隐式复制执行环境 | Lite 已实现并完成父子运行态隔离验收 |
| Console 后台任务 | `app/routers/console.py` | Compatibility API | 保留响应形状，生命周期写入 Task Ledger | 已实现兼容桥 |
| Durable Task | `kernel/*`、`tasks/*`、`app/routers/tasks.py` | Kernel + Application | 新稳定主干 | 已实现 Lite 基线 |
| Tool Guard | `security/tool_guard/*` | Runtime policy adapter | 保留，禁止 Kernel 依赖具体实现 | Agent Profile 策略与 Task 审批桥已实现 |
| Environment / Sandbox | `kernel/models.py`、`runtime/environments.py`、`sandbox/*` | Kernel contract + Edition adapter | Invocation 前解析；Edition 只能承诺可证明的约束 | Lite Chat 已接本地证据与失败关闭；隔离型 Adapter 待接 |
| Approval | `app/approvals/*` | Application adapter | 活跃等待由旧服务承担，决定写入 Ledger | 双向决策与 fail-closed bridge 已实现 |
| Checkpoint | `checkpoints/*`、`tasks/replay.py` | Infrastructure adapter | 保留旧快照，实现安全恢复契约 | Task 安全 checkpoint 已实现；运行时快照待接 |
| Memory | `agents/memory/*`、`memory/*` | `memory.provider` Contribution | 核心只定义 port/session，后端外迁 | 已接入 Chat 固定 generation |
| Proactive cognition | `agents/memory/proactive/*` | `sensor` + Proposal producer | 只能产生 Proposal，审批后进入 TaskOrder | 已实现门禁路径 |
| Cron / Heartbeat | `app/crons/*` | Scheduler adapter | 保留调度，触发 `TaskSource.SCHEDULE` | 已有能力待接 Task |
| Channels | `app/channels/*` | scoped Contribution | 复用 BaseChannel，按 workspace scoped reload | 已有插件能力 |
| MCP / Drivers | `app/mcp/*`、`drivers/*` | `driver.provider` / scoped runtime | 保留策略与凭据边界 | 系统 Driver 已接入固定 generation；原生插件治理契约待冻结 |
| Modes | `modes/*` | `agent.mode.provider` + 细分 Contributions | 与 Task `strategy` 分离 | 生命周期已适配，内部贡献待拆分 |
| Commands | `runtime/slash_command_registry.py` | `command.provider` | 系统保留名优先，普通冲突显式报错 | 固定 Catalog、三态分发与 Skill fallback 已接入 |
| Lifecycle Hooks | `runtime/hooks.py`、`hooks/*` | `hook.provider` | 八阶段、依赖图、短路与清理语义保留 | 固定 Catalog 与跨 Provider Router 已接入 |
| ReAct Stop Gates | `loop/stop.py`、Mode stop handlers | `loop.gate.provider` | 只控制推理循环，不承担请求取消 | 固定 Catalog、scope 选择与延迟停止已接入 |
| Codex/Qoder Harness | `harnesses/*` | `runner` Contribution | 复用统一事件模型，不成为独立 Task 系统 | 已有能力待接 TaskRunner |
| Plugin v1 | `plugins/registry.py`、`plugins/api.py` | legacy adapter | 继续兼容 `type` 和单入口 | 已实现兼容 |
| Plugin v2 Contributions | `plugins/contributions.py`、`generations.py` | Extension runtime | 多 Slot、原子 generation、lease、回滚 | 已实现基础机制 |
| Frontend Menu/Route/Slot | `console/src/plugins/registry/*` | Experience extension host | 保留，增加 contextual Task slots | 已实现，公开 Host 类型与六 Contribution 示例 |
| Artifact / Evidence | `tasks/artifacts.py`、`renderers.py`、Task API、Tasks UI | Kernel ref + renderer Contribution | 大内容外置，Ledger 只存引用 | 固定 generation、安全预览、原始下载与 Evidence 已接入 |
| Workspace 文件与配置 | `app/routers/workspace.py` | Application / infrastructure | 保留，Task 显式绑定项目目录 | 已实现 Task 目录绑定 |
| Hub runtime control | `hub/*` | Hub deployment adapter | 不下沉 Kernel，复用相同任务契约 | 已有独立能力，尚未接统一 Profile |

## 4. 目录与依赖规则

### 4.1 目标目录

```text
src/qwenpaw/
├── kernel/                 # 稳定领域模型、事件、状态机、ports
├── tasks/                  # Task application service 与 Lite adapters
├── editions/               # 组合声明，不包含领域逻辑
├── plugins/
│   ├── sdk/                # 二次开发者唯一稳定导入面
│   ├── contributions.py    # Slot 校验
│   └── generations.py      # 原子激活、租约和回收
├── app/                    # HTTP、Console、Chat、Workspace 装配
├── agents/                 # AgentScope runtime adapter
├── harnesses/              # Codex/Qoder 等 runner adapters
├── security/               # Tool Guard policy implementation
├── sandbox/                # 执行隔离实现
├── modes/                  # 可替换运行策略
├── memory/                 # memory provider implementations
└── hub/                    # 多租户控制面与 runtime provisioners

console/src/
├── api/modules/tasks.ts
├── pages/Tasks/
└── plugins/registry/       # Menu / Route / Slot host
```

### 4.2 强制依赖方向

```text
Experience -> Application -> Kernel
Runtime    -> Kernel
Adapters   -> Kernel ports
Kernel     -X-> FastAPI / AgentScope / editions / plugins / UI / database
```

额外规则：

- `kernel` 不读取环境变量，也不决定 Lite / Workstation / Hub。
- `editions` 只能组合端口实现和开关，不复制状态机。
- 插件只能依赖 `qwenpaw.plugins.sdk` 和公开 Kernel 类型。
- HTTP DTO 可以适配领域模型，但不能成为领域真相源。
- UI Slot 不能直接访问数据库或 Runtime 单例。
- 跨模块协作通过 port、事件或显式 application service 完成。

## 5. 公共契约

当前稳定领域类型位于 `src/qwenpaw/kernel/models.py` 与
`src/qwenpaw/kernel/conversations.py`：

- `Task`、`Plan`、`PlanStep`、`Run`、`TaskOrder`
- `ExecutionCheckpoint`、`RunnerSignal`
- `ApprovalRequest`、`ApprovalDecision`、`Proposal`
- `ArtifactRef`、`EvidenceRef`
- `CapabilityDescriptor`、`PluginContribution`
- `ConversationForkCommand`、`ConversationForkOrigin`、
  `ConversationForkResult`

端口位于 `src/qwenpaw/kernel/ports.py`：

- `TaskStore`、`ExecutionLedger`、`TaskRunner`
- `ApprovalPort`、`ArtifactStore`、`CheckpointStore`
- `CapabilityResolver`、`CapabilityLease`、`EventPublisher`
- `ConversationForkPort`

所有公共领域模型继承 `KernelModel`，序列化时携带
`schema: qwenpaw.kernel-model.v1`；旧数据缺少该字段时使用默认版本加载。
事件 envelope 使用更具体的 `qwenpaw.execution-event.v1`。事件必须满足：

- `(task_id, sequence)` 单调且唯一；
- `event_id` 重试幂等；
- 领域投影和事件在同一事务提交；
- 不保存隐藏推理、原始凭据或无限 payload；
- 大内容通过 `ArtifactRef` 引用；
- 插件事件使用 `plugin.<plugin_id>.*` 命名空间。

HTTP 稳定入口为 `/api/tasks`，错误使用
`application/problem+json` 和稳定错误码。`/console/chat/task` 是兼容层，
不是第二套任务领域模型。

## 6. Lite / Workstation / Hub 组合矩阵

| 维度 | Lite | Workstation | Hub |
|---|---|---|---|
| 核心用户 | 单用户、个人设备 | 专业个人/小团队工作站 | 多租户团队和组织 |
| 当前实现状态 | 可运行 deployment adapter | Profile 与 adapter requirements 已定义，runtime 未实现 | Profile、控制面与 adapter requirements 已定义，尚未接共享 ports |
| Kernel | 共享 | 共享 | 共享 |
| tenant mode | `single_user` | `single_user` 或本机多身份 | `multi_user` |
| runner | 单本地 runner | 本地 runner pool + Harness | distributed / isolated runtime |
| Ledger | SQLite WAL | SQLite WAL，可选外部 DB adapter | 共享数据库 adapter |
| Artifact store | 项目 filesystem | workspace filesystem / NAS adapter | object store |
| Approval | strict | policy + interactive | policy + RBAC / audit |
| Scheduler | 进程内、有限 | durable local scheduler | distributed scheduler |
| Remote runner | 关闭 | 可选 | 开启 |
| Harness | 可作为插件启用 | Codex/Qoder 为重点 runner | 远端 runtime 内运行 |
| Channels | 本地 scoped | 多 workspace scoped | tenant scoped |
| Plugin default | hot | hot，部分 scoped | signed/policy controlled |
| Runtime isolation | 本地 Sandbox | process/container 可选 | process/container 强隔离 |
| Conversation Fork | 消息对上下文分支，共享当前工作区 | 消息对分支，可选绑定 Git worktree / checkpoint | 消息对分支，可选绑定隔离 sandbox snapshot |
| 高可用 | 无 | 单机恢复 | 多实例与租户隔离 |

### 6.1 Lite 拓扑（当前基线）

```text
Console -> FastAPI app -> TaskService -> SQLite WAL
                           |    |-> Filesystem ArtifactStore
                           |    `-> GenerationRegistry
                           `-> LocalAgentRunner -> Console Agent Runtime
                                                -> Tool Guard -> Sandbox
```

### 6.2 Workstation 拓扑（目标）

```text
Desktop Console -> local control plane -> shared Kernel
                      |-> durable local scheduler
                      |-> runner pool
                      |    |-> native Agent runner
                      |    |-> Codex Harness
                      |    `-> Qoder Harness
                      `-> workspace artifact/cache adapters
```

Workstation 不新增领域模型，只增加本地调度、多个 runner 和资源治理适配器。
当前 `WorkstationDeploymentAdapter` 会以结构化错误列出缺失的
`runner.local_pool`、`scheduler.local_durable`、`harness.adapter` 和
`resource.local_budget`，避免 Profile 被误当成可运行产品。

### 6.3 Hub 拓扑（目标，复用现有 `hub/*`）

```text
Hub Console/API -> tenant policy/control plane -> shared Task contracts
                         |-> database ledger adapter
                         |-> object artifact adapter
                         `-> RuntimeService / Provisioner
                              -> isolated tenant runtime
                                   -> runner + Tool Guard + Sandbox
```

Hub 的 `RuntimeRecord` 是部署资源，不替代 `Task` 或 `Run`。控制面通过
tenant-aware adapters 实现 Kernel ports。
当前 `HubDeploymentAdapter` 同样 fail closed，并准确报告数据库、对象存储、
分布式 runner/scheduler 与 tenant RBAC adapter 缺口；它不会回退到 Lite 的
单用户本地实现。

## 7. Contribution 与 Slot 契约

| Slot | 类型 | Host | 默认重启策略 | 当前状态 |
|---|---|---|---|---|
| `engine` | backend | Agent assembly | scoped | 已声明 |
| `agent.factory` | system backend | Chat Runtime Assembly | hot | 系统核心；固定 generation 构建 Agent，不向插件开放 |
| `agent.mode.provider` | backend | Chat mode lifecycle | hot | 已固定 mode snapshot 并迁移 turn start |
| `command.provider` | backend | Chat command router | hot | Catalog、保留名、冲突规则与三态结果已接入 |
| `hook.provider` | backend | Chat lifecycle router | hot | 固定 snapshot、依赖排序、短路与清理已接入 |
| `loop.gate.provider` | backend | ReAct loop router | hot | 固定 snapshot、scope 选择、继续/终止与延迟停止已接入 |
| `tool` | backend | Tool registry | hot | 已声明，legacy bridge 存在 |
| `tool.provider` | backend | Chat Toolkit assembly | hot | 系统与插件 Provider 已统一治理 |
| `runner` | backend | Task coordinator | hot | 已支持 Task 从 pinned generation 选择并执行；示例插件覆盖 |
| `memory` | backend | Legacy memory assembly | scoped | 兼容别名，停止扩展 |
| `memory.provider` | backend | Chat memory session | hot | 现有 memory 已作为系统适配器接入 |
| `prompt.provider` | backend | Chat prompt assembly | hot | 类型化 Fragment、排序与 generation 已接入 |
| `driver.provider` | backend | Chat Driver session | hot | 系统 Driver 工具与提示快照已接入 |
| `sensor` | backend | Proposal ingress | hot | 插件与 proactive 共用审批型 Proposal ingress；示例插件覆盖 |
| `artifact.renderer` | backend | Artifact projection | hot | 系统/插件同 Port、失败回退、安全 MIME 与来源证明已接入 |
| `ui.task.toolbar` | frontend | Task header | hot | 已预留上下文；示例插件覆盖 |
| `ui.task.tab` | frontend | Task detail tabs | hot | 已预留上下文；示例插件覆盖 |
| `ui.task.inspector` | frontend | Task side panel | hot | 已预留上下文；示例插件覆盖 |
| `ui.artifact.preview` | frontend | Artifact outcome | hot | 内建安全预览；上下文与示例插件覆盖 |
| `ui.settings` | frontend | Settings host | hot | 已声明 |

Manifest v2 使用 `schema_version`、`restart_policy` 和多条
`contributions`。普通 hot 插件的激活流程为：

```text
discover -> validate manifest -> stage -> import
         -> validate Slot Port + identity -> health check
         -> build shadow generation -> atomic publish
         -> new runs pin new generation -> old lease drains -> reclaim
```

公共 backend Slot 在发布前必须通过对应 runtime-checkable Port，并声明与
`<plugin_id>.<contribution_id>` 一致的能力身份。UI Slot 必须返回带非空
`entrypoint` 的入口描述。任一步失败都不会改变当前 generation。

以下情况必须重启，不承诺安装即生效：

- Kernel ABI 或公共模型不兼容变更；
- 核心数据库 migration；
- host middleware、进程启动参数或 native library 冲突；
- 需要重建 Agent/Channel/Memory 实例的 scoped contribution；
- 安全策略要求由管理员重新签名或重新装配的 Hub 插件。

## 8. Task 端到端闭环

```text
POST /tasks
  -> task.created
  -> plan_task / task.planned
  -> start_task / run.started (选择默认或插件 runner，并 pin generation)
  -> conversation.user
  -> runner.dispatched
  -> runner.generating / runner.responding
  -> conversation.assistant.delta
  -> artifact.produced + EvidenceRef
  -> run.completed
```

分支路径：

- 高风险动作：`approval.requested -> waiting_approval -> approval.decided`。
- 用户取消：取消 in-process execution，再提交 `run.cancelled`。
- 执行失败：提交脱敏后的 `run.failed`，不返回内部异常字符串。
- 执行超时：Coordinator 取消 runner stream，并提交
  `TaskExecutionTimeoutError` 类型的 durable `run.failed`。
- 安全暂停：保存 `safe_to_resume` checkpoint 后进入 `suspended`。
- 恢复：创建新的 `Run.attempt`，旧 run 不被覆盖。
- 进程重启：Ledger 可恢复投影；正在运行的流目前不能自动续接，这是已知缺口。

Lite 当前已验证指定项目目录、真实 Console Agent Runtime、计划、时间线、
最终对话、Markdown artifact 和 evidence。Task-scoped 内容 API 会验证内容
摘要和归属，Console 提供 256 KiB 上限的文本/Markdown 预览及命名下载；
Chat 工具输出也已由宿主在工具成功后捕获为不可变 Artifact/Evidence，历史消息
只暴露 canonical 引用和 opaque receipt，不再依赖前端从文件路径猜测成果。

Task Runtime 不注入 `approval_level=off`，也不使用 CLI 私有的
`_headless_tool_guard` 标记。它显式传递 `agent_id`，由既有 Tool Guard 按
Agent Profile 解析执行级别；Sandbox 继续作为独立执行隔离层。策略来源记录在
`runner.dispatched` 事件中。Tool Guard 活动审批使用与旧 ApprovalService 相同
的 UUID 投影到 Task Ledger；批准、拒绝和超时均先持久化，再唤醒运行时 Future。
工具拒绝/超时采用 `resume_on_decision`，让 Agent 继续解释；提案拒绝保持
`fail_on_rejection`，不会混淆两类终态语义。

## 9. 兼容与迁移

| 旧入口 | 兼容策略 | 移除条件 |
|---|---|---|
| `/console/chat/task` | 保留响应结构，内部写 Task Ledger | 所有调用方迁移且经过弃用周期 |
| Chat history | 保持原存储，不伪造历史 execution events | 不移除，只增加 Task 引用 |
| Plugin v1 `type` | 转换为 legacy contribution hint | v2 覆盖率和迁移工具达标 |
| mutable `PluginRegistry` | 作为旧 API adapter，v2 使用 generation registry | 所有插件切换稳定 SDK |
| ApprovalService | 继续通知和等待，决定同步到 Ledger | durable approval adapter 完整替换 |
| Cron direct agent run | 逐步改为创建 schedule Task | Task scheduler 支持可靠恢复 |
| Checkpoint commands | 独立保留，通过 `CheckpointStore` 对接 Task | 不要求破坏性迁移 |
| Modes / Harness sessions | 作为 runner strategy/session adapter | 不另建平行 Task 模型 |

迁移必须是增量的：先加 adapter，再迁调用方，最后才弃用旧入口。现有配置、
聊天历史和 checkpoint 不做破坏性重写。

## 10. 二次开发者最短路径

开发者只需：

1. 创建一个 `plugin.json`；
2. 从 `qwenpaw.plugins.sdk` 导入稳定类型；
3. 声明一个或多个 Contribution；
4. 运行 `qwenpaw plugin validate <path>`；
5. 安装插件，普通 hot contribution 生成新的 registry generation；
6. 新任务自动 pin 新 generation，运行中的任务保持旧 generation。

参考实现：`examples/plugins/task-insights`，其中 Planner、Strategy、Runner、
Artifact Renderer 与 UI Slots 均只依赖公共 SDK；Planner → Plan → Strategy →
Runner → Artifact/Evidence 由同一固定 generation 的 Orchestrator 串联。详细步骤位于
`docs/development/plugin-quickstart.md`。

## 11. 分阶段路线图

### R0A：Handbook 吸收项（Chat-first）

- [x] 完成 Chat 工具输出到不可变 Artifact / Evidence 的宿主捕获；内置
  `write_file`、`edit_file`、`append_file`、`send_file_to_user` 与插件声明共享
  `ToolArtifactOutput`，由 Tool Coordinator 的统一 result processor 发布。
- [ ] 将 Tool、Driver、MCP、Shell、Browser 与远程执行统一适配到
  `ActionRequest` / `ActionResult`，不为每类能力复制审批、重试和审计。
  - [x] Tool 垂直切片已落地：系统 Tool 与插件 Tool 共用执行前 Request、执行后
    Result、内容最小化、Artifact/Evidence 关联和 fail-closed 语义。
  - [x] Driver 垂直切片已落地：系统与插件 Provider、旧 Driver Manager 兼容路径
    共用 Driver Action；运行中审批通过不可变 `ActionApprovalLink` 关联，拒绝与
    执行失败分开。MCP 作为 Driver 协议随该路径接入；显式 `readOnlyHint` 映射为
    低风险无副作用，缺少注解时保持保守默认值，不按名称推断。
  - [ ] Browser 与 Harness Remote 仍通过后续显式 Adapter 迁移；不能因枚举和
    模型已存在就宣称 Action Plane 全量完成。
- [x] 将 `PromptFragment` 兼容演进为带来源、版本、信任级和哈希的
  `ContextFragment`，并在 Provider 调用前为每次实际尝试生成不可变
  `ContextManifest`。真实 Chat 验收覆盖 generation 11、93 个 Fragment 和
  70 个 Tool Schema；Manifest 不含用户原文、Secret 或隐藏推理指纹，文件权限
  为 `0600`，Conversation 目录键与 Manifest SHA256 均独立复算一致。
- [ ] 完成统一 Environment Plane：
  - [x] 冻结 `EnvironmentContract` / `EnvironmentResolution` /
    `EnvironmentRef` 与 host-owned Resolver/Store；Lite Chat 在 Agent 建立前解析，
    把不可变证据绑定到 Invocation，并由 Action 保存 resolution 引用。
  - [x] Lite 对 Workspace/Mount、OS/架构和依赖做真实校验；对不能兑现的隔离、
    网络、Secret、资源、超时、并发、快照和清理约束在执行前失败关闭。
    固定 Chat 已在 `/clear` 后以真实 `read_file` 验证环境 Resolution 与 Tool
    Action 引用一致，环境证据文件模式为 `0600`。
  - [ ] 将现有 Sandbox、Harness Remote 和 Hub runner 适配到同一契约；在这些
    Adapter 完成前，不把枚举存在误报为平台已具备隔离能力。
- 从现有事件派生统一的 `MODEL`、`ACTION`、`GUARDRAIL`、`COMPACTION`、
  `HITL`、`INTERRUPT` 与 `VERIFICATION` 语义观测；区分模型意图、策略决定、
  实际执行和 Runtime 独立证据。
- 冻结 `ModelCallAttempt` / `RouteDecision`；Lite 对直连、重试和失败留证，
  Workstation / Hub 再实现多模型路由与自动降级。
- 区分 Run Completion、Verification 与业务 Outcome，并预留 Trajectory 投影。
- Task Workbench 继续后置；上述契约先在真实 Chat 中完成验证。

`BudgetLease`、外置 Resource Registry 和分布式发现先冻结公共边界，由
Workstation / Hub 实现；Lite 不引入 Nacos、向量数据库或互联网 Federation。

### R0：Lite 基线收口（当前）

- 保持 Kernel 依赖纯度和领域契约稳定。
- 完成 Task、Console Runtime、Artifact/Evidence 和 UI 闭环。
- 保持已实现的绝对执行 watchdog，并评估按活动事件续期的 idle timeout。
- 保持已实现的 artifact 安全读取、Markdown preview 和命名导出契约。
- 保持 Tool Guard runtime approval 与 Task Ledger 的双向决策桥接。
- 把 Cron、Memory、MCP、Harness 逐项接入 Contribution adapter。

### R1：Workstation

- 将已声明的 `WORKSTATION_PROFILE` 接入可用 runtime adapters。
- 引入 durable local scheduler 和 runner pool。
- 将 Codex/Qoder Harness 实现为可选择的 runner contribution。
- 增加 workspace 级资源预算、并发和恢复策略。

### R2：Hub

- 将已声明的 `HUB_PROFILE` 接入现有 `hub/*` 控制面。
- 实现 tenant-aware Ledger、ArtifactStore、CapabilityResolver。
- 将 RuntimeService/Provisioner 接入远端 TaskRunner。
- 增加 RBAC、审计、配额、签名插件和分布式 scheduler。

## 12. Goal 验收差距矩阵

| 验收 | 当前判定 | 权威证据或缺口 |
|---|---|---|
| A 现状映射真实 | 已实现 | 本文第 3 节逐项绑定当前源码 |
| B Kernel 独立 | 已实现 | `kernel/*` 及 dependency tests |
| C 领域、状态、幂等、错误、事件契约 | 已实现 Lite 基线 | `kernel/*`、`tasks/*`、Task API tests |
| D Lite 端到端闭环 | 已实现 | Tasks UI、真实 README 翻译任务、artifact/evidence、安全预览与下载 |
| E 安全与审批边界 | 已实现 Lite 基线 | Task 继承 Agent Profile；Guard/Sandbox 回归通过；活动工具审批双向投影 Ledger，持久化失败时不唤醒工具；批准/拒绝/超时及参数脱敏有测试 |
| F Slot、热激活、回滚 | 已实现目标范围 | runner 以同 generation lease 执行；tool 复用生产 PluginApi→Workspace Registry→Governance bridge；sensor 与 proactive 共用审批型 Proposal ingress；四个必选前端 Slot、六 Contribution 示例、失败回滚和热卸载均有测试。memory/renderer 是额外预留 Slot，不属于本项必选范围 |
| G 三 Profile 共用契约 | 已实现当前范围 | 三个不可变 Profile 共享 Kernel 与 Contribution 契约；Lite deployment adapter 可运行，Workstation/Hub 通过同一工厂 fail closed 并返回精确缺失 adapter，未复制领域代码 |
| H 现有能力兼容归属 | 已实现当前范围 | 本文第 3、9 节明确 Chat、Channel、Cron、Memory/Proactive、MCP、Harness 与 Plugin 的保留/适配路径；458 项兼容切片通过。贡献化迁移按 R0/R1 渐进执行，不要求 Lite 破坏性切换 |
| I 二开 SDK 与示例 | 已实现基础版 | `plugins/sdk`、example、Quickstart |
| J 定点验证与真实任务 | 已实现当前切片 | 最终融合矩阵 287 backend tests、34 个 Tasks/API UI tests、Prettier/ESLint/pre-commit 与真实 README 翻译任务 |
| K 验收证据与风险 | 已实现当前切片 | `qwenpaw-lite-acceptance.md` 和本文第 13 节 |
| L 外部变更约束 | 已遵守 | 未 commit、push、PR 或外发钉钉 |

F、G、H 均已达到“Lite 可运行、既有能力不退化、Workstation/Hub 保留同契约
部署边界”的当前阶段目标。这不代表 Workstation/Hub runtime 或所有额外预留
Slot 已实现；对应工作保留在 R0/R1/R2 路线图中。

## 13. 风险、回滚与退出条件

### 13.1 当前残余风险

1. Console 流已有与旧后台任务一致的 3600 秒绝对 watchdog；尚未提供按最新
   活动事件续期的 idle timeout，极慢任务和真正停滞仍只能共享同一预算。
2. 进程重启后 Ledger 保留 running 状态，但 in-process execution handle 丢失。
3. Windows/Linux 尚未完成与 macOS 等价的真实运行验收。
4. v2 generation 已覆盖声明式 Contribution 和激活期 Port/identity 门禁，旧
   PluginRegistry 仍有大量调用方。
5. Hub 已有 runtime control plane 和 fail-closed deployment adapter 边界，但
   没有实现共享 Task ports 的多租户 adapter。
6. 当前命名下载通过浏览器直连内容 URL；部署启用 header-only token 时，需要
   由统一下载客户端补齐认证头或改用一次性下载凭证。
7. Context Manifest 的 Token 数量是稳定估算值，不是 Provider 的实际计费 Token；
   当前捕获最终 AgentScope 输入及 Tool Schema，但 Provider Formatter 仍可能做
   厂商特定序列化。后续 Model Call Plane 应记录实际 Provider usage 和格式版本。
8. `ContextManifestStore` 已具备按 `ChatSpec.id` 查询的公共 Port，当前尚未开放
   HTTP / UI 查看入口；Task Workbench 继续按既定顺序后置。

### 13.2 回滚原则

- Lite 路由和菜单可以独立关闭，旧 Chat 路径继续工作。
- generation 激活失败保留最后健康快照。
- SQLite migration 只前进，不自动破坏性降级。
- 回滚只反向应用本功能 patch，不覆盖工作树中用户原有修改。

### 13.3 完成退出条件

只有满足以下条件才可以结束融合 Goal：

- A–L 每项均有源码、测试或真实运行证据；
- F、H 的目标范围不再是“部分实现”或“契约已定义”；
- Lite deployment adapter 可运行；Workstation/Hub Profile 可实例化，并通过
  共享契约和结构化缺口测试。只有进入对应产品里程碑后，才要求其 runtime
  adapters 可运行；
- Console stream 具备终态 watchdog，重启中的 running task 有确定恢复策略；
- Artifact 可以安全读取并至少由一个 Markdown preview 或导出路径消费；
- 定点验证、跨平台静态审查和验收记录同步更新。
