# AI Agent Handbook 对 QwenPaw 3.0 的吸收评估

状态：Evidence-backed design input

日期：2026-10-01

上游资料：[`aliyun/ai-agent-handbook`](https://github.com/aliyun/ai-agent-handbook)

核对版本：`6d12dd2dc006eefd0f89f213c4e0ca2edfe7e9a8`

## 1. 结论

这份 Handbook 对 QwenPaw 3.0 最重要的价值不是提供更多功能清单，而是给出
一条更清晰的责任边界：

> QwenPaw 3.0 应被定义为“产品化 Harness + Agentic OS Substrate”，而不是把
> Harness、Runtime、Sandbox、Task 语义和平台治理全部称为 OS Kernel。

两部分分别负责：

- **Productized Harness**：Agent Contract、Prepare / Model / Act / Observe /
  Verify Loop、Context Policy、Plan、阶段门禁、能力选择和完成验证。
- **Agentic OS Substrate**：Agent Identity、Run、Session / Conversation、
  Workspace、Capability、Budget Lease、Policy、Evidence、Checkpoint 和强制执行点。

现有 `kernel/` 暂时可以继续作为稳定公共类型的物理目录，但类型和 Port 必须标注
逻辑归属，避免任务目标、业务验收和上下文策展策略继续无边界地下沉。

## 2. 为什么值得吸收

Handbook 的 2026 调研与 QwenPaw 的产品机会高度一致：

- 只有 18% 的受访企业真正部署 Agent 到生产环境，说明差异化不在“能对话”，
  而在可靠运行和治理。
- 90% 的企业有上下文与记忆需求；多 Agent 最大痛点是状态与上下文衰减。
- 63% 把多模型路由与自动降级列为急需能力。
- 55% 仍靠人工抽查评估，基于轨迹自动评估的比例不足 8%。
- 73% 期待端侧与开源运行底座。
- 57% 采购 Coding Agent，78% 使用通用 Agent Framework，近四成同时使用
  两者；市场碎片化意味着可迁移契约比绑定单一 Harness 更有价值。

这组数据支持 QwenPaw 3.0 聚焦：

1. 本地优先、可自托管的产品化 Harness；
2. 内置和插件同契约的可迁移能力层；
3. 可干预、可恢复、可验证、可审计的长任务运行底座；
4. Chat-first，而不是先建设一个孤立的 Task 管理产品。

## 3. 与当前 3.0 设计的对齐情况

### 3.1 已经高度对齐，不需要重新设计

| Handbook 原则 | QwenPaw 当前证据 | 结论 |
|---|---|---|
| 稳定 Loop + 生命周期扩展点 | Hook、Loop Gate、Middleware safe point | 保留，继续避免在主 Loop 堆条件分支 |
| 消息不是唯一任务状态 | Ledger、Queue Projection、Interaction、Checkpoint | 已建立权威状态，不回退到前端猜测 |
| 能力发现与执行授权分离 | Capability Registry + Tool Guard + Approval | 已对齐 |
| 内置和外部能力同治理 | system/plugin Contribution、generation pinning | 已对齐，属于核心差异化 |
| 模型只能申请完成 | Artifact / Evidence / Verification completion gate | 已对齐 |
| Event Log、Snapshot、Checkpoint 不等价 | Ledger、Projection、safe checkpoint | 语义已基本具备，应补文档命名 |
| Steering / Interrupt 在安全点生效 | Queue / Invocation Control、reasoning/tool safe point | 已对齐 |
| 大结果退出上下文但不退出任务 | Artifact Store、内容引用、安全预览 | 基础已具备，Chat 工具产物链仍在闭环 |
| 运行中版本固定 | registry generation + lease | 已对齐，是插件热更新的关键优势 |
| Lite / Workstation / Hub 共享契约 | Edition Profile + fail-closed adapter | 已对齐 |

### 3.2 当前有零散能力，但缺少统一公共契约

| 缺口 | 当前碎片 | 应吸收的统一设计 |
|---|---|---|
| Action Plane | `ToolDefinition`、Driver Tool、Tool Guard、Approval、Side Effect 各自表达部分语义 | `ActionRequest` / `ActionResult` / `ActionStatus`，覆盖 Tool、Shell、Browser、Driver、MCP 与 Remote Agent |
| Context 可解释性 | `PromptFragment` 只有 ID、文本、优先级 | `ContextPolicy` + `ContextManifest`，记录来源、版本、信任级、选择原因、变换、哈希与 Token |
| Environment | project dir、Sandbox policy、Harness environment 分散 | `EnvironmentContract`，声明挂载、网络、凭据引用、依赖、配额、快照、审计和清理 |
| Budget 与授权联动 | `ExecutionBudget`、Usage Scope 和权限原为独立对象 | Lite 已实现进程内可派生、可撤销的 `BudgetLease` 并复用持久 Usage Ledger；Workstation / Hub 再增加分布式租约与 fencing |
| 业务 Outcome | `run.completed` 与 Verification 证明执行完成 | 增加 `Outcome` 投影，区分执行成功、业务验收和后续动作 |
| 资产发布 | Capability generation 解决运行版本，Plugin lifecycle 解决安装 | 增加 Release / Lock Manifest，区分逻辑资源、发布版本、运行引用和派生索引 |
| 轨迹评估 | 已有 causal event，但没有稳定 Trajectory 数据产品 | 从 Event / Context Manifest / Action / Evidence 派生 Trajectory，后续用于 Badcase 与回归 |
| 通信语义 | HTTP、SSE、Queue、Ledger 与 Continuation 已分别存在 | 冻结 `CommunicationContract`，分开声明调用、流、任务句柄和续传能力，不从传输方式推断恢复语义 |
| 插件发布证据 | shadow generation 已有结构、身份与健康检查 | 增加风险分级的 Scenario / Evidence / Evaluation 门禁；验证通过后才原子晋升，不把“可导入”当作“可发布” |

### 3.3 当前模型需要收紧的地方

#### OS 不持有业务任务语义

Handbook 的三条下沉判据值得成为 QwenPaw 的架构门禁：

1. 多个应用重复实现，且差异不产生业务价值；
2. 必须由被约束方之外的组件执行才有效；
3. 需要独立证据来源才能验证。

目标、计划内容、任务终止条件、业务验收规则和 Context 策展策略不满足上述
条件，不应被描述为 OS 基础设施。OS 可以保存和执行这些契约，但不应替应用决定
其内容。

#### Task、Run、Session / Conversation 必须继续分离

- `Task`：围绕可验收目标的业务执行对象。
- `Run`：一次可暂停、恢复、失败和重试的执行尝试。
- `ChatSpec.id`：人机交互与消息分叉的稳定 Conversation 身份。
- `Invocation`：一次实际运行时调用。
- `session_id`：仅保留为 Provider 或传输适配身份，不再承担领域主键。

Handbook 使用 Session 作为交互边界；QwenPaw 应继续以 `ChatSpec.id` 表达这一
语义，而不是重新把公共 API 改回 `session_id`。

#### 不通过扩张 TaskStatus 表达所有等待原因

Handbook 给出了 `WAITING_INPUT`、`WAITING_APPROVAL`、`WAITING_EVENT`、
`VERIFYING` 等状态。QwenPaw 不应机械照搬为越来越大的状态枚举，而应：

- 保持 Task / Run 生命周期紧凑；
- 用 `InteractionRequest`、`WaitCondition`、`VerificationRecord` 表达正交原因；
- 在 Projection 中派生用户可见状态；
- 仅当某个状态改变合法转换和恢复语义时，才进入核心状态机。

这能避免 Ask User、Approval、后台 Tool、Suggestion 和外部事件各自创造平行状态机。

## 4. 建议吸收的十一项设计

### A1. 明确 Productized Harness 与 OS Substrate

优先级：P0，先改设计，不搬目录。

逻辑归属建议：

```text
QwenPaw Product
├── Experience / Channel
├── Productized Harness
│   ├── Agent Contract
│   ├── Prepare / Model / Act / Observe / Verify
│   ├── Context Compiler
│   ├── Plan / Phase Gate / Verifier
│   └── Capability Selection
└── Agentic OS Substrate
    ├── Run / Conversation / Workspace
    ├── Capability / Policy / Budget Lease
    ├── Interaction / Queue / Interrupt
    ├── Event / Checkpoint / Artifact / Evidence
    └── Runtime / Sandbox / Storage adapters
```

物理代码继续遵循 `Experience -> Application -> Kernel Ports <- Adapters`，等 API
稳定后再决定是否拆成 `kernel/harness/` 与 `kernel/os/`，避免为概念调整做大规模
文件迁移。

### A2. Context Compiler 与 Context Manifest

优先级：P0，Chat-first 基建。

新增稳定类型：

```text
ContextFragment
├── fragment_id / source_type / source_id / version
├── scope / trust_level / permission_basis
├── priority / selected_reason
├── transform / content_hash / token_count
└── content or stable reference

ContextManifest
├── invocation_id / model_call_id
├── context_policy_id / registry_generation
├── selected fragments / omitted fragments
├── capability and tool disclosure snapshot
└── context_fingerprint
```

现有 `PromptFragment` 应兼容升级为 `ContextFragment` 的 system-prompt 子集。模型
调用前由 Host 编译，插件只能贡献带来源的 Fragment，不能直接覆盖最终 Prompt。

验收：同一 Chat 在 `/clear` 前后可解释模型看到了什么；插件热替换后旧 Invocation
的 Manifest 不漂移；敏感内容只保存哈希或受控引用。

### A3. 统一 Action Plane

优先级：P0，在完成当前 Chat Artifact/Evidence 捕获后推进。

截至 2026-10-01，Tool、Driver 与 Browser 三条垂直切片已经完成。内置与插件 Tool 共用
稳定 Action 契约、执行前不可变请求、执行后内容最小化结果以及
Artifact/Evidence 引用；系统与插件 Driver 共用 Driver Action、动态审批关联和
策略拒绝语义。MCP 作为 Driver 协议已经随 Driver 接入，旧 Driver Manager 路径也
通过兼容 Adapter 留证。统一与兼容 Browser 实现都在外层受治理执行边界显式声明
`ActionKind.BROWSER`；插件可通过同一 `ToolDefinition.action_kind` 接入，Host 不按
工具名或 Policy 名猜测。Browser 内部逐方法副作用分类与溢出输出 Artifact 关联仍由
Browser 子系统继续闭环；其中溢出输出 Artifact 关联已经完成，逐方法副作用分类仍待
收敛。本地 Codex/Qoder Harness Remote 已在 provider-neutral Event 边界接入同一
Action Plane：每个受控 Chat turn 固定 capability generation，Action 引用调用前
持久化的 EnvironmentResolution，审批型动作在恢复 Provider 前先记录 Request 和
Approval Link，完成/拒绝/失败/取消均生成内容最小化 Result。没有 Provider 审批回调
的动作最早只能在 `TOOL_STARTED` 被观察，不能伪称为宿主执行前拦截；Hub remote
runner 与独立 Runtime attestation 仍属于后续迁移范围。
MCP 标准 `readOnlyHint=true` 会被保留并映射为低风险无副作用 Action；服务未声明
注解时保持 `external_write/high` 的保守默认值，禁止根据工具名称猜测权限。

固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 已在 `/clear` 后完成真实
Browser 可见链路：访问 `https://example.com`，记录 7 个步骤和两次 Browser 调用，
最终返回 `BROWSER_ACTION_E2E_OK: Example Domain`。Chat 现可通过有界只读
`GET /api/chats/{ChatSpec.id}/actions` 查询脱敏 `ActionRecord`；API 边界会再次
清理旧版本持久化的内容字段并重算投影哈希，不暴露私有目录或历史 Browser code。

同一固定 Chat 还完成了 Browser 溢出实测：1100022 字节 stdout 超过 1048576 字节
控制帧上限后，Action 保持失败语义，同时发布 1 个 `browser.output` Artifact 和
1 个 Evidence；Artifact 绑定 ChatSpec.id、Invocation 和 generation 11。内联预览按
256KB 上限返回 413，附件下载返回 200 和完整 1100022 字节，下载响应的源内容哈希与
Artifact 哈希一致。

```text
ActionRequest
├── action_id / invocation_id / parent_event_id
├── capability_id / version / registry_generation
├── actor / purpose / stage
├── arguments / expected_output_schema
├── environment_ref / resource_scope
├── effect / reversible / risk
├── idempotency_key / timeout
└── approval / audit requirements

ActionResult
├── status: succeeded / failed / partial / unknown / cancelled
├── observation
├── artifact_refs / evidence_refs
├── error_code / retryability / side_effect_status
├── executor identity / environment / cost
└── candidate_state_patch
```

Tool、Driver、MCP、Shell、Browser 和 Harness Remote Action 都适配到这一管线；
`ask_user`、Approval、Suggestion 继续属于 Interaction Plane，不伪装成 Tool。

Host 统一提交 Task / Conversation Patch，第三方能力不得直接修改权威状态。
Context 的相关性和信任等级也不能直接转化为执行授权：`ActionRequest` 必须保留
所依据 Fragment 的来源和 trust label，Policy 再根据调用主体、资源范围、风险与
当前授权独立判定。来自网页、工具结果或远端 Agent 的内容即使已进入模型上下文，
仍不能扩大可执行权限。

### A4. Environment Contract

优先级：P1。2026-10-01 已冻结 Kernel 契约并接入 Lite Chat 本地解析器。

契约包含：镜像 / OS / 架构、文件挂载与读写范围、网络出入站、Secret 引用、
依赖和版本、CPU / 内存 / 存储、超时与并发、快照需求、审计与清理策略。

这会把当前分散在 `project_dir`、Sandbox、Harness 与 Hub runtime 的假设统一起来，
也使 Workstation / Hub 的差异落在 Adapter，而不是 Task 领域模型。

当前垂直切片包含 `EnvironmentContract`、`EnvironmentResolution`、
`EnvironmentRef`、host-owned `EnvironmentResolver` / `EnvironmentStore`：

- 每次 Chat Invocation 在 Agent 建立前解析并写入不可变环境证据；不满足时在
  模型或工具运行前失败关闭，并释放 generation lease。
- Lite 只承诺能在本机证明的 Host isolation、继承网络、Workspace/Mount
  读写和依赖存在性；Sandbox/Container、网络隔离、Secret 注入、硬资源限额、
  快照和自动清理不会被静默忽略。
- Action 只保存 `EnvironmentRef`，通过 resolution identity 关联同一次调用的
  环境事实，不复制挂载路径或凭据。
- 旧 Sandbox 已通过 Action 级 Adapter 接入：文件可见性、挂载、拒绝路径、
  网络/端口、环境变量名、依赖、资源上限、超时和平台约束先转换为不含 Secret
  值的 Contract，再依据具体后端的真实 enforcement 集合生成 Resolution。旧逻辑
  仅警告的 `max_memory_mb`、`max_processes`、domain allowlist 等约束现在会在
  Shell 执行前失败关闭。
- 固定 Chat 的当前全局配置为 `security.sandbox_enabled=false`；真实 `pwd` 回归
  因此正确保留 Invocation Host EnvironmentRef，没有伪造 Sandbox Resolution。
  Sandbox 开启态的真实执行验收仍待在不修改用户全局配置的隔离环境完成。
- Plugin SDK 公开环境数据模型供 Provider 读取，但不公开 Resolver/Store；
  环境兑现属于 Kernel 与 Edition 基建，不是可任意热替换的外设插件。
- 本地 Codex/Qoder Harness 已在受控 Chat Invocation 中接入同一环境管线。
  `EnvironmentEvidenceLevel` 区分 `host_verified`、`provider_declared` 和
  `provider_attested`；当前 Codex sandbox 与 Qoder permission 只能构成 Provider
  声明，Workspace、Skill mount 和 MCP stdio 依赖才由本机独立校验。声明约束与
  enforced constraints 分栏持久化，缺失 MCP 命令或未知模式会在 Provider 调用前
  失败关闭。

固定 Chat `1ee31988-b37a-48b9-b6ce-423c52f6a3a9` 已在 `/clear` 后完成真实
`read_file` 验收：macOS arm64 Lite Resolver 在模型运行前生成 `satisfied`
Resolution，JSON 权限为 `0600`；Tool Action 的 Invocation、ChatSpec.id、
contract/version、resolver 和 resolution identity 全部与环境证据一致。

尚未完成：Harness Remote 与 Hub runner 的 attested Adapter，以及
Workstation/Hub 对隔离、网络、资源、快照和清理约束的真实兑现。完成这些之前，
不能宣称 Environment Plane 全量完成。

### A5. Budget Lease

优先级：P1；Lite 进程内实现已接入，Workstation / Hub 分布式实现待办。

当前 `ExecutionBudget` 是额度合同，Usage Meter 是计量器，Capability lease 是版本
租约，三者都不是 Handbook 所说的可派生授权租约。新对象应绑定：

- 调用额度和资源上限；
- 可执行能力和资源范围；
- 有效期、撤销和父子派生关系；
- Task / Run / Agent identity；
- 子任务终止时的级联收回。

Lite 现已把这三个角色分开：Execution Contract 保存静态额度，Task Ledger 保存不可
回滚的实际用量，`BudgetLease` 决定一次运行及其后代是否仍获准消费。子 Agent 经
Usage Scope 获得独立 Lease identity，可预留本地额度、释放未用额度并接受根级联撤销；
超额用量仍先落 Ledger，再把 Lease 标记为 exhausted。进程重启只从累计
`UsageSnapshot` 重建根准入，不恢复旧 Lease identity。

不要把它实现为另一个前端 Queue，也不要把 Lite 的进程内实现宣称为跨主机 Lease。
Workstation / Hub 仍需补 TTL、fencing token、权威租约存储和跨节点级联撤销。

### A6. Release / Lock Manifest 与渐进式能力披露

优先级：P1。

Capability Registry 已经解决运行时 generation，但还应区分：

- 逻辑资源；
- 不可变发布版本；
- 稳定标签；
- 当前运行引用；
- 可重建发现索引。

任务或阶段开始时生成 Lock Manifest。核心能力固定解析；长尾 Tool、Skill、MCP
和 Remote Agent 可以动态发现，但相关性不等于使用资格，最终仍受 Profile、Policy、
风险和 generation 约束。

Lite 先做本地 Registry 与关键词发现，不引入 Nacos、向量数据库或互联网 Federation。

### A7. Outcome、Trajectory 与 Evaluation

优先级：P2，但数据契约现在就要预留。

- `Verification`：一次任务是否满足声明的验收条件。
- `Outcome`：业务世界是否获得预期结果，可能在 Run 完成后写回。
- `Trace`：运行事实和调用链。
- `Trajectory`：从 Trace、Context Manifest、Action、状态迁移和 Evidence 派生的
  可评估行为序列。
- `Evaluation`：跨样本、跨版本比较 Harness，而不是替代单次 Verifier。

先保证事件携带稳定因果 ID、版本和证据引用，再建设自动评分与仿真界面。

截至 2026-10-08，Lite 已提供第一版可回放 Trajectory：按 ChatSpec + correlation 从
现有 Observation 聚合 Model、Action、Submission、Steer/Interrupt、Interaction、
Artifact、Evidence、Verification、Wait/Recovery、Compaction，并追加完整 Outcome
supersession 历史。派生索引只保存 source pointer，通过固定 watermark 正序分页；它
不是第二套事件库，也不包含隐藏推理。无 correlation 的 legacy 事实保持缺失，不使用
时间邻近或文本相似度进行猜测关联。

### A8. 语义观测与独立 Runtime Evidence

优先级：P1；契约现在冻结，先复用现有 Ledger 和 Middleware。

当前 QwenPaw 已有 AgentScope 流事件、Task 因果事件、Operational Event 和
Langfuse Tool Span，但它们还不是一套跨 Harness 可比较的语义观测契约。应统一
最小语义类别：`MODEL`、`ACTION`、`CONTROL`、`GUARDRAIL`、`COMPACTION`、
`HITL`、`INTERRUPT`、`VERIFICATION`，并明确：

- 模型生成 Action 意图与系统实际执行是两个事件；
- Approval 的等待时间不计入 Action 执行耗时；
- 跨请求 HITL 等待结束当前 Trace，恢复时以因果关系连接新 Trace；
- Interrupt 记录发起者、原因、安全点和已经产生的有效结果，不伪装成失败或完成；
- Runtime 从进程、文件、网络或远端执行器采集的事实是独立 Evidence，不能只信
  Tool / Agent 自报成功。

Lite 不新建重型 Observability 服务，先从现有 Event / Context Manifest /
Action Result 派生语义投影；Workstation / Hub 再接 OTEL、集中审计和成本归因。
运行控制使用同一原则：Control Command/Receipt 是权威事实，Steer、Cancel 与
Reorder 派生 CONTROL，Interrupt Current 与 Stop and Clear 派生 INTERRUPT；
指令和回执正文不进入观察 API，只公开存在性、状态、revision 与安全点。

截至 2026-10-01，COMPACTION 也已按该原则落地：Kernel 冻结内容最小化的
`CompactionRecord` / Store Port，Lite 文件 Adapter 和 Runtime recorder 覆盖自动、
手动与上下文溢出恢复。记录只保留策略、触发原因、前后数量、淘汰/折叠、上下文
变化和安全错误码，不保存消息、摘要、用户提示或异常正文。真实 Chat 验收发现
`/compact` 在 Agent 构建前由 standalone Command Handler 执行，原实现会绕过 Agent
recorder；该旁路现已接入同一记录器。当前固定 Chat 仅占 0.1% 上下文，执行
`/compact` 是真实 no-op，因此不生成虚假的 COMPACTION 成功记录；material、failure
和 overflow 路径由定点契约测试验证。这个结果把 Handbook 的“所有执行入口使用
同一观测强制点”从原则落实成了回归门禁。

VERIFICATION 随后也接入同一语义面，但继续以 Task append-only Ledger 为唯一事实
源：`VerificationHistoryPort` 只回放与 ChatSpec.id 关联的 Task 事件，兼容新
`conversation_id` 和旧 `chat_id` 元数据。Host 从 Execution Event 补齐 Invocation、
correlation、generation 与可信事件时间；观察 API 只提供 verifier、通过/失败计数和
Artifact/Evidence 数量，不公开 acceptance criterion、reason 或插件 metadata。
因此 Runner 发出“完成”或模型声称“已验证”都不能产生 VERIFICATION 观察项，只有
通过 Ledger 完整性检查的 `verification.completed` 才能成为独立 Evidence。

跨来源时间线现已增加固定水位分页。Lite 维护内容零拷贝的派生索引，只保存
Observation identity、权威 source pointer、UTC 排序键与单调索引序号；事实内容仍在
Model Call、Action、Interaction、Control、Compaction 和 Task Ledger 中。第一页固定
索引水位，后续新增或为既有请求补出的 Evidence 不会进入旧快照；相同时间戳用稳定
Observation identity 排序。该实现移除了原来每类最多扫描 1000 条的正确性上限，
同时保留旧列表 API 作为第一页兼容视图。

阻塞 Interaction 现已投影为稳定 `WaitCondition`：Approval 与 Ask User 共享
waiting / satisfied / expired / cancelled 生命周期，只公开 ChatSpec.id、Invocation、
source identity、revision 和 `ContinuationRef`，不复制 prompt、选项或回答内容。
Suggestion 明确不属于 WaitCondition。普通 Chat Ask User 仍如实标记为
`live_invocation`；Task Approval 则携带 Ledger 中真实 checkpoint pointer，投影为
`checkpoint`。`ContinuationRef.availability` 进一步标记 live waiter / resolution
hook 是 `attached` 还是 `detached`，避免把孤立的开放请求误报为活跃执行。

Task Approval 已完成第一条跨进程 continuation：审批决定先提交；并行审批等到最后
一个 blocker；随后以原始 run_id fencing 孤立 Run，并用稳定幂等键从审批 Checkpoint
创建新 Run。恢复失败时决定和 Checkpoint 均保留，Task 停在可人工重试的 failed，
不会伪装为 running。Task API 和 Chat Interaction response 均进入这套状态机；Chat
入口先提交 Ledger 决定，再关闭 Interaction，并由 Interaction 独占 live waiter
交付，避免双重 resolve。普通 Chat Ask User 还没有可序列化 Tool continuation，
运行中也尚未主动释放计算资源，这两项仍不能宣称完成。

### A9. Model Call Plane 与显式 Route Decision

优先级：P1；Lite 先做记录，不先做复杂路由器。

Handbook 调研中，多模型路由与自动降级是比例最高的网关需求之一。QwenPaw
3.0 应把每次模型尝试建模为 `ModelCallAttempt`，把选择建模为
`RouteDecision`，至少记录：请求目的、逻辑模型约束、实际 Provider / Model、
策略版本、选择原因、超时、成本、失败分类、是否重试或降级，以及所使用的
`ContextManifest`。

Lite 默认仍是确定性的单 Provider 直连；重试和降级必须形成新的 Attempt，不能
在 Adapter 内静默切换。Workstation / Hub 再实现按能力、延迟、成本、数据边界和
健康度的路由。这使 Harness、上下文和模型故障能够分别归因，也避免把 Provider
偶发问题误判成 Agent Loop 设计缺陷。

截至 2026-10-01，Lite 已冻结 `RouteDecision`、`ModelCallAttempt`、
`ModelCallResult` 与 Store Port，并接入真实 Provider 边界。每个 ContextManifest
建立独立调用 Session；底层 Provider 每次网络尝试在发送前写入 route/attempt，按
实际调用顺序区分 primary、same-model retry、fallback 与 overflow retry，流式终态、
取消、错误分类和 Provider usage 在完成后留证。Route 同时区分逻辑请求与实际
Provider/Model，Attempt 记录实际 Adapter、Formatter 及版本；Provider 不返回价格时
显式记录 `cost_unknown=true`，不把未知成本伪装为零。Chat 通过所有权校验后的
`GET /api/chats/{ChatSpec.id}/model-calls` 查询内容最小化记录。当前仍未实现按成本、
健康度或数据边界自动选路，也不把既有静态 fallback 配置冒充智能路由器。

截至 2026-10-02，`WAIT_RESOURCE` 已从结果标签推进为可执行基础设施。Lite 新增
独立 `ModelResourceWait` 与 SQLite source of truth：限流使用 timer，额度耗尽等待
外部资源事件；成熟或被释放的等待通过 durable outbox 创建同一 `ChatSpec.id`、沿用
原 `correlation_id` 的新 Submission / Invocation。它不进入普通用户 Queue，不创建
Ask User，也不保存 Prompt、异常正文或隐藏 reasoning。enqueue 后、outbox 标记前
崩溃由稳定 idempotency key 收敛到同一 Submission。当前仍缺 Provider health 自动
释放、真实 retry-after hint、部分流 continuation boundary 和副作用自动对账，因此
不能宣称任意网络中断已经可以无损续传。

模型流随后增加内容安全的 `ModelOutputBoundary`：明确区分请求尚未输出、非流式完整
响应、部分流、看到 Provider 终态 chunk，以及没有终态 chunk 的 incomplete EOF。该记录只
是枚举，不保存输出、摘要、哈希或隐藏 reasoning；Observation 可以据此避免把部分流
误判成“从未输出后可直接重试”。部分流已能装配为 bounded Model Step
continuation；受控 Harness 也会在四方 admission 后通过 durable outbox 创建 fenced
continuation。两条路径都沿用 correlation、创建新 Invocation，并受 Stop / Interrupt、
新输入、幂等与恢复预算约束。

源码核验进一步确认，当前 AgentScope 在收到终态 chunk 后才把 Assistant / ToolCall
写入 Context，Acting 也发生在完整 Reasoning 之后；它尚不具备 Codex 的“边收工具、
边持久化、断流后排空”前提。因此 QwenPaw 不从残缺 tool delta 猜测并执行 Action。
此前 provider generator 正常 EOF 但没有终态 chunk 时，Token wrapper 会先错误留下一
条成功结果，随后才由 AgentScope 抛出“empty streaming response”；现已在 Provider
边界直接改为 `stream_interrupted`。无内容的 incomplete EOF 可由现有 transport retry
安全重试；已有内容则禁止重放，并保留 `continue_model_step` 供后续 durable
continuation 使用。

### A10. 通信能力契约，而不是统一成一种传输

优先级：P0，直接约束当前 Chat Queue、Continuation 与后续 Hub。

Handbook 第 12 章最值得吸收的不是某个消息中间件，而是把四种语义分开：

```text
S1 Request / Response  短调用，一次请求得到结果或回执
S2 Request Stream      当前请求内增量可见，断流不等于可续传
S3 Durable Handle      执行独立于连接，可按稳定标识查询结果
S4 Durable Channel     有保留、确认、重投和续传位置的持久通道
```

QwenPaw 3.0 不为四档分别建四套业务模型，而是在 Port 与 Adapter 契约中显式声明
能力：`delivery_mode`、`ordering_scope`、`idempotency_key`、`resume_cursor`、
`retention`、`backpressure` 和 `disconnect_policy`。领域对象继续保持清晰：

- `ChatSpec.id` 是 Conversation 身份，不是网络连接或 Provider session；
- `Submission` 是一次已接受输入的持久句柄，不等同于 Invocation；
- `Invocation` 是一次执行尝试，失败或等待后可以由新 Invocation 续行；
- SSE cursor 只承诺恢复当前投影，不冒充完整 Event Log 消费位置；
- `InteractionRequest` 和 `WaitCondition` 描述等待事实，Continuation outbox 负责
  在决定提交后可靠地产生后续 Submission；
- 只有确实需要离线消费、重投与背压的 Hub 链路才升级为 S4。Lite 不因为“未来
  可能分布式”就强制引入消息队列。

Handbook 第 1 章已把“请求—回答”归为上一阶段应用形态，第 4 章则采用持续
Agent Loop、显式等待和异步续行；不能因为其中部分 HTTP/SDK 示例采用请求/响应接口，
就把传输边界误读为长程 Agent 的运行边界。QwenPaw 仅把“一问一答”保留为短 Chat
的快速路径和 UI 投影：一个用户意图可以沿同一 `correlation_id` 跨越多个
Submission、Invocation、Model Step、Action、Interaction 和恢复周期。Assistant
message 只负责对人表达，不负责划定 Runtime 生命周期；等待和恢复也不要求用户再发
一句话来“推动下一轮”。

因此本项裁决不是继续优化问答轮次，而是用 **intent-driven continuous execution**
替换 Runtime 的 turn-driven 假设：一次用户意图在同一 correlation 下自主经过模型、
Action、验证、等待和恢复；只有 Outcome、显式 Stop / Interrupt、不可自动化的 typed
WaitCondition，或预算/恢复终态才结束执行链。Assistant Message 是可多次产生的用户
投影，HTTP response 与 SSE 断开只是传输事件。该模型仍兼容短问答，因为最简单的
执行链自然只有一个 Submission、一个 Invocation 和一个最终 Message。

该裁决现已进入 Chat Runtime 公开契约：`ConversationExecutionChain` 按 correlation
聚合同一意图的 Submission、Invocation 与阻塞 Interaction。技术执行成功后状态为
`inactive`，而不是 `completed`；只有独立 `ConversationOutcome` 事实才能声明业务
达成。Lite Outcome Store 要求具名 producer 和显式 supersession，并允许引用 Artifact、
Evidence 与 Verification；Host Outcome Broker 已成为唯一写入准入边界。system 与
plugin producer 走相同声明入口并支持热注册/注销，普通 Chat 引用必须属于当前
ChatSpec，Task achieved 则复用已有 Execution Contract、Verification Policy 和 Result
Package 完成门禁。模型文本仍不能自动生成 Outcome。这使 Console、插件和未来 Task
Workbench 无需从最后一条 assistant message 猜测任务状态。

该 Broker 已进入真实 Workspace/Invocation 装配，而非停留在独立 Store：Outcome-aware
Tool/Driver Host 通过可选 `OutcomeHostAccess` 暴露服务，旧 Provider Host 合同不变；
Host 从 pinned `InvocationScope` 写入 ChatSpec、correlation、Invocation 和 generation，
producer 不能自行填写身份字段。插件只有经 Host 显式注册后，新 Invocation 才能取得
该服务，注册本身不要求 Runtime 重启。

模型资源等待进一步验证了这一点：一次调用因限流或额度耗尽停止时，恢复由 Runtime
自身的等待事实和 continuation 驱动，不制造“是否继续？”对话。只有资源恢复需要
用户授权或高影响选择时，才正交地创建 Interaction；资源等待本身不是人机问答。

后台 Action 也已采用同一原则：工具从前台 offload 后，Action Recorder 先准备未发布
私有 context snapshot，再提交 ActionResult；只有精确匹配的已提交结果才能发布
durable continuation。来源 Invocation 结束后由 Runtime 自主创建后续 Invocation，
不要求用户追问结果或发送“继续”。并行结果通过
`CommittedActionItem` 分别留证，并在恢复时合并最新 Session，而不是用一份旧对话
快照覆盖另一份。

这一边界已落实为可执行 `UserInputReason` 契约：新 Ask User 只能声明缺失必要事实、
关键偏好、范围授权或高影响裁决，未分类请求在写入统一 Interaction Store 前被拒绝；
内置 Tool、插件 SDK 和兼容 Adapter 共用同一 admission gate。历史未分类记录继续
可读，结构化原因进入内容安全 Observation，供后续评估是否存在“逐步追问代替自动
推进”的策略滥用。

这项设计也决定普通 Chat Ask User 的正确恢复方式：先原子提交回答和内容最小化
continuation outbox，再创建同一 `ChatSpec.id` 下的新 Submission / Invocation；不
序列化 Python 调用栈，也不依赖旧 SSE 连接仍然存在。问题与回答只从权威
Interaction Store 在执行时装配，Queue Store 仅保存 `interaction_id` 等引用。

验收：SSE 断线不改变已接受 Submission；重连可以取得当前投影；进程在回答提交后、
Continuation 入队前退出时，重启能够从 outbox 补发且不重复；短 Tool 调用仍保持
S1/S2，不被迫走持久队列。

### A11. Scenario / Evidence / Evaluation / Promotion 插件晋升门禁

优先级：P1；先增强现有 shadow generation，不建设独立仿真平台。

Handbook 第 16 章明确区分“运行完成、任务成功、允许发布”。这一点应进入 3.0
插件生命周期，否则“安装即生效”容易被误解为“代码能导入就立刻获得执行权”。
建议把现有激活流程升级为：

```text
discover -> validate -> stage -> shadow generation
         -> contract checks -> risk-based scenarios
         -> evidence bundle -> evaluation decision
         -> authorized promotion -> atomic publish
```

其中：

- **Contract checks** 验证 Port、identity、schema、权限声明和兼容版本；
- **Scenario** 只替换本次实验的环境条件，不替换被测插件行为；
- **Evidence bundle** 保存结构化结果、Action / Interaction / Artifact 引用和环境
  Manifest，不把日志文本当作发布证据；
- **Evaluation** 按插件声明的成功标准判定，证据不足返回 `indeterminate`；
- **Promotion** 是 Host 的授权动作，只有它能发布新 generation；失败继续使用上一
  个健康 generation，不污染正在运行的 lease。

Lite 按风险分级保持轻量：纯 Renderer / Prompt 贡献只跑确定性契约样例；只读 Tool
增加隔离调用；外部写入、Shell、Browser、Driver 和 Harness 贡献必须验证 Policy、
Approval、幂等和副作用证据。验证在安装事务内自动完成，因此普通插件仍然“安装后
无需重启即生效”，只是生效点从 import 成功收紧为 promotion 成功。

这一门禁还为后续受控自进化预留边界：生产 Badcase 可以生成候选 Scenario、Skill
或 Policy，但只能进入 shadow generation，不能由运行中的 Agent 自行修改当前
generation。

## 5. 不建议直接吸收的内容

| 内容 | 决策 | 原因 |
|---|---|---|
| Nacos 作为 QwenPaw 必选 Registry | 不吸收 | Handbook 的产品实现示例，不是领域契约；Lite 应保持轻量 |
| 现在就建设 ARD / RAD 全套语义检索 | 延后 | 先解决版本、治理资格和 Context Manifest；关键词发现足够验证契约 |
| 为了“OS”重写 Linux 内核或自建容器平台 | 不吸收 | 当前价值在系统职责和强制点，不在实现层次 |
| 立刻引入完整 Multi-Agent 编排产品 | 延后 | 当前优先 Chat 单 Agent 基建；委派契约可预留，不先做团队拓扑 UI |
| 用 Task 替代 Conversation | 不吸收 | Task 与交互线程生命周期不同；继续使用 `ChatSpec.id` |
| 把所有 Memory、RAG、Ontology 统一成一个数据库 | 不吸收 | 来源、生命周期、权限和责任不同，统一 Port 不等于统一存储 |
| 先开发 Task Workbench 承载新概念 | 不吸收 | 用户已明确 Task 页面后置；先让 Chat 使用真实新基建 |

## 6. 对当前实施顺序的影响

不改变“Chat-first、Task 页面后置”的原则，建议顺序调整为：

1. 保持已完成的 Chat 工具产物 Artifact / Evidence 宿主捕获；
2. 保持已完成的 Context Manifest 调用前留证，覆盖每次真实模型调用的消息、
   Tool Schema、来源、信任和 generation；
3. 冻结并实现统一 Action Plane，使内置和插件不再只在 Tool Adapter 层对齐；
4. 冻结 Environment Contract，并让 Lite 本地 Runtime 兑现；
5. 冻结 Communication Contract，先让 Chat Submission、SSE、Interaction
   Continuation 和 Event Log 的承诺互不混淆；
6. 统一语义观测，先从现有 Event 派生，不另建第二套运行状态机；
7. 冻结 Model Call Attempt / Route Decision；Lite 记录直连与重试，后续 Edition
   再实现自动路由；
8. 把 Checkpoint 与 Workspace snapshot / Action uncertainty 对齐，完成失败恢复；
9. 在 shadow generation 增加风险分级 Scenario / Evidence / Promotion 门禁；
10. 预留 Outcome / Trajectory 事件，不先建设评估平台；
11. 基础模块全部通过真实 Chat 验收后，再恢复 Task Workbench。

Workstation / Hub 后续增加 Budget Lease、外置 Registry、分布式状态和隔离 Runtime；
Lite 不为未来形态提前承担其部署复杂度。

## 7. 可验证的验收补充

- 每次真实模型调用都有可查询的 Context Manifest，且不保存隐藏推理或 Secret。
- 同一内置 Tool 与插件 Tool 生成相同 Action 生命周期、审批、Artifact / Evidence
  和错误分类。
- Shell / Driver / MCP 不再绕过 Action Request 的身份、风险、幂等和审计字段；
  Driver 运行中产生的审批以不可变 Link 关联 Action，策略拒绝记为 `denied` 而非
  伪装成执行错误。
- Environment 不满足声明时在执行前失败，不让模型进入模糊降级。
- `/clear` 清理 Conversation Context，但不删除 Artifact、Evidence、Task State 或
  已提交 Outcome。
- generation 热替换不会改变运行中 Context / Capability / Action 版本。
- Run 成功但业务 Outcome 未确认时，界面和 API 不得显示为“业务已完成”。
- 轨迹可从因果事件重建，并能定位某次 Action 使用的 Context、能力版本和审批。
- Tool Call 意图、策略判定和真实副作用分别留证；Tool 自报成功不能代替 Runtime
  Evidence。
- HITL 与 Interrupt 的等待、恢复、拒绝、中断点和有效结果可跨请求还原。
- WaitCondition 不包含用户问题、回答或审批参数；`live_invocation` 与
  `checkpoint` continuation 必须可区分，禁止把可查询等待伪称为跨进程续跑。
- Harness 审批型 Action 在 Provider 恢复前已有 Request 与 Approval Link；只提供
  start/completed 事件的 Provider 明确标注为 observed boundary，缺失完成事件时结果
  为 `unknown` 且副作用状态为 `uncertain`。
- 每次模型重试或降级都有独立 Attempt 与 Route Decision，不在 Provider Adapter
  内静默切换模型。
- Model Call 记录只关联 ContextManifest 和实际 usage，不保存消息、Prompt、隐藏
  推理或 Secret；流看到终态 chunk 后被消费者关闭仍记为成功，未见终态的提前关闭
  才记为取消。
- HTTP/SSE 断连不改变已接受 Submission 的权威状态；Projection cursor、Event Log
  cursor 和任务句柄分别声明，禁止互相冒充。
- Interaction 回答与 Continuation outbox 原子提交；恢复产生的新 Invocation 使用
  同一 `ChatSpec.id`，稳定幂等键确保最多形成一个后续 Submission。
- 插件只有在 shadow generation 的契约、风险场景和证据门禁通过后才发布；失败或
  证据不足保留上一健康 generation，且普通热插件无需重启服务。

## 8. 参考章节

- [2026 Agent 开发者调研报告](https://github.com/aliyun/ai-agent-handbook/blob/main/2026-agent-survey-report.md)
- [第 3 章：Harness 的责任边界](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%203%20%E7%AB%A0%20%E8%8C%83%E5%BC%8F%EF%BC%9AHarness%20%E7%9A%84%E4%B8%BB%E6%B5%81%E6%9E%84%E5%BB%BA%E6%96%B9%E5%BC%8F%E5%92%8C%E8%B4%A3%E4%BB%BB%E8%BE%B9%E7%95%8C.md)
- [第 4 章：任务、长程推进与完成证据](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%204%20%E7%AB%A0%20%E4%BB%BB%E5%8A%A1%EF%BC%9A%E7%BC%96%E6%8E%92%E3%80%81%E9%95%BF%E7%A8%8B%E6%8E%A8%E8%BF%9B%E4%B8%8E%E5%8D%8F%E4%BD%9C%E6%B5%81%E8%BD%AC.md)
- [第 5 章：Context、State 与 Workspace](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%205%20%E7%AB%A0%20%E4%BF%A1%E6%81%AF%EF%BC%9A%E4%B8%8A%E4%B8%8B%E6%96%87%E3%80%81%E7%8A%B6%E6%80%81%E4%B8%8E%E5%8F%AF%E5%A4%8D%E7%94%A8%E8%83%BD%E5%8A%9B%E8%B5%84%E4%BA%A7.md)
- [第 6 章：Action Plane 与 HITL](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%206%20%E7%AB%A0%20%E8%A1%8C%E5%8A%A8%EF%BC%9A%E5%8F%97%E6%8E%A7%E6%89%A7%E8%A1%8C%E3%80%81%E9%AA%8C%E8%AF%81%E5%8F%8D%E9%A6%88%E4%B8%8E%E4%BA%A4%E4%BB%98%E5%87%86%E5%A4%87.md)
- [第 7 章：Agent Runtime 与 Sandbox](https://github.com/aliyun/ai-agent-handbook/blob/main/03-run/%E7%AC%AC%207%20%E7%AB%A0%20%20Agent%20%E8%BF%90%E8%A1%8C%E6%97%B6%E4%B8%8E%E6%B2%99%E7%AE%B1.md)
- [第 8 章：状态、Checkpoint 与 Artifact](https://github.com/aliyun/ai-agent-handbook/blob/main/03-run/%E7%AC%AC%208%20%E7%AB%A0%20Agent%20%E7%8A%B6%E6%80%81%E5%AD%98%E5%82%A8%E4%B8%8E%E8%AF%AD%E4%B9%89%E8%B5%84%E4%BA%A7.md)
- [第 9 章：AI 网关与统一流量治理](https://github.com/aliyun/ai-agent-handbook/blob/main/03-run/%E7%AC%AC%209%20%E7%AB%A0%20%20AI%20%E7%BD%91%E5%85%B3%E4%B8%8E%E7%BB%9F%E4%B8%80%E6%B5%81%E9%87%8F%E6%B2%BB%E7%90%86.md)
- [第 10 章：异步任务与自动化流程](https://github.com/aliyun/ai-agent-handbook/blob/main/03-run/%E7%AC%AC%2010%20%E7%AB%A0%20%20Agent%20%E5%BC%82%E6%AD%A5%E4%BB%BB%E5%8A%A1%E4%B8%8E%E8%87%AA%E5%8A%A8%E5%8C%96%E6%B5%81%E7%A8%8B.md)
- [第 12 章：Agent 分布式通信](https://github.com/aliyun/ai-agent-handbook/blob/main/03-run/%E7%AC%AC%2012%20%E7%AB%A0%20Agent%20%E5%88%86%E5%B8%83%E5%BC%8F%E9%80%9A%E4%BF%A1.md)
- [第 13 章：Agent 的可观测性](https://github.com/aliyun/ai-agent-handbook/blob/main/04-governance/%E7%AC%AC%2013%20%E7%AB%A0%E3%80%80Agent%20%E7%9A%84%E5%8F%AF%E8%A7%82%E6%B5%8B%E6%80%A7.md)
- [第 14 章：Agent 安全](https://github.com/aliyun/ai-agent-handbook/blob/main/04-governance/%E7%AC%AC%2014%20%E7%AB%A0%E3%80%80Agent%20%E5%AE%89%E5%85%A8.md)
- [第 15 章：AI 资产注册与发现](https://github.com/aliyun/ai-agent-handbook/blob/main/04-governance/%E7%AC%AC%2015%20%E7%AB%A0%E3%80%80AI%20%E8%B5%84%E4%BA%A7%E7%9A%84%E5%8F%91%E7%8E%B0%E4%B8%8E%E7%AE%A1%E7%90%86.md)
- [第 16 章：Agent Simulation 与质量验证](https://github.com/aliyun/ai-agent-handbook/blob/main/04-governance/%E7%AC%AC%2016%20%E7%AB%A0%E3%80%80Agent%20%E8%A1%8C%E4%B8%BA%E7%94%9F%E6%88%90%E4%B8%8E%E8%B4%A8%E9%87%8F%E9%AA%8C%E8%AF%81.md)
- [第 19 章：Agent 轨迹数据](https://github.com/aliyun/ai-agent-handbook/blob/main/05-optimization/%E7%AC%AC%2019%20%E7%AB%A0%E3%80%80Agent%20%E8%BD%A8%E8%BF%B9%E6%95%B0%E6%8D%AE.md)
- [第 23 章：受控自进化](https://github.com/aliyun/ai-agent-handbook/blob/main/05-optimization/%E7%AC%AC%2023%20%E7%AB%A0%E3%80%80%E5%8F%97%E6%8E%A7%E8%87%AA%E8%BF%9B%E5%8C%96.md)
- [第 30 章：Agentic OS](https://github.com/aliyun/ai-agent-handbook/blob/main/07-conclusion/%E7%AC%AC%2030%20%E7%AB%A0%20%E4%BB%8E%20Agentic%20Application%20%E5%88%B0%20Agentic%20OS.md)
