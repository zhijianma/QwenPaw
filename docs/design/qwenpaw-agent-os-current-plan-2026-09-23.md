# QwenPaw Agent OS 总体架构与实施路线

- 日期：2026-09-24
- 状态：实施中
- 目标分支：`feat/lite-agent-os`
- 文档类型：功能设计
- 架构范围：QwenPaw OS，以及 Lite / Workstation / Hub 三种产品配置

> 2026-09-24 执行调整：暂停 Task Workbench 功能开发，先完成 OS R0 全部基建
> 模块及兼容迁移。当前逐模块计划见
> `docs/design/qwenpaw-os-infrastructure-migration-plan.md`。
>
> `docs/share/qwenpaw-detailed-solution.md` 的差异与吸收裁决见
> `docs/analysis/qwenpaw-detailed-solution-gap-analysis-2026-09-24.md`。产品概念可称
> Work，R0 代码与公共 API 继续以 Task 为唯一持久目标聚合，避免形成平行事实源。

## 1. 架构定位

QwenPaw 的目标不是再实现一个相似的 Coding Agent，而是形成一个可组合、可治理、可持续演进的 Agent OS。OS 提供稳定的任务、执行、能力、审批、产物、证据、调度和治理契约；具体 Agent、Harness、Tool、Memory、Channel、Sensor、UI 与部署能力通过适配器或插件贡献接入。

Lite、Workstation、Hub 不是三套代码，也不是由低到高复制功能的三个版本，而是同一 OS Kernel 上的三种产品 Profile：

- Lite：单用户、本地优先、安装后即可运行，验证 OS 的最小完整闭环。
- Workstation：面向专业个人和小团队，增加本地调度、Runner Pool、Harness 和资源治理。
- Hub：面向组织和多租户，增加远程运行时、RBAC、审计、配额、高可用和分布式调度。

当前先以 Chat 作为第一条可运行纵切面，再迁移 Task Workbench。所有领域模型、端口、Contribution 和 Slot 都必须站在 OS 全局设计，不能把 Lite 的 SQLite、文件系统或单进程实现写入 Kernel。

## 2. OS 设计原则

1. 稳定 Kernel 只保存跨产品不变的领域契约和状态机。
2. 产品差异由 Profile、Port Adapter 和部署拓扑表达，不复制领域代码。
3. 内置能力与插件能力走同一 Contribution、Catalog 和运行协议。
4. 所有 Run 固定不可变 capability generation，执行中不发生能力漂移。
5. Application 负责用例编排，HTTP、Console 和 UI 只负责命令与投影。
6. AgentScope、Codex、Qoder、MCP、Driver 等都是 Runtime Adapter，不反向定义 OS 领域模型。
7. 高风险动作统一进入 Approval Broker，并先持久化再唤醒执行。
8. 大内容统一进入 Artifact Store，Evidence 用于证明产物和结论来源。
9. 普通插件以热激活为默认；只有 ABI、数据库、进程或安全边界变化才要求重启。
10. 兼容层只能翻译旧契约，不允许形成第二套任务或审批事实源。
11. 能力缺失、Slot 错误或协议不匹配必须失败关闭，禁止静默降级。
12. 插件开发者只依赖稳定 SDK，不需要理解内部数据库、线程、HTTP 或前端 Store。

## 3. OS 分层架构

```text
┌───────────────────────────────────────────────────────────────┐
│ Experience                                                   │
│ Console / Task Workbench / Chat / Channels / CLI / Hub Admin │
├───────────────────────────────────────────────────────────────┤
│ Application                                                  │
│ Task / Chat / Schedule / Approval / Plugin lifecycle         │
│ Workspace assembly / Projection / Compatibility APIs         │
├───────────────────────────────────────────────────────────────┤
│ Runtime                                                      │
│ Agent / Harness / Tool Guard / Sandbox / MCP / Driver        │
│ Runner pool / Runtime provisioner / live event delivery      │
├───────────────────────────────────────────────────────────────┤
│ Stable Kernel                                                │
│ Task / Plan / Run / Event / Approval / Artifact / Evidence   │
│ Capability / Contribution / Profile / state machines / ports │
├───────────────────────────────────────────────────────────────┤
│ Infrastructure                                               │
│ SQLite / database / filesystem / object store / process      │
│ container / queue / identity / observability adapters        │
└───────────────────────────────────────────────────────────────┘
```

强制依赖方向：

```text
Experience -> Application -> Kernel
Runtime    -> Kernel
Adapters   -> Kernel ports
Kernel     -X-> FastAPI / AgentScope / plugins / editions / UI / database
```

## 4. OS 能力域

OS 3.0 的 P1–P6 表示能力域，不是固定调用层级：

| 能力域 | OS 稳定职责 | QwenPaw 当前承载 |
|---|---|---|
| P1 Capability & Execution | 能力描述、解析、执行协议 | Runner、Tool、Harness、Driver |
| P2 Scheduling & Contracts | Task、Plan、Run、事件、调度契约 | TaskService、Ledger、Cron adapter |
| P3 Agent Runtime | Agent 会话、策略、执行上下文 | AgentScope、Modes、Console Runtime |
| P4 Memory & Semantics | Memory Port、语义上下文 | `memory.provider` 已接入 Chat；后端仍由现有 memory 实现承载 |
| P5 Cognition | Proposal、Sensor、主动认知边界 | proactive responder、sensor host |
| P6 Evolution & Governance | Contribution、代际、审批、策略 | Plugin、Tool Guard、Sandbox、Hub policy |

Task Runtime 是贯穿 P1–P6 的第一条纵向闭环，不等于整个 OS。Chat、Channel、Schedule、Memory、Cognition、Workspace 和 Hub Control Plane 都应逐步对齐同一套 OS 契约。

## 5. 核心领域模型

```text
Workspace / Tenant Scope
├── Task（持久目标）
│   └── Run（一次执行尝试）
│       ├── TaskOrder / Plan / Checkpoint
│       ├── ConversationMessage / ToolActivity
│       ├── ApprovalRequest / ApprovalDecision
│       ├── ArtifactRef / EvidenceRef
│       └── ExecutionEvent
├── CapabilityDescriptor / CapabilityContribution
├── RegistryGeneration / CapabilityLease
├── Proposal / Sensor
├── Schedule
└── Policy / Audit Record
```

`Task` 表示可持续追踪的目标，`Run` 表示一次执行尝试。失败、取消和恢复不能覆盖旧 Run，而是保留完整历史并在恢复时创建新 attempt。Conversation、Tool、Approval、Artifact 和 Evidence 必须能够从同一事实日志投影出来。

Kernel 模型不选择 SQLite、对象存储、本地 Runner 或 Hub Runtime，这些都由 Profile 对应的 Adapter 注入。

## 6. Contribution 与 Slot 系统

```text
CapabilityContribution
├── contribution_id
├── capability_id
├── slot
├── provider_id
├── provider_kind: system | plugin
├── version
├── restart_policy: hot | scoped | restart
├── input_schema / output_schema / config_schema
└── metadata
```

OS 运行槽位：

| Slot | 协议职责 | 系统适配方向 |
|---|---|---|
| `agent.factory` | 系统核心：从固定 generation 构建框架 Agent | current AgentBuilder adapter |
| `agent.mode.provider` | 绑定 Chat 会话模式生命周期 | Default/Coding/Goal/Mission compatibility adapter |
| `command.provider` | 提供静态命令目录与三态分发 | Workspace commands/Skill fallback adapter |
| `hook.provider` | 提供固定生命周期 Hook Catalog | Workspace HookRegistry adapter |
| `loop.gate.provider` | 决定 ReAct 循环继续或结束 | Agent Modes stop-handler adapter |
| `planner` | 目标生成可执行计划 | basic planner |
| `runner` | 执行 TaskOrder | Native Agent runner |
| `strategy` | 准备运行策略 | Default/Coding/Goal/Mission |
| `tool.provider` | 提供受治理工具 | Workspace/MCP tools |
| `driver.provider` | 绑定浏览器或设备 Driver | DriverManager |
| `harness.runner` | 外部 Coding Agent 执行 | Codex/Qoder |
| `memory.provider` | 打开语义与长期记忆 | memory backends |
| `prompt.provider` | 提供有序、类型化系统提示片段 | current PromptManager adapter |
| `sensor` | 产生 Proposal，不能直接执行 | proactive cognition |
| `artifact.renderer` | 安全渲染产物 | text/Markdown renderer |
| `scheduler` | 触发和恢复任务 | local/distributed scheduler |

Experience 槽位：

| Slot | 使用位置 |
|---|---|
| `ui.task.toolbar` | Task 顶部动作 |
| `ui.task.tab` | Task 详情标签页 |
| `ui.task.inspector` | Task 侧边检查器 |
| `ui.artifact.preview` | Artifact 预览 |
| `ui.settings` | 设置中心 |
| `ui.workspace.panel` | Workspace 扩展面板 |

内置实现是 `provider_kind=system` 的可信 Contribution，不拥有特殊运行通道。旧 `tool`、`memory`、`engine` 等名称在迁移期映射到新槽位。

## 7. 模块组织

```text
src/qwenpaw/
├── kernel/                 # 稳定领域模型、状态机、ports
├── capabilities/           # 通用 Catalog、generation 与系统能力
├── tasks/                  # Task application service、投影与 replay
├── editions/               # Lite / Workstation / Hub Profile 组合
├── plugins/
│   ├── sdk/                # 二次开发者唯一稳定导入面
│   ├── contributions.py    # Manifest -> common contribution
│   └── generations.py      # 旧插件 API 的兼容适配器
├── app/                    # HTTP、Console、Chat、Schedule、Workspace
├── agents/                 # Native Agent runtime adapter
├── harnesses/              # Codex/Qoder runner adapters
├── runtime/                # Tool、Runner、Session 等运行装配
├── security/               # Tool Guard 与 policy adapter
├── sandbox/                # 本地/容器执行隔离
├── memory/                 # Memory provider implementations
├── drivers/                # Browser/device provider implementations
└── hub/                    # 多租户控制面与 runtime provisioner

console/src/
├── api/                    # OS API clients
├── pages/Tasks/            # Task Workbench
├── pages/Chat/             # Chat Experience
├── pages/Settings/         # Profile / plugin / policy 配置
└── plugins/registry/       # Menu / Route / Slot hosts
```

`editions` 只能组合端口实现和功能开关，不允许承载领域状态机。插件只能依赖 `qwenpaw.plugins.sdk` 和公开 Kernel 类型。

## 8. 三种产品配置

| 维度 | Lite | Workstation | Hub |
|---|---|---|---|
| 用户 | 单用户、个人设备 | 专业个人、小团队 | 多租户团队、组织 |
| Kernel | 共享 | 共享 | 共享 |
| Runner | 单本地 Runner | 本地 Runner Pool + Harness | 分布式隔离 Runtime |
| Ledger | SQLite WAL | SQLite / 外部 DB adapter | 共享数据库 adapter |
| Artifact | 项目文件系统 | Workspace / NAS adapter | Object Store |
| Scheduler | 进程内、有限 | Durable Local Scheduler | Distributed Scheduler |
| Approval | 交互式严格审批 | Policy + Interactive | RBAC + Policy + Audit |
| Isolation | 本地 Sandbox | Process/Container 可选 | 强隔离 Runtime |
| Plugin | 默认 hot | hot + scoped | 签名、策略控制 |
| HA | 无 | 单机恢复 | 多实例高可用 |

Lite 当前承担 OS 最小可运行闭环；Workstation 只增加本地生产力与资源治理适配器；Hub 只增加多租户控制面和分布式适配器。三者共享 Task、Run、Approval、Artifact、Contribution 和事件契约。

## 9. OS 统一执行链路

```text
用户 / Channel / Schedule / Sensor
  -> 创建 Task 或 Proposal
  -> Policy 与 Approval Gate
  -> RuntimeOrchestrator 固定 generation
  -> Planner + Strategy
  -> 构造不可变 RuntimeContext
  -> Runner 调用 Tool / Memory / Driver / Harness
  -> Event + Conversation + Approval + Artifact + Evidence
  -> Projection 驱动 Console / Workbench / Hub Admin
  -> complete / fail / cancel / suspend
  -> 从安全 Checkpoint 创建新 Run 恢复
```

Chat-first 迁移阶段不强制每次对话先创建 Task。Chat 与 Task 共同使用
`InvocationScope`、固定 capability generation 和 Runtime Assembly；Task 只在
需要持久目标、计划、审批、恢复或成果追踪时附加其领域上下文。这避免 Task
模型反向污染 Kernel 的通用调用边界。

Sensor 只能产生 Proposal，不能绕过 Policy 直接执行。Channel 和 Chat 可以成为 Task 入口，但不能各自维护另一套执行状态。所有能力都通过固定 generation 解析。

## 10. 插件热激活与生命周期

```text
discover -> validate -> stage -> import -> health check
         -> build shadow generation -> atomic publish
         -> new runs pin new generation
         -> old leases drain -> reclaim
```

普通 `hot` Contribution 安装后无需重启。`scoped` Contribution 在 Workspace、Agent 或 Session 边界安全重建。只有以下情况允许要求服务重启：Kernel ABI 不兼容、核心数据库 migration、host middleware 或启动参数变化、native library 冲突、Hub 安全策略要求重新签名或部署。

激活失败不得修改当前已发布 generation。卸载通过发布不含该 Contribution 的新 generation 实现，不中断持有旧租约的 Run。

## 11. Task Workbench 在 OS 中的职责

Task Workbench 是 OS 的任务控制面，而不是 Lite 专属页面。权威 Task Projection 返回 Task、Runs、Plan、Conversation、Tool Activity、Approvals、Artifacts、Evidence、Checkpoint、Capabilities 和最后事件序号。

Workbench 必须提供：创建/启动、计划、真实对话、工具活动、审批、产物预览与下载、证据、取消、失败诊断和恢复。Workstation 在相同 Projection 上增加 Runner、资源与并发视图；Hub 增加 Tenant、Policy、Audit 和分布式 Runtime 视图。

## 12. 当前实现状态

### 已形成的 OS 基础

- Kernel 模型、Port、Task/Run 状态机、SQLite Ledger、Projection 和 Replay。
- 内置与插件 Runner/Planner/Strategy 使用共同 Capability Catalog 和代际租约。
- 类型化 Approval Broker、Cancellation Token、Artifact Emitter 和 Evidence。
- Driver、Codex、Qoder 审批向统一 Task 边界桥接。
- 系统/插件 capability bundle、Generation Registry、原子发布与租约。
- Sensor Contribution Host 和 Proposal 审批入口。
- Task Workbench 的计划、时间线、对话、审批、产物、证据和 SSE 更新。
- Lite Profile 已接通真实本地运行适配器；Workstation/Hub Profile 采用 fail-closed 缺口报告，未伪装为可运行产品。
- Task-independent `InvocationScope` 已固定会话身份、工作区、审批级别、能力
  选择和 registry generation，不包含 `task_id` / `run_id`。
- Provider-neutral `GenerationRegistry` 已从插件目录上移到 `capabilities`，支持
  staging、health check、原子发布、旧租约排空和 provider 热替换。
- Chat Runtime 已通过 system-only `agent.factory` Slot 装配 Agent；现有
  `AgentBuilder` 作为系统兼容适配器继续提供原能力，并固定在 Invocation generation。
  第三方 manifest 不能声明该 Slot，避免 HookContext、AppServices 和 AgentScope
  事件对象泄漏为公共契约；外部扩展使用下层公共 Provider Slots。
- Chat 工具已通过 `tool.provider` Slot 装配；系统 Workspace Provider 与插件
  Provider 共享 `InvocationScope`、`ToolSelection` 和 `ToolDefinition` 契约。
- 新 Invocation 自动选择当前 generation 中已安装的 Tool Provider；旧
  Invocation 保持原 generation，Provider 更新不会造成运行中能力漂移。
- Provider 返回的 `ToolDefinition` 由 Runtime 统一注册治理身份并包裹 Tool
  Guard；插件卸载后等待旧 generation lease 排空，再按 Contribution owner
  清理治理身份，不允许绕过治理或中断运行中的 Invocation。
- Tool Guard 的可发现身份仍由进程级 Registry 管理，执行期的 tool type、
  target/pattern parameter、sandbox requirement 和 side-effect class 则固定在每个
  Invocation 工具包装器。同名工具热替换不再导致旧 Invocation 读取新治理元数据。
- Chat Memory 已通过 `memory.provider` Slot 装配；稳定契约由
  `MemoryProvider.open(InvocationScope, MemoryHost)` 返回 invocation-scoped
  `MemorySession`，统一提供 prompt、tools 和 close 生命周期。
- 系统 Workspace Memory Provider 将现有 `BaseMemoryManager` 适配到稳定契约，
  保留 memory tools、自动 recall/persist middleware 和 Workspace 持久化生命周期；
  Runtime 不再直接把具体 memory manager 当作固定实现读取。
- Memory guidance 已成为独立 Prompt Contribution，即使没有 `AGENTS.md` 或用户
  关闭 workspace prompt files 仍然生效；旧文件中的 memory 标记会被移除，避免
  新旧路径重复注入。
- Memory Provider 返回的工具与 Tool Provider 复用同一 Tool Guard 和治理注册
  路径；插件卸载同样等待 generation lease 排空后清理 owner，不留悬挂身份。
- Chat Agent Mode 生命周期已通过 `agent.mode.provider` Slot 装配。系统 Provider
  在 Invocation 开始时固定 Workspace mode 对象快照，Builder 从 Session 获取
  active mode names，Runtime 通过 Session 执行 turn start，不再直接遍历
  `workspace.plugins.modes`。
- `/clear` 与 `/new` 的 conversation reset 已进入同一个固定 Mode Session；旧的
  `CommandHandler` 直接遍历 Workspace modes 仅作为无 Runtime Assembly 调用方的
  兼容回退。单个 Mode reset 失败会被隔离，不阻断其余 Mode 和上下文清理。
- Chat Prompt 已通过 `prompt.provider` Slot 装配。系统 PromptManager、Mode
  Prompt、Memory guidance 与旧插件 Prompt 作为系统兼容 Provider 的一个
  Fragment 接入；原生插件返回类型化 `PromptFragment`，由 Runtime 校验 Provider
  所有权、拒绝重复 ID，并按 `priority + fragment_id` 稳定排序。
- 新 Invocation 自动选择当前 generation 中所有已安装 Prompt Providers；旧
  Invocation 保持原 generation，热替换不会改变运行中的系统提示来源。
- Chat Driver 已通过 `driver.provider` Slot 装配。系统 Provider 在 Invocation
  内只解析一次当前 DriverManager 的工具与 policy hints，并由 Driver Session
  固定；长期 Driver 服务继续由 Workspace 管理，不被单次请求错误关闭。
- Chat Command 已通过 `command.provider` Slot 装配。每个 Invocation 固定静态
  Catalog；结果明确区分未处理、已处理并继续 Agent、已处理并直接响应。系统命令
  保留名不能由旧插件注册，跨 Provider 普通名称冲突会显示歧义错误，不再按安装
  顺序覆盖；动态 Skill fallback 只允许系统 Provider 使用。
- Chat Lifecycle Hook 已通过 `hook.provider` Slot 装配。系统 Provider 在请求开始
  时固定八阶段 Hook Catalog；旧 `HookBase` 只在兼容 Host 内运行，公开 Outcome
  不依赖 AgentScope。跨 Provider 的 `before`/`after` 依赖、priority、短路与
  `SKIP_AGENT` 粘性由统一 Router 执行。
- Chat Stop Gate 已通过 `loop.gate.provider` Slot 装配。每个 Invocation 固定
  Gate Catalog，Router 保留 unscoped、default、Goal/Mission/custom scope 的选择
  语义，并把 `BYPASS`、继续推理和终止收敛为公开契约。工具调用后的停止仍延迟到
  工具执行完成；`/clear` 会重置固定 Gate Session 与旧的 pending gate 状态。
- Artifact Renderer 已通过 `artifact.renderer` Slot 接入 Task Projection 与内容
  API。系统与插件实现使用相同 Port；每次预览固定一个 registry generation，按
  priority 稳定选择，插件异常或不安全输出会回退下一个 Renderer。内联输出仅允许
  JSON、Markdown 和纯文本，且携带 CSP、来源哈希、Renderer ID 与 generation；
  下载保留经哈希验证的原始 Artifact 字节。
- Application、Workspace 和 Plugin Loader 共享同一 Capability Registry，保证
  内置能力与插件能力进入同一 generation 事实源。
- 系统 Task bundle 已迁入独立 `qwenpaw.system.tasks` provider namespace，避免
  与 Chat Workspace Tool bundle 互相原子替换；旧 Task capability ID 在启动
  边界自动映射。所有系统 bundle 具备 namespace 唯一性自动门禁。
- Task Run 的固定 generation 现可安全重入嵌套 Chat Invocation；只有携带内部
  Approval Broker 的 Task bridge 才能指定 generation，外部请求伪造值无效。
  旧 generation 在 lease 排空后不可再次解析。
- Invocation 建立时会完整校验所选 Agent、Mode、Tool、Memory、Prompt、Driver、
  Command、Hook、Gate 与可选 Strategy 的存在性和 Slot，失败时先释放 lease。
  Runtime FINALLY Hook 在所有 Provider Session 存活时执行，随后按逆序隔离关闭，
  单个 close 失败不会阻断其余资源或 generation lease 回收。
- `RuntimeStrategyDirective` 已成为 Task Strategy 到 Runner 的类型化事实；通用
  Task Context 不再把插件 Strategy 参数原样注入 Chat。只有系统 Console Runner
  能将 Default/Coding/Goal/Mission 的精确参数桥接为 Chat 指令，额外字段、伪造
  系统 ID 或插件 ID 均不能激活内置模式。
- `planner` / `strategy` 的公共 SDK 纵切面已经闭环：SDK 公开 Planner 输出所需的
  `PlanStep`，`task-insights` 示例通过同一 manifest 同时声明 Planner、Strategy 与
  Runner。系统与插件均由真实 `TaskRuntimeOrchestrator` 持久化 Plan、固定 Run
  generation 与 Strategy 身份，并把经 JSON/32 KiB 校验的参数交给 Runner；行为
  合同同时核验 Artifact/Evidence producer，不以协议 `isinstance` 代替运行链路。
- Scheduler 的 system/plugin 行为合同已经贯穿完整执行：内置
  `local-durable-scheduler` 与示例 `scheduler-provider.local-durable` 均通过
  generation-pinned Fire lease 创建唯一 Schedule Task，并继续进入共同
  Planner/Strategy/Harness Runner 与 Artifact/Evidence 管线；重复触发只 replay
  已绑定 Task，SQLite 重开后仍可读取 definition。
- Delivery Adapter 的 system/plugin 行为合同已连通真实 Task Ledger、
  Projector、Worker、Dispatcher 与 Receipt Store。内置 Inbox Adapter 与示例
  `delivery-provider.local-jsonl` 共享相同 generation 固定、identity 校验与幂等
  replay 规则；示例只依赖公共 SDK，可通过 production Loader 热安装与
  卸载，而已 pin 的旧 generation 仍可完成投递。

### 正在实施

- Task Strategy 与 Agent Mode 已明确分离：Task `RuntimeStrategy` 保持 `strategy`
  Slot；Chat Default/Coding/Goal/Mission 使用 `agent.mode.provider`。公共 Mode、
  Prompt、Tool、Command、Hook、Stop Gate 与 Artifact Renderer 已进入 Runtime
  Assembly。Approval/Policy/Sandbox/Audit 已共享 Invocation 身份，Side Effect
  已进入 Task Ledger 并通过工具 effect 契约接入真实执行路径；并行审批、取消、
  Runtime 丢失、uncertain Side Effect 审核与 Checkpoint 新 attempt 已形成确定恢复
  路径。I2 代码基建已收口，严格模式真实端到端仍保留为阶段验收；下一步冻结
  Execution Contract 和因果事件信封。
- Tool/Memory Provider 已保留 Tool Guard、Sandbox、MCP、memory middleware 与
  旧 Plugin API 行为；Driver 工具已由 `driver.provider` 进入 Assembly。第三方
  Provider 使用类型化 Tool/Prompt、配置、凭据与统一审批契约，最小 Host 不暴露
  DriverManager、request context 或系统兼容 `load()`；系统 Provider 继续通过私有
  Host 在每个 Invocation 内读取一次现有 DriverManager。

### 尚需迁移

- 原生 Command Provider 的配置选择 UI 尚未完成；旧
  `register_slash_command()` 已能生成去重的 Contribution manifest 迁移草案，
  但 legacy handler 转 Provider factory 仍需人工实现并通过热激活验证。
- 旧 `register_hook()` 仍写入 Workspace Registry，并由下一次 Invocation 的系统
  Provider 快照吸收；现可生成 `hook.provider` manifest 迁移草案，但旧 API
  的变更本身仍没有独立 generation 编号，Provider 代码也不会被自动改写。
- 旧 `register_stop_handler()` 仍写入 Workspace Registry；系统兼容 Provider 会在
  下一次 Invocation 吸收其快照。新插件可直接声明 `loop.gate.provider`，
  迁移命令可生成 manifest 草案；为避免改变 stop 语义，不自动改写处理器。
- Cron/Heartbeat 尚需通过统一 Scheduler Port 创建和恢复 Task。
- 第三方原生 Driver Provider 契约已冻结；Artifact Renderer 公共槽位已接入。
- Chat/Channel 与 Task 的引用、投影和生命周期仍需进一步统一。
- Workstation Runner Pool、Durable Local Scheduler 和资源预算尚未实现。
- Hub tenant-aware Ledger、Object Store、RBAC、远程 Runner 和 Distributed Scheduler 尚未接入共享 Port。

## 13. 当前验证证据

- Chat-first Kernel 定点测试：79 项通过，耗时 1.13 秒。
- Tool Provider 最终相关回归：172 项通过，耗时 2.93 秒，覆盖 Kernel、
  Capability、Runtime、插件生命周期、Task 兼容与热激活。
- Tool Provider system-plugin 行为合同与治理快照扩大回归：291 项通过，
  耗时 6.05 秒。覆盖 manifest 激活、自动选择、RuntimeAssembly、AgentBuilder、
  system/plugin 共同 Guard 包装，以及热替换前后同名工具的治理元数据隔离。
- Memory Provider 最终相关回归：417 项通过，耗时 4.03 秒，覆盖 Kernel、
  Capability、Runtime、现有 Memory 后端、Prompt、插件 generation、卸载回收、
  Task/Lite 兼容与热激活；另有 12 项针对 Prompt 与卸载边界的定点测试通过。
- Memory Provider system-plugin 行为合同定点回归：54 项通过，耗时
  2.54 秒。覆盖显式 Provider 选择、RuntimeAssembly、Builder Session、Prompt
  Contributor、Tool Guard、Agent/Conversation 状态隔离、revision CAS 和私有
  compatibility backend 不可见。
- Memory Provider 相关 Python pre-commit 定点校验通过，包括 AST、mypy、
  Black、Flake8 和 Pylint。
- Agent Mode Provider 扩展回归：296 项通过，耗时 2.68 秒，覆盖 Kernel、
  Assembly、固定 mode snapshot、Goal/Mission/default/custom-loop 生命周期、
  `/clear` reset、插件 Slot、generation 热替换和现有 Tool/Memory 兼容链路。
- Agent Mode Provider system-plugin 行为合同定点回归：101 项通过，耗时
  3.49 秒。覆盖 manifest、配置校验、RuntimeAssembly、真实 Runtime Session、
  active names、turn start、conversation reset、`/clear` 接线和 generation 行为
  隔离；插件 Host 不含 Workspace context、系统 Mode snapshot 或兼容 lifecycle。
- Agent Factory 系统边界扩大回归：365 项通过、7 项按环境条件跳过，耗时
  6.68 秒。覆盖 Slot 稳定性、系统 identity/protocol 门禁、插件 manifest
  `system_slot` 诊断、Plugin SDK 导出边界、Loader/API、RuntimeAssembly 和热激活；
  其他公开 Provider Slot 未受影响。
- Prompt Provider 扩展回归：309 项通过，耗时 3.15 秒，覆盖类型化 Fragment、
  ownership、稳定排序、自动选择、generation 热替换、现有 Prompt Contributors、
  Mode/Memory Prompt 以及 Task/Lite 兼容链路。
- Prompt/Command/Hook system-plugin 行为合同定点验证：29 项通过，
  耗时 2.58 秒。Command 覆盖公共 SDK、manifest activation、固定
  generation、RuntimeAssembly、真实 Runtime 会话打开、系统/插件共同路由与
  插件无动态 fallback；Hook 进一步覆盖稳定目录、真实生命周期打开、
  确定性执行和有来源的上下文注入。
- Stop Gate system-plugin 行为合同定点验证后，与 Prompt/Command/Hook、
  Runtime、Steer 及插件热激活合并回归共 36 项通过，耗时 2.50 秒。
  覆盖 manifest → generation → RuntimeAssembly → 真实 Stop Gate Session，
  并验证工具调用后 safe-point 注入和 conversation reset。
- Driver Provider 扩展回归：714 项通过，耗时 5.00 秒，覆盖 Driver/MCP、凭据、
  审批、旧配置迁移、固定 generation、Tool/Prompt/Memory/Mode 与 Task/Lite
  兼容链路。
- Driver Provider system-plugin 行为合同定点回归：54 项通过，耗时 2.47 秒。
  覆盖 manifest 激活、显式 Provider 选择、RuntimeAssembly、Builder Session、
  Prompt Contributor、唯一 AgentScope Adapter、真实统一审批，以及热替换前后
  已打开 Session 的行为隔离；插件 Host 无系统 DriverManager 与 request context。
- Harness Runner system-plugin 行为合同定点回归：48 项通过，耗时 2.14 秒。
  覆盖公开 manifest、system Contribution、统一 TaskExecutionCoordinator、固定
  generation、RuntimeContext、Ledger 信号、Run/Task 终态，以及 system/plugin
  共用内容寻址 Artifact 与 Evidence Emitter。
- Command Provider 扩展回归：456 项通过，耗时 25.71 秒，覆盖 Runtime 命令、
  固定 Catalog、三态结果、系统保留名、跨 Provider 冲突、动态 Skill fallback、
  generation 热替换、Cron 兼容、Plugin SDK 与 ACP 命令发布链路。
- Hook Provider 扩展回归：418 项通过，耗时 25.34 秒，覆盖完整 Runtime、Loop、
  固定 Hook snapshot、跨 Provider 顺序、短路、`SKIP_AGENT`、取消与 FINALLY、
  generation 热替换和 Plugin SDK 兼容链路。
- Stop Gate Provider 定点回归：175 项通过，耗时 2.25 秒；扩大到 Runtime、Loop、
  Modes、Kernel 与 Plugin SDK 后共 449 项通过，耗时 24.61 秒。覆盖固定 Gate
  Catalog、scope 选择、工具调用后的延迟停止、generation 热替换与 `/clear` reset。
- Stop Gate 相关 Python pre-commit 定点校验通过，包括 AST、mypy、Black、
  Flake8 和 Pylint；后端 `/api/healthz` 返回 `status=ok`。
- Artifact Renderer 扩展回归：155 项通过，耗时 3.16 秒，覆盖系统/插件同一
  Port、安全 MIME 门禁、失败回退、运行中 generation 固定、热替换、Task API、
  Artifact/Evidence 与真实插件加载卸载。新增的核心/API定点组另有 83 项和 7 项
  通过；Tasks 前端 6 项通过，相关 Prettier/ESLint 与 Python pre-commit 均通过。
- Chat Artifact 纵切面复用同一 `ArtifactRef`、`EvidenceRef`、内容寻址 Store 与
  `artifact.renderer`。上传 receipt 在首次发送时原子绑定 Chat，伪造引用、跨
  Chat 读取和不完整引用均 fail closed；直发与消息队列共享同一前端契约。
- 复用真实 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 执行 `/clear`，页面
  显示 “History Cleared!”；随后上传并发送 `README.md`，刷新后恢复附件及
  “Conversation Artifact Chat 正常”。内容端点返回系统安全 Renderer、源哈希、
  sandbox CSP，下载字节与原文件一致；另一 Chat 读取返回 404。
- Chat Artifact 后端定点回归 27 项通过；前端上传、队列和附件定点回归 16 项
  通过，TypeScript `--noEmit`、新增独立模块 ESLint、相关 Prettier、Python
  pre-commit 与 `git diff --check` 通过。Chat 大页面仍有既存 ESLint 债务，
  本轮新增行未引入新的诊断；未执行规范禁止的全量 npm build/test/format。
- Task/Chat/Channel 因果身份纵切面定点回归 173 项通过，耗时
  4.33 秒。新 Run 固定 Invocation，恢复 Run 更换 Invocation 但延续根
  correlation；Runtime Assembly 仅信任内部 Task Broker。Execution Event、
  DeliveryRequest、Channel Message 和 Inbox Projection 保留同一身份及
  Artifact/Evidence 引用。
- 系统 provider namespace、Catalog 共存、generation 重入与 Task Context 定点
  回归共 37 项通过。真实 Chat 返回“Kernel 策略共存正常”；随后 Catalog 的
  Agent、Mode、Tool、Memory、Prompt、Driver、Command、Hook、Gate、Strategy
  与 Renderer 均稳定处于 generation 11，未发生 provider 覆盖或漂移。
- Runtime 生命周期与 Provider 回归 38 项通过，Selection fail-closed 与 lease
  释放回归 17 项通过；相关 mypy、Black、Flake8、Pylint 均通过。固定 Chat 在
  `/clear` 显示 “History Cleared!” 后返回“Runtime 生命周期正常”。
- Strategy 类型化边界相关 53 项回归通过，mypy、Black、Flake8、Pylint 全部
  通过。真实 Coding Strategy Task `1185ea49-d41c-4730-b2ab-29a06bc82eb3`
  返回预期结果并产出 Artifact 与 Evidence。
- 审批状态机与 Tool/Driver/Harness bridge 相关定点回归 407 项通过，耗时
  4.71 秒。并发决策、取消、超时、durable hook 与 Tool、Governance Tool、
  Driver、Codex、Qoder 共用桥接路径均通过；相关 Python pre-commit 通过。
- Kernel 纯度门禁递归覆盖子目录并禁止产品、框架、数据库和文件系统实现反向
  依赖，Kernel 定点回归 51 项通过。Runtime cleanup 可抵抗二次取消，并会在部分
  Provider 装配失败时逆序关闭已经打开的 Session；相关 Provider 回归 35 项通过。
- 现有全部 Contribution Slot 已具备机器可读 SlotContract，明确输入、输出、
  生命周期、失败模式和稳定性。Kernel、插件、Edition 与 Runtime 生命周期组合
  回归 71 项通过；相关 mypy、Black、Flake8、Pylint 全部通过。
- Sensor Proposal 已从直接调用 TaskService 迁入 run-scoped TaskApprovalBroker。
  Tool、Driver、Codex、Qoder 与 Proposal 的 durable 审批只剩同一个 Broker 入口；
  Proposal/API 相关定点回归 43 项通过，相关静态检查通过。
- Runtime 所有的 Invocation/Correlation identity 已贯穿 Governance ToolCallSpec、
  Sandbox escalation、SQLite Audit 与 durable ApprovalRequest。外部 payload 中的
  伪造 identity 会被覆盖；旧 audit.db 原位加列且不丢历史。身份链定点回归
  339 项通过，新增纵切面 17 项通过，相关静态检查通过。
  固定 generation 11，返回“Strategy 类型边界正常”，并产生 1 个 Artifact 和
  1 条 Evidence；未通过 Task 页面进行开发或验收。
- Capability Registry 新增“清除同 provider 遗留能力”的热替换回归测试。
- 真实 Console Chat 会话
  `ca0ea4be-966d-4b52-a710-d35366aac227` 返回
  “Kernel Chat 链路正常”。
- 真实 Tool Provider Chat 会话
  `28be71d0-fb78-44e1-bc5e-54e8804b8975` 展示 `获取 当前时间` 工具活动，
  并返回 `2026-09-24 10:49:54 Asia/Shanghai (Thursday)`。
- 最终代码热重载后的 Chat 会话
  `a4553747-b653-4509-878f-2e2f446f9a39` 再次完成工具调用并返回
  `2026-09-24 10:57:30 Asia/Shanghai (Thursday)`。
- Memory Provider 完成后的真实 Chat 会话
  `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 通过新 Kernel/Runtime 装配链路完成
  1 个执行步骤并返回“Kernel Memory Chat 正常”。
- 同一会话随后执行 `/clear`，页面回到空白欢迎态；复用该会话再次经过
  Agent Mode Provider 链路完成 1 个执行步骤并返回
  “Agent Mode Provider Chat 正常”。
- conversation reset 迁移完成后，在相同会话再次执行 `/clear` 并确认欢迎态，
  随后的 1 个执行步骤返回“Mode Session Reset 正常”。
- Prompt Provider 迁移后继续复用同一会话，执行 `/clear` 后的 1 个执行步骤返回
  “Prompt Provider Chat 正常”。
- Driver Provider 迁移后继续复用同一会话，执行 `/clear` 后的 1 个执行步骤返回
  “Driver Provider Chat 正常”。
- Command Provider 迁移后继续复用相同会话，`/clear` 直接返回 “History
  Cleared!” 并列出 summary、Memory、Plan 已清理；随后普通消息继续进入 Agent
  并返回“Command Provider Chat 正常”。
- Hook Provider 迁移后继续复用相同会话，先执行 `/clear` 并确认清理结果，随后
  完整对话链路返回“Hook Provider Chat 正常”。
- Stop Gate Provider 迁移后仍复用会话
  `1ee31988-b37a-48b9-b6ce-423c52f6a3a9`；`/clear` 返回 “History Cleared!”
  并确认 summary、Memory、Plan 已清理，随后 1 个执行步骤返回
  “Stop Gate Provider Chat 正常”。
- 真实 Task Workbench 打开 Task
  `1a089e4a-1a77-4bcd-ac0c-ad5d95bcfb8f`，成果卡明确显示系统 Renderer，点击后
  读取真实 Artifact 内容“统一运行策略已生效”，并显示绑定的 Evidence 与 100%
  覆盖率；Projection 同时返回 Renderer ID 与 generation 2。
- `examples/plugins/chat-tool-provider` 通过 CLI manifest 校验，并由公共 SDK
  契约测试完成 generation 激活、自动选择和工具定义解析。
- 本轮 Python pre-commit 定点校验全部通过；`git diff --check` 通过。
- 后端定点测试：242 项通过，耗时 3.27 秒。
- 前端 Tasks/API 定点测试：38 项通过。
- 涉及文件的 Python pre-commit 定点校验通过。
- 前端定点 Prettier / ESLint 校验通过。
- `git diff --check` 通过。
- 未执行规范禁止的全量 `npm run build/test/format`。
- 默认 Strategy 真实任务：Task `681ac448-c179-478d-a80f-3739b4642c47`。
- 类型化 Broker 真实任务：Task `e66f5f08-279f-4b2d-b0fa-ee7e2c4f9a0f`。
- 取消审批真实任务：Task `420a8fe2-29dc-483b-bd28-edead70c0f47`，事件顺序为 `approval.requested -> approval.decided -> run.cancelled`。

这些证据证明当前 OS 纵切面的定点行为，不代表 Workstation、Hub 或全量生产回归已经完成。

## 14. OS 路线图与验收

### R0：稳定 OS Kernel 与 Chat-first 纵切面

- [x] Task/Run/Plan/Event/Approval/Artifact/Evidence 契约建立。
- [x] 内置与插件 Runner/Planner/Strategy 进入共同 generation。
- [x] Task Workbench 展示真实计划、消息、审批和产物。
- [x] 建立不依赖 Task 的 InvocationScope 与共享 Capability Registry。
- [x] Chat 通过固定 generation 的 `agent.factory` Slot 正常构建并回复。
- [x] `agent.factory` 标记为 system-only 并从 Plugin SDK 移除；插件声明在加载前
  返回结构化 `system_slot` 诊断，系统实现继续经过 identity/protocol 激活门禁。
- [x] 保留现有 AgentBuilder 行为，并收敛为系统兼容适配器。
- [x] Tool Provider 通过 Runtime Assembly 解析，旧 tools 目录作为适配器。
- [x] Memory Provider 通过 Runtime Assembly 解析，旧 memory 实现作为适配器。
- [x] Agent Mode 生命周期通过 Runtime Assembly 解析并固定 Workspace mode 快照。
- [x] Prompt Provider 通过 Runtime Assembly 解析，旧 PromptManager 作为适配器。
- [x] 系统 Driver 通过 Runtime Assembly 解析并固定请求级工具与提示快照。
- [x] ReAct Stop Gate 通过 Runtime Assembly 解析并固定请求级 Gate Catalog。
- [x] Artifact Renderer 通过共享 generation 解析并安全投影真实 Task 产物。
- [x] Chat 与 Task 共用 Artifact/Evidence、内容 Store 和安全 Renderer 契约。
- [x] Task Strategy 固定在 Task Run generation，并通过类型化 RuntimeContext
  向 Chat Invocation 传递有界参数；Chat 会话模式继续由 Agent Mode Provider
  管理，不混用生命周期。
- [x] Tool/Memory/Driver/Harness/Scheduler 完成统一 Port 与 Contribution 接入。
- [ ] Chat、Channel、Cron 与 Task 生命周期完成兼容迁移。
- [ ] 插件热安装、失败回滚、代际排空完成端到端验收。
- [x] SDK 示例可由新开发者独立完成开发、校验、安装和卸载。

### R1：Workstation 产品化

- [ ] Durable Local Scheduler 可在进程恢复后继续调度。
- [ ] Runner Pool 支持 Native Agent、Codex、Qoder 的统一选择。
- [ ] Workspace 级并发、预算、缓存和资源限制可观察、可治理。
- [ ] Process/Container 隔离可配置，失败不污染控制面。
- [ ] Workstation 不新增或复制 Kernel 领域模型。

### R2：Hub 产品化

- [ ] Tenant-aware Ledger、Artifact Store、Capability Resolver 完成。
- [ ] RuntimeService/Provisioner 作为远程 TaskRunner 接入。
- [ ] RBAC、审计、配额、签名插件和策略中心完成。
- [ ] Distributed Scheduler、重试、幂等和高可用完成故障演练。
- [ ] Lite/Workstation 创建的公共 Task 契约可由 Hub 读取和迁移。

### 总体验收门禁

- [x] Kernel 依赖纯度测试持续阻止框架和产品实现反向侵入：AST 门禁扫描全部
  `src/qwenpaw/kernel/**/*.py`，只允许标准库、Pydantic 与单层 Kernel 相对导入；
  产品层绝对导入和跨目录相对导入均由合成反例验证为失败关闭。
- [x] 内置与插件对每个公共 Slot 具有同协议、同治理、同失败语义。
  Capability Registry 已统一 system/plugin activation 门禁，发布前共同校验 Protocol、
  capability identity 与 UI entrypoint；`prompt.provider`、`command.provider`、
  `hook.provider`、`loop.gate.provider`、`tool.provider`、`memory.provider`、
  `driver.provider`、`harness.runner` 与 `agent.mode.provider` 已通过 manifest →
  generation → 真实消费端的 system/plugin 行为合同；`planner`、`strategy` 与
  `runner` 也已通过同一真实 Orchestrator 行为合同，`scheduler` 已通过真实 Fire
  dispatch 与 replay 合同，`sensor`、`delivery.adapter` 与
  `artifact.renderer` 也已通过各自的 system/plugin 真实链路合同。UI experience
  Slot 已由 Console 真实 bundle loader 执行 manifest 声明、plugin identity、同步注册、
  失败回滚和延迟注册拒绝合同；宿主默认内容与插件 fill/replace 经同一
  `Slot` 消费端验证。
- Sensor 已从无上下文的手动轮询升级为 generation-pinned `SensorContext`：公共
  Host 注入 Agent 身份并限制 32 KiB trigger / 25 个 Proposal，Proposal source
  必须匹配 capability ID。内置 proactive memory 通过 system Sensor Adapter 进入
  与插件相同的 Task、Run 和 pending Approval Ledger；旧 `propose()` 插件仍由兼容
  路径支持。system/plugin 行为合同已验证 generation、审批 requester 与事件顺序。
- [ ] 所有高风险执行均可追溯到审批、策略和审计记录。
- [ ] 所有运行产物均有归属、摘要和可验证 Evidence。
- [x] Profile 缺失 Adapter 时准确 fail closed，不回退到其他产品配置。
  Workstation/Hub 缺失的部署 Adapter 以结构化清单拒绝启动；Chat Runtime
  缺失 Host-owned Capability Registry 或选中的 capability 时均直接失败，
  不再现场创建隐式 Registry 或退回 Lite 默认实现。
- [ ] 主要功能 Code Review 无 Blocking finding。

## 15. 关联文档

- `docs/design/qwenpaw-agent-os-fused-architecture.md`
- `docs/design/qwenpaw-unified-task-runtime.md`
- `docs/design/qwenpaw-task-runtime-contract.md`
- `docs/design/qwenpaw-lite-agent-os.md`
- `docs/design/qwenpaw-lite-migration.md`
- `docs/development/plugin-quickstart.md`
