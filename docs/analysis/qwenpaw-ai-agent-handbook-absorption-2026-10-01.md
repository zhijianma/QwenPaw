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
| Budget 与授权联动 | `ExecutionBudget`、Usage Scope 和权限是独立对象 | Workstation / Hub 引入可派生、可撤销的 `BudgetLease`；Lite 先冻结接口，不建设分布式服务 |
| 业务 Outcome | `run.completed` 与 Verification 证明执行完成 | 增加 `Outcome` 投影，区分执行成功、业务验收和后续动作 |
| 资产发布 | Capability generation 解决运行版本，Plugin lifecycle 解决安装 | 增加 Release / Lock Manifest，区分逻辑资源、发布版本、运行引用和派生索引 |
| 轨迹评估 | 已有 causal event，但没有稳定 Trajectory 数据产品 | 从 Event / Context Manifest / Action / Evidence 派生 Trajectory，后续用于 Badcase 与回归 |

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

## 4. 建议吸收的九项设计

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

截至 2026-10-01，Tool 与 Driver 两条垂直切片已经完成。内置与插件 Tool 共用
稳定 Action 契约、执行前不可变请求、执行后内容最小化结果以及
Artifact/Evidence 引用；系统与插件 Driver 共用 Driver Action、动态审批关联和
策略拒绝语义。MCP 作为 Driver 协议已经随 Driver 接入，旧 Driver Manager 路径也
通过兼容 Adapter 留证。Browser 与 Harness Remote 仍属于后续迁移范围。
MCP 标准 `readOnlyHint=true` 会被保留并映射为低风险无副作用 Action；服务未声明
注解时保持 `external_write/high` 的保守默认值，禁止根据工具名称猜测权限。

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

优先级：P1，先冻结接口，Lite 使用本地适配器。

契约包含：镜像 / OS / 架构、文件挂载与读写范围、网络出入站、Secret 引用、
依赖和版本、CPU / 内存 / 存储、超时与并发、快照需求、审计与清理策略。

这会把当前分散在 `project_dir`、Sandbox、Harness 与 Hub runtime 的假设统一起来，
也使 Workstation / Hub 的差异落在 Adapter，而不是 Task 领域模型。

### A5. Budget Lease

优先级：P1；Lite 冻结、Workstation / Hub 实现。

当前 `ExecutionBudget` 是额度合同，Usage Meter 是计量器，Capability lease 是版本
租约，三者都不是 Handbook 所说的可派生授权租约。新对象应绑定：

- 调用额度和资源上限；
- 可执行能力和资源范围；
- 有效期、撤销和父子派生关系；
- Task / Run / Agent identity；
- 子任务终止时的级联收回。

不要把它实现为另一个前端 Queue，也不要把 Lite 的进程内 Usage Scope 宣称为
分布式 Budget Lease。

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

### A8. 语义观测与独立 Runtime Evidence

优先级：P1；契约现在冻结，先复用现有 Ledger 和 Middleware。

当前 QwenPaw 已有 AgentScope 流事件、Task 因果事件、Operational Event 和
Langfuse Tool Span，但它们还不是一套跨 Harness 可比较的语义观测契约。应统一
最小语义类别：`MODEL`、`ACTION`、`GUARDRAIL`、`COMPACTION`、`HITL`、
`INTERRUPT`、`VERIFICATION`，并明确：

- 模型生成 Action 意图与系统实际执行是两个事件；
- Approval 的等待时间不计入 Action 执行耗时；
- 跨请求 HITL 等待结束当前 Trace，恢复时以因果关系连接新 Trace；
- Interrupt 记录发起者、原因、安全点和已经产生的有效结果，不伪装成失败或完成；
- Runtime 从进程、文件、网络或远端执行器采集的事实是独立 Evidence，不能只信
  Tool / Agent 自报成功。

Lite 不新建重型 Observability 服务，先从现有 Event / Context Manifest /
Action Result 派生语义投影；Workstation / Hub 再接 OTEL、集中审计和成本归因。

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
5. 统一语义观测，先从现有 Event 派生，不另建第二套运行状态机；
6. 冻结 Model Call Attempt / Route Decision；Lite 记录直连与重试，后续 Edition
   再实现自动路由；
7. 把 Checkpoint 与 Workspace snapshot / Action uncertainty 对齐，完成失败恢复；
8. 预留 Outcome / Trajectory 事件，不先建设评估平台；
9. 基础模块全部通过真实 Chat 验收后，再恢复 Task Workbench。

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
- 每次模型重试或降级都有独立 Attempt 与 Route Decision，不在 Provider Adapter
  内静默切换模型。

## 8. 参考章节

- [2026 Agent 开发者调研报告](https://github.com/aliyun/ai-agent-handbook/blob/main/2026-agent-survey-report.md)
- [第 3 章：Harness 的责任边界](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%203%20%E7%AB%A0%20%E8%8C%83%E5%BC%8F%EF%BC%9AHarness%20%E7%9A%84%E4%B8%BB%E6%B5%81%E6%9E%84%E5%BB%BA%E6%96%B9%E5%BC%8F%E5%92%8C%E8%B4%A3%E4%BB%BB%E8%BE%B9%E7%95%8C.md)
- [第 4 章：任务、长程推进与完成证据](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%204%20%E7%AB%A0%20%E4%BB%BB%E5%8A%A1%EF%BC%9A%E7%BC%96%E6%8E%92%E3%80%81%E9%95%BF%E7%A8%8B%E6%8E%A8%E8%BF%9B%E4%B8%8E%E5%8D%8F%E4%BD%9C%E6%B5%81%E8%BD%AC.md)
- [第 5 章：Context、State 与 Workspace](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%205%20%E7%AB%A0%20%E4%BF%A1%E6%81%AF%EF%BC%9A%E4%B8%8A%E4%B8%8B%E6%96%87%E3%80%81%E7%8A%B6%E6%80%81%E4%B8%8E%E5%8F%AF%E5%A4%8D%E7%94%A8%E8%83%BD%E5%8A%9B%E8%B5%84%E4%BA%A7.md)
- [第 6 章：Action Plane 与 HITL](https://github.com/aliyun/ai-agent-handbook/blob/main/02-build/%E7%AC%AC%206%20%E7%AB%A0%20%E8%A1%8C%E5%8A%A8%EF%BC%9A%E5%8F%97%E6%8E%A7%E6%89%A7%E8%A1%8C%E3%80%81%E9%AA%8C%E8%AF%81%E5%8F%8D%E9%A6%88%E4%B8%8E%E4%BA%A4%E4%BB%98%E5%87%86%E5%A4%87.md)
- [第 8 章：状态、Checkpoint 与 Artifact](https://github.com/aliyun/ai-agent-handbook/blob/main/03-run/%E7%AC%AC%208%20%E7%AB%A0%20Agent%20%E7%8A%B6%E6%80%81%E5%AD%98%E5%82%A8%E4%B8%8E%E8%AF%AD%E4%B9%89%E8%B5%84%E4%BA%A7.md)
- [第 9 章：AI 网关与统一流量治理](https://github.com/aliyun/ai-agent-handbook/blob/main/03-run/%E7%AC%AC%209%20%E7%AB%A0%20%20AI%20%E7%BD%91%E5%85%B3%E4%B8%8E%E7%BB%9F%E4%B8%80%E6%B5%81%E9%87%8F%E6%B2%BB%E7%90%86.md)
- [第 13 章：Agent 的可观测性](https://github.com/aliyun/ai-agent-handbook/blob/main/04-governance/%E7%AC%AC%2013%20%E7%AB%A0%E3%80%80Agent%20%E7%9A%84%E5%8F%AF%E8%A7%82%E6%B5%8B%E6%80%A7.md)
- [第 14 章：Agent 安全](https://github.com/aliyun/ai-agent-handbook/blob/main/04-governance/%E7%AC%AC%2014%20%E7%AB%A0%E3%80%80Agent%20%E5%AE%89%E5%85%A8.md)
- [第 15 章：AI 资产注册与发现](https://github.com/aliyun/ai-agent-handbook/blob/main/04-governance/%E7%AC%AC%2015%20%E7%AB%A0%E3%80%80AI%20%E8%B5%84%E4%BA%A7%E7%9A%84%E5%8F%91%E7%8E%B0%E4%B8%8E%E7%AE%A1%E7%90%86.md)
- [第 19 章：Agent 轨迹数据](https://github.com/aliyun/ai-agent-handbook/blob/main/05-optimization/%E7%AC%AC%2019%20%E7%AB%A0%E3%80%80Agent%20%E8%BD%A8%E8%BF%B9%E6%95%B0%E6%8D%AE.md)
- [第 30 章：Agentic OS](https://github.com/aliyun/ai-agent-handbook/blob/main/07-conclusion/%E7%AC%AC%2030%20%E7%AB%A0%20%E4%BB%8E%20Agentic%20Application%20%E5%88%B0%20Agentic%20OS.md)
