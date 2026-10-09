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
| Approval / Ask User | `interactions/*`、`app/approvals/*` | OS substrate + Application adapter | Interaction 是权威事实，WaitCondition 是内容最小化投影 | Task Approval checkpoint continuation 已实现；Chat Ask User 待迁移 |
| Checkpoint | `checkpoints/*`、`tasks/replay.py` | Infrastructure adapter | 保留旧快照，实现安全恢复契约 | Task 安全 checkpoint 已实现；运行时快照待接 |
| Memory | `agents/memory/*`、`memory/*` | `memory.provider` Contribution | 核心只定义 port/session，后端外迁 | 已接入 Chat 固定 generation |
| Proactive cognition | `agents/memory/proactive/*` | `sensor` + Proposal producer | 只能产生 Proposal，审批后进入 TaskOrder | 已实现门禁路径 |
| Cron / Heartbeat | `app/crons/*` | Scheduler adapter | 保留调度，触发 `TaskSource.SCHEDULE` | 已有能力待接 Task |
| Channels | `app/channels/*` | scoped Contribution | 复用 BaseChannel，按 workspace scoped reload | 已有插件能力 |
| MCP / Drivers | `app/mcp/*`、`drivers/*` | `driver.provider` / scoped runtime | 保留策略与凭据边界 | 系统 Driver 已接入固定 generation；原生插件治理契约待冻结 |
| Modes | `modes/*` | `agent.mode.provider` + 细分 Contributions | 与 Task `strategy` 分离 | 生命周期与 namespaced Mode State Host 已适配，内部贡献待拆分 |
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
| `agent.mode.provider` | backend | Chat mode lifecycle + namespaced state | hot | 已固定 mode snapshot、turn start 与 CAS State Host |
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
         -> build shadow generation -> risk-based scenarios
         -> evidence + evaluation -> authorized promotion
         -> atomic publish
         -> new runs pin new generation -> old lease drains -> reclaim
```

公共 backend Slot 在发布前必须通过对应 runtime-checkable Port，并声明与
`<plugin_id>.<contribution_id>` 一致的能力身份。UI Slot 必须返回带非空
`entrypoint` 的入口描述。纯 UI、Renderer 和 Prompt Contribution 可以只运行
确定性契约样例；Tool、Driver、Browser、Shell 和 Harness 等有副作用能力还必须在
shadow generation 验证 Policy、Approval、幂等、Artifact/Evidence 和失败语义。
Evaluation 只提交证据与结论，只有 Host Promotion 能发布 generation。任一步失败或
证据不足都不会改变当前 generation。普通 hot 插件仍在安装事务完成后立即生效，
不要求重启服务。

以下情况必须重启，不承诺安装即生效：

- Kernel ABI 或公共模型不兼容变更；
- 核心数据库 migration；
- host middleware、进程启动参数或 native library 冲突；
- 需要重建 Agent/Channel/Memory 实例的 scoped contribution；
- 安全策略要求由管理员重新签名或重新装配的 Hub 插件。

## 8. 持续执行与 Task 闭环

### 8.1 Conversation Execution Chain

Chat 是 QwenPaw 3.0 的首要交互入口，但“一问一答”只是一种 UI 投影，不再作为
Agent Runtime 的唯一生命周期模型。短问答继续走单 Submission、单 Invocation 的
快速路径；长程工作由同一组 Kernel 契约自然扩展，不另建平行的长任务会话。

```text
ChatSpec.id
  -> Submission (用户意图或控制输入)
  -> Invocation N
  -> Model Step -> Action(s) -> Artifact / Evidence
  -> complete: Verification -> Outcome
  -> wait: Interaction / Resource Wait -> end Invocation N
           -> durable continuation Submission
           -> Invocation N+1 (same correlation_id)
```

身份边界如下：

- `ChatSpec.id` 标识整个 Conversation，公共 API 优先使用它而不是
  竞争性的 `session_id`；
- `Submission.id` 提供接收幂等、队列顺序和精确回执；
- `correlation_id` 贯穿同一用户意图的多次 Submission、Invocation、Action、
  Interaction、Artifact 和 Verification；
- `Invocation.id` 只标识一次 Runtime 尝试，不跨进程复活；
- Assistant message 是面向人的输出，不是 Runtime 状态机的终止标志。

Action、Model Call 以及 Queue / Steer / Interrupt 控制面的 Submission、Command、
Receipt、Queue、Execution Chain 与 Runtime Projection 公共 Kernel/HTTP 合同均使用
`chat_id`。持久化适配器仍可读取旧 `conversation_id`，SDK 也暂时保留只读 Python
属性，但新 JSON、SSE、Console 类型与插件代码不得继续产生第二个 Chat 身份名称。
插件和内置 Provider 共同接收的 `InvocationScope` 也只发布可选 `chat_id`；
`session_id` 继续表示传输上下文，不能作为 Chat ownership fallback。旧插件构造参数
仍可读取，冲突双身份失败关闭；`RuntimeAssemblyFactory.open()` 对新调用者使用同一
canonical 参数，并在生成 Scope 前完成兼容归一化。
Outcome 与 Observation 等嵌套权威事实按各自合同独立迁移，不能因为被 Runtime
Projection 引用就隐式改写其 schema。Outcome 声明与事实、RuntimeObservation 和
ConversationTrajectoryPage 公共合同均已使用 `chat_id`；历史输入继续兼容
`conversation_id`，Observation 的派生索引、owner hash 与游标格式不变。

Chat Runtime 已按 `correlation_id` 从权威 Submission 与 Interaction 派生
`ConversationExecutionChain`：同一意图的多次 Submission / Invocation 保持一个执行
链，状态明确区分 queued、running、waiting_user、failed、interrupted、cancelled 与
inactive。`inactive` 只表示当前没有运行中的 Invocation；即使最近一次 Submission 为
`succeeded`，也不表示业务 Outcome 已达成。后续 Outcome 必须来自独立权威事实，不能
由 assistant message、HTTP response 或 SSE 结束推断。公开投影使用最近 200 条
Submission 加当前 Queue 的有界窗口，并显式返回 `execution_window_truncated`；完整
历史仍由权威 History Port 提供。

Lite 已冻结 `ConversationOutcome` / `ConversationOutcomeStore`：achieved、partial、
not_achieved 与 abandoned 都必须由具名 producer 显式提交，并通过 outcome ID 建立不可
变 supersession 链。执行链只在 Outcome 晚于最近 Submission、且当前没有 Queue、运行
或 blocking Interaction 时采用 Outcome 状态；否则仍以实时执行事实为准。Outcome 可
引用 Artifact、Evidence 与 Verification，但 producer admission 和“Task 验收通过才
能声明 achieved”的策略由 Host Outcome Broker 统一执行。system/plugin producer
必须先由 Host 注册，均通过同一声明入口且不能直接访问 Store；普通 Chat 的引用必须
属于当前 `ChatSpec.id`，Task-owned achieved 还必须通过既有 Execution Contract、
Verification Policy 与 Result Package 完成门禁。Producer 支持热注册/注销，不要求
Runtime 重启；模型文本仍不能自动补写 Outcome。

Outcome 的公共声明和不可变事实只输出 `chat_id = ChatSpec.id`；Invocation-bound
Request 仍不携带 Chat 身份，由 Host 强制绑定，避免 system/plugin producer 伪造。
Lite SQLite 的旧列和历史 JSON 无需重写：读取时接受 `conversation_id`，幂等比较先
恢复成同一领域对象，因此滚动升级不会制造虚假 Outcome 冲突。

Runtime 装配使用独立的可选 `OutcomeHostAccess`，不修改既有 `ToolHost`、
`DriverHost` 的必选协议，因此旧插件保持结构兼容。实际 `OutcomeHost` 由当前 pinned
`InvocationScope` 绑定 Agent、`ChatSpec.id`、correlation、Invocation ID 和 registry
generation；provider 请求不包含这些字段，不能跨会话或冒充其他 producer。system 与
显式获准的 plugin 使用同一 Host 类型和 Broker，未注册 plugin 得到 `None`。准入在
Invocation 打开时形成 lease；热卸载只影响后续 Invocation，不打断已固定旧 generation
的执行。

Trajectory 不另建事实源。Lite 从既有 semantic Observation 与不可变 Outcome
supersession 历史派生 correlation-scoped 正序回放，并以 content-free index 保存源
指针、固定分页 watermark。Steer/Interrupt 通过其 target Invocation 对应的 Submission
恢复 correlation；Artifact、Evidence、Verification、Wait/Recovery 和 Outcome 保留
原权威引用。缺少 correlation 的 legacy 事实不会靠时间邻近猜测归属。

Approval、Ask User、Suggestion、Steer 和 Interrupt 均是执行链中的一等事件。
Interaction 或资源等待必须保存 durable `WaitCondition` 并释放计算资源；满足条件后
由 outbox 创建新的 continuation Submission，而不是恢复旧 Python 调用栈。
`InteractionRequest.chat_id` 是 Approval、Ask User 与 Suggestion 的唯一公共 Chat
身份；对应的 `WaitCondition.chat_id` 沿用同一命名。旧 `conversation_id` 只在
持久化读取、checkpoint/outbox 存储和 Python 兼容属性中保留。

### 8.2 分层恢复

QwenPaw 区分五层故障边界：

1. 浏览器/SSE 断开只重建投影，不停止服务端 Invocation；
2. 模型连接前失败可创建新的 `ModelCallAttempt`，使用独立 transport 退避；
3. 模型流已产生内容但未正常结束时保存 partial outcome，不盲目重放完整 Turn；
4. 已提交 Action 以 `ActionResult` / Side Effect 状态对账，成功不重做，
   uncertain 必须查证或取得显式重试授权；
5. 进程重启从 Checkpoint、Wait 或 Outbox 创建新 Invocation，不伪装原地续跑。

传输失败、Provider overload、rate limit、quota、budget、auth、policy、用户
Interrupt 和 unknown error 必须进入类型化恢复裁决。短暂网络抖动可以有界重试；
超过等待预算后进入 durable Resource Wait 并释放槽位，禁止用无限 retry 永久占用
Lite 运行资源。恢复事件必须受最新 Interrupt revision fencing。

Provider 的 WebSocket 增量续传、sticky route 和 HTTP fallback 属于 Adapter
capability，不能进入 Kernel。流内并发执行工具只有在 Provider 输出稳定的
committed action identity，且 Host 已原子保存 `ActionRequest` 后才允许启用；
不得从未完成的模型增量中猜测并执行工具。

详细裁决和验收见
`docs/analysis/qwenpaw-codex-recovery-and-long-horizon-absorption-2026-10-01.md`。

### 8.3 Task 端到端闭环

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
    Result、内容最小化、Artifact/Evidence 关联和 fail-closed 语义。Action 公共模型
    统一以 `chat_id = ChatSpec.id` 表达归属，历史 `conversation_id` 仅作为输入与
    Python 属性兼容，不再作为新 JSON 字段扩散。
  - [x] Driver 垂直切片已落地：系统与插件 Provider、旧 Driver Manager 兼容路径
    共用 Driver Action；运行中审批通过不可变 `ActionApprovalLink` 关联，拒绝与
    执行失败分开。MCP 作为 Driver 协议随该路径接入；显式 `readOnlyHint` 映射为
    低风险无副作用，缺少注解时保持保守默认值，不按名称推断。
  - [x] Browser 外层受治理执行边界已迁移：统一与兼容实现、内置与插件均用
    显式 `ActionKind.BROWSER`，执行前后复用同一 Request / Result 管线，不按名称
    推断；真实 Chromium、固定 Chat、失败输出 Artifact/Evidence 及安全 Action 查询
    均已通过。Browser 内部逐方法副作用分类仍由对应子系统继续闭环。
  - [x] 本地 Codex/Qoder Harness Remote 已通过 provider-neutral Event Adapter
    接入：受控 Chat turn 固定 generation 和 EnvironmentRef；审批回调在 Provider
    恢复前记录 Request/Approval Link，TOOL_COMPLETED 写入最小化 Result，缺失终态
    收敛为 `unknown/uncertain`。仅有 TOOL_STARTED 的动作属于观测边界，不冒充宿主
    执行前拦截。
  - [ ] Hub remote runner、跨主机执行与 attested Runtime 仍待显式 Adapter；不能因
    本地 Harness 已接入就宣称远程 Action Plane 全量完成。
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
  - [x] 现有 Sandbox 已作为 Action 级环境子契约接入；具体后端声明的真实
    enforcement 决定 Resolution，环境变量只记录名称不记录值，原来仅告警的
    未兑现安全约束会在执行前失败关闭。固定 Chat 当前显式关闭 Sandbox，真实
    Shell 回归保持 Host EnvironmentRef，未虚构隔离证据；开启态真实验收待隔离
    配置环境完成。
  - [x] 本地 Codex/Qoder Harness 已在受控 Chat Invocation 的 Provider 调用前
    生成同一 Contract/Resolution；Workspace、Skill 目录和 MCP stdio 依赖由宿主
    校验，MCP Secret 只保存变量名和 opaque 引用。Codex sandbox 与 Qoder
    permission 明确记录为 `provider_declared`，不冒充宿主 enforcement 或远端证明。
  - [ ] 将 Harness Remote 和 Hub runner 适配到同一契约并提供
    `provider_attested` 证据；在这些 Adapter 完成前，不把 Provider 声明或枚举存在
    误报为平台已具备远端隔离能力。
- 从现有事件派生统一的 `MODEL`、`ACTION`、`CONTROL`、`GUARDRAIL`、
  `COMPACTION`、`HITL`、`INTERRUPT` 与 `VERIFICATION` 语义观测；区分模型意图、策略决定、
  实际执行和 Runtime 独立证据。稳定 `RuntimeObservation` 只读契约以
  `INTENT/POLICY/EXECUTION/EVIDENCE` 表示责任边界，并通过 `source_type +
  source_id` 回指权威事实，不形成第二份事实库。Lite 已接入 Model Call 和 Action，
  Action Policy 单独派生 GUARDRAIL；统一 Interaction Request/Resolution 已接入 HITL，
  且不复制 prompt、自由文本回答或结构化 values。Control Command/Receipt 通过独立
  只读 History Port 派生 CONTROL 与 INTERRUPT，保留 revision 和 Steer safe point，
  但不复制 instruction、idempotency key 或 receipt detail。COMPACTION 从不可变
  CompactionRecord 派生，覆盖 automatic、manual 与 overflow recovery；消息正文、
  摘要、压缩指令和异常正文均不落入记录，真正 no-op 不生成虚假的成功事实。
  VERIFICATION 从 Task Ledger 的 `verification.completed` 权威事件派生，以只读
  `VerificationHistoryPort` 关联 ChatSpec.id；公开投影不复制验收文本、失败原因或
  插件 metadata。执行成功、Artifact 生成和 Verification 通过仍是三个独立事实。
  Lite 通过只保存 source pointer 的派生索引提供固定水位分页；不透明游标绑定
  `ChatSpec.id`、首屏 `indexed_sequence` 和最后排序键。翻页期间新增或后补的
  Evidence 不进入旧快照，且权威事实仍从原 Store 动态投影，不形成第二份事实库。
  Observation 与 Trajectory Page 的公共 JSON 只使用 `chat_id = ChatSpec.id`；旧
  `conversation_id` 只在恢复输入和 Python 兼容属性中保留，不改变索引与游标所有权。
  Chat-owned Artifact/Evidence ownership record 与 Task-result snapshot 也遵循同一
  身份规则；私有 receipt、Task Ledger 和查询 Port 保持原存储结构，无需数据重写。
- [x] 冻结 `ModelCallAttempt` / `RouteDecision` / `ModelCallResult` 和 Store Port；
  Lite 已在真实 Provider 网络边界记录直连、同模型重试、跨模型 fallback、overflow
  retry、流式成功/取消/失败和 usage，并提供 Chat-owned 只读查询。Route 区分逻辑
  请求与实际 Provider/Model，Attempt 记录 Adapter/Formatter 身份和版本；缺失价格
  以 `cost_unknown` 留证，不折算为零。
- [x] Token 统计收敛到同一模型调用事实，不由前端或消息历史反推。
  - [x] 新 Model Call Attempt 已保存
    `agent_id → ChatSpec.id → invocation(turn) → provider/model` 完整归属；Result 已保存
    Provider 报告的 input/output、cache 与 cost，且先持久化事实，再更新旧 JSON 兼容投影。
  - [x] 日期按 UTC、模型按实际调用路由；Provider usage 标记为
    `provider_reported`，Chat 上下文估算标记为 `local_estimate`，两种口径不混合。
  - [x] Lite 使用可删除 SQLite 投影按 `attempt_id` 幂等索引 Model Call usage；启动时
    从全部已配置 Agent Workspace 重建。首次初始化把下一 UTC 日设为不可变 cutover：
    水位前读取 legacy JSON，水位后有 Chat/Turn 归属的调用只读事实投影，无 Attempt
    的兼容调用仍可读取。`/api/token-usage/projection` 公开无内容的水位、索引量和最后
    rebuild 状态。旧聚合记录继续可读，但不伪造历史上不存在的 chat/turn 归属。
  - [x] Context Window 统计不扫描消息正文：Attempt 记录实际模型 window 和当次
    compaction threshold，Result 提供 Provider input/cache usage。投影在 global、Agent、
    Chat、Invocation turn、日期和实际模型层统一计算加权占用、单次峰值、可观测调用及
    临近压缩调用。缓存语义可验证时用 cache-eligible input，否则用 Provider input；
    旧调用缺 window 时保持不可观测，不伪造 `0%`。Chat 的 `local_estimate` 只服务当前
    上下文指示器，不混入跨范围 Provider 统计。
- [x] 将 Chat 运行模型从隐含的一问一答升级为 Conversation Execution Chain：
  `Submission` 是输入，`Invocation` 是一次运行尝试，`correlation_id` 贯穿同一意图
  的多次等待与恢复；短问答继续使用单 Invocation 快速路径。
  - [x] 已冻结退出边界：Outcome、显式 Stop / Interrupt、不可自动化的 typed Wait，
    或预算/恢复终态；Assistant Message、HTTP response 和 SSE 断开不结束执行链。
  - [x] Ask User admission 已限制为必要事实、关键偏好、范围授权和高影响裁决，禁止
    用“是否继续”代替 Runtime 调度。
  - [x] Goal Mode 的 active state 已按 `ChatSpec.id` 归属，传输 session 变化不再切断
    长程意图；模型调用 `update_goal` 只是 Outcome 请求，必须由当前 Invocation 的 Host
    Broker 持久化后才生效。无 Chat 的 Channel 保留 session fallback，短问答无需进入
    Goal Mode。
  - [x] Lite Goal 已通过 Kernel `GoalExecutionStore` 持久化并在 turn-start 恢复；CAS
    revision 阻止 active replacement 和契约漂移，pending Outcome 可跨 Invocation
    幂等收敛。Chat admission 在 active Goal 中继承同一 correlation；`/clear`、预算和
    迭代上限只产生明确技术终态，不制造业务完成。`GoalExecution` 公共合同只输出
    `chat_id = ChatSpec.id`；Lite Store 的旧列和历史 JSON 由 Adapter 兼容读取，不要求
    数据重写，也不允许 canonical/legacy 双身份冲突。
  - [x] Goal pending Outcome 已接入 Workspace 启动恢复：内部 Submission 复用统一
    Invocation Control 与 generation lease，直接执行 Host 声明和 CAS finalize，不调用
    模型、不产生对话消息，也不等待用户发送下一轮输入。
- [x] 冻结 Model Recovery Contract：区分连接前失败、部分流中断、Provider 限流、
  quota/budget/auth/policy、用户 Interrupt 与 unknown；transport retry 不消耗业务
  retry，超过短等待预算后持久化 Resource Wait 并释放运行槽。稳定
  `ModelRecoveryDecision` 在 Kernel 固化 failure/disposition 矩阵和 Retry-After
  约束；Provider 分类器与持久 `ModelCallResult` 使用同一验证入口。
- [ ] 完成长程恢复闭环：
  - [x] Interaction conversation turn 已通过 durable outbox 创建后续 Submission，
    继承 correlation 并创建新 Invocation；重启与 enqueue/mark 崩溃窗口保持幂等。
  - [x] Resource Wait 与 bounded Model Step continuation 已跨进程恢复；partial output
    不进入下一次上下文，Stop / Interrupt、崩溃窗口与恢复预算均有持久化边界。
    Interaction、Model Step、Resource Wait 与 Harness recovery 的 durable contract
    共用 `chat_id = ChatSpec.id`；旧 `conversation_id` 只作为 SQLite/checkpoint
    恢复输入和 Python 兼容属性，不再由新 JSON 或插件 SDK 产生。
  - [x] 成功 Action 不重做，uncertain Action 必须先对账或取得显式授权；当前已经
    持久化 pending / uncertain / durable-context 三类 assessment；同步 terminal Action
    与同 Invocation 内进入上下文的后台完成 hint，已通过 provider-neutral
    `CommittedActionItem`、不可变私有 snapshot 和内容安全 Checkpoint 自动续行。
    受控 Harness completion 已由 Session Bridge 在原子写入规范化 tool output 时附加
    同一 binding；completion event 与 history hydrate 不会伪造该事实。Harness 断流
    已通过 `HarnessRecoveryContextCheckpoint` 四方校验并持久化 admission；共享
    dispatcher 已用 durable outbox 创建 fenced continuation，并校验 Stop / Interrupt、
    新输入、Queue revision、反向绑定、backend 与 2-cycle 预算。后台 Action 也已在
    结果 digest 确定后先准备未发布私有 context snapshot，ActionResult 成功落库后再由
    同一 dispatcher 跨 Invocation 主动续行；并行结果合并到最新 Session，不相互覆盖。
- [ ] Workstation / Hub 再实现按能力、健康、成本和数据边界的动态路由；Lite 当前
  继续使用确定性主模型与显式 fallback 顺序，不冒充智能路由器。
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
9. 模型流与受控 Harness 已具备 typed outcome、Resource Wait 或 fenced continuation；
   Provider 传输能力已从 Kernel 恢复逻辑中分离：Kernel 只冻结 transport contract、
   哈希化 response/prefix/route evidence 与 fail-closed 裁决；当前内置 Provider 均使用
   HTTP/non-resumable 安全默认，部分流进入 durable context rebuild。尚未完成首个真实
   cursor-resume Adapter，以及浏览器级真实断流/进程重启演练。

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
- Chat 长程意图可跨多个 Invocation 保持 correlation，且网络、资源等待、用户中断
  与副作用不确定性有互不混淆的恢复策略；
- Artifact 可以安全读取并至少由一个 Markdown preview 或导出路径消费；
- 定点验证、跨平台静态审查和验收记录同步更新。
