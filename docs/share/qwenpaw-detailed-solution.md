# QwenPaw 完整解决方案与能力建设路线

## 一、目标与范围

本方案基于精简版战略判断，重点回答：

- QwenPaw 当前问题的根因是什么；
- 应建立哪些核心领域对象和模块边界；
- Chat、Task、Inbox、Artifact、Automation 如何协同；
- 如何实现可靠后台运行、无人值守、因果可观测和结果验证；
- 如何把任务经验沉淀成可复用能力；
- Lite、Workstation、Hub 如何共享核心并差异化装配；
- 每个阶段如何验收，如何避免再次形成平行系统。

本方案不要求一次重写现有 Runtime，也不新建平行任务系统。核心策略是：

> 在现有 AgentScope Runtime 之上建立统一 Work 领域层，将已有 Chat、Goal、
> Cron、Harness、Approval、Audit、Checkpoint、Plugin 和 Artifact 能力逐步
> 接入同一事实源。

## 二、总体解决思路

QwenPaw 的目标产品闭环是：

    Work Sources
    ├── Chat
    ├── Task
    ├── Time Trigger
    ├── Event / Webhook
    └── Plugin Trigger
              ↓
       Unified Work Service
              ↓
       Reliable Work Runtime
    ├── Plan / Step
    ├── Executor / Harness
    ├── Policy / Approval
    ├── Retry / Recovery
    └── Budget / Timeout
              ↓
       Causal Event Graph
    ├── Timeline Projection
    ├── Audit Projection
    ├── Checkpoint Relation
    └── Root Cause Analysis
              ↓
       Delivery & Outcome
    ├── Inbox
    ├── Artifact
    ├── Evidence
    └── Verification
              ↓
       Capability Flywheel
    ├── Retrospective
    ├── Candidate Scoring
    ├── Capability Registry
    └── Plugin Distribution

所有入口、执行器和插件最终都创建或操作统一 Work，不允许维护第二套事实源。

## 三、解决问题一：能力分散，工作闭环不完整

### 1. 根因

当前 Chat、Goal、Mission、Coding、Cron、Harness、Approval 和 Checkpoint
分别从不同角度描述工作，但缺少统一身份和生命周期。

结果是：

- 同一项工作在多个模块拥有不同 ID；
- 状态只能局部解释；
- Approval、Artifact 和 Checkpoint 无法稳定追溯到同一 Work；
- 新入口或插件容易再次创建一套状态模型；
- Console 很难展示一致的任务状态。

### 2. 解决方案：统一 Work 领域模型

建议建立以下核心对象：

#### Work

表示用户希望 QwenPaw 完成的一项真实工作。

关键字段：

- work_id；
- title；
- goal；
- acceptance；
- source_type 与 source_ref；
- workspace_id；
- autonomy_level；
- status；
- current_run_id；
- created_at、updated_at；
- policy_scope；
- budget；
- artifact_refs。

#### Run

表示 Work 的一次执行。重新运行、恢复或切换 Executor 时可以创建新 Run，
但仍属于同一 Work。

关键字段：

- run_id；
- work_id；
- executor_ref；
- status；
- started_at、finished_at；
- resume_from；
- budget_usage；
- failure_reason。

#### Step

表示 Plan 中可以观察和恢复的执行步骤。

关键字段：

- step_id；
- run_id；
- title；
- order；
- status；
- depends_on；
- retry_policy；
- checkpoint_ref；
- artifact_refs。

#### Artifact、Evidence 与 Verification

- Artifact：最终或阶段性产出；
- Evidence：支撑结果的来源、测试和事实；
- Verification：根据 Acceptance 对结果作出的判定。

三者必须与 Work、Run、Step 建立明确关联。

### 3. 状态模型

Work 状态建议收敛为：

    draft
      ↓
    ready
      ↓
    queued
      ↓
    running
      ├── waiting_approval
      ├── waiting_input
      ├── paused
      ├── recovering
      └── verifying
      ↓
    completed / failed / cancelled

状态转换必须由领域服务统一执行，并记录 Event。Chat、Cron、Harness 与 Plugin
不得直接写入互相矛盾的状态。

### 4. Chat 与 Task 双入口

#### Chat

Chat 保持轻量：

- 可以快速问答；
- 可以调用低风险工具；
- 可以逐步形成需求；
- 不强制生成完整 Plan。

当出现明确 Artifact、多步骤、后台运行、外部副作用或正式验收时，系统建议
升级为 Task。

#### Task

Task 是 Work 的目标驱动产品视图：

- Overview：Goal、Acceptance、状态与结果；
- Plan：步骤与当前执行点；
- Timeline：发生了什么；
- Conversation：与 Agent 的交互；
- Approvals：待处理和历史审批；
- Artifacts：交付物；
- Evidence：验证证据。

#### Chat 升级 Task

升级不是复制 Chat，而是：

1. 创建 Work；
2. 将 Chat 关联为 source_ref；
3. 从对话提取 Goal 和 Acceptance 草稿；
4. 生成可修改 Plan；
5. 后续执行进入 Work Runtime；
6. Chat 中保留 Task 引用；
7. Task Conversation 继续使用原上下文。

### 5. API 边界

建议对外稳定的 Work API 至少包括：

- create_work；
- get_work；
- list_works；
- start_work；
- pause_work；
- resume_work；
- cancel_work；
- provide_input；
- list_events；
- list_artifacts；
- verify_work。

内部 Runtime、Console、CLI 和 Plugin 都通过同一应用服务访问 Work，不直接
操作持久层。

### 6. 验收标准

- Chat 可以无损升级为 Task；
- 手动、Cron 和 Webhook 创建的 Work 使用同一 Schema；
- Approval、Checkpoint、Artifact 和 Verification 都能追溯到 work_id；
- Console 刷新后状态一致；
- Runtime 重启后 Work 能从持久状态恢复；
- 不存在第二套公开 Task 状态枚举。

## 四、解决问题二：长任务和无人值守不够可靠

### 1. 根因

Agent Run 通常假设一次进程内连续执行，而无人值守工作需要面对：

- 进程退出；
- 网络和外部 API 失败；
- 工具执行超时；
- 用户长时间不在线；
- Approval 延迟；
- 重试导致重复副作用；
- Token、费用和时间超限；
- Executor 或插件失效。

### 2. 解决方案：可靠运行契约

每个 Work 在进入后台或无人值守前，必须具备 Execution Contract：

- Goal；
- Acceptance；
- autonomy_level；
- permission_scope；
- side_effect_policy；
- time、token、cost、concurrency budget；
- retry_policy；
- timeout_policy；
- escalation_policy；
- exit_conditions；
- required_artifacts；
- verification_policy。

缺少 Acceptance、权限或退出条件的 Work 不允许直接进入 L3。

### 3. 三个自治等级

#### L1：交互执行

- 适合首次运行和高风险任务；
- 关键步骤向用户同步；
- 高影响动作需要明确批准；
- 失败时优先请求用户决策。

#### L2：受控后台

- 页面关闭后继续运行；
- Policy 允许范围内自动执行；
- 自动重试和恢复；
- 越权、超预算或无法恢复时进入 Inbox；
- Workstation 的默认复杂任务模式。

#### L3：无人值守

- 适合成熟 Workflow 和 Hub Automation；
- 在预授权范围内自动执行、恢复和验证；
- 正常过程不打扰用户；
- 完成后交付 Result Package；
- 只有异常、风险和策略越界才升级人工。

### 4. 运行可靠性机制

#### 持久队列和租约

- queued Work 进入持久队列；
- Worker 获取带期限的 lease；
- Worker 周期性 heartbeat；
- lease 过期后允许安全重新调度；
- 同一 run_id 同时只能有一个有效 lease。

#### 幂等副作用

所有可能产生外部写入的动作必须使用 idempotency_key：

- GitHub 评论；
- 创建 PR；
- Push；
- 发送消息；
- 写入外部文档；
- 发布或部署。

重试前检查 Side Effect Record，已成功动作不重复执行。

#### Checkpoint 与恢复

Checkpoint 不只保存文件状态，还需要记录：

- work_id、run_id、step_id；
- 当前 Plan 版本；
- Runtime 上下文摘要；
- 已完成副作用；
- Artifact 与 Evidence 引用；
- 恢复前置条件。

恢复时创建新 Run 或新的 attempt，并保留与原失败 Run 的因果关系。

#### 预算与 Kill Switch

Work 应支持：

- 最大执行时间；
- 最大 Token；
- 最大费用；
- 最大工具调用数；
- 最大重试次数；
- 最大并行度；
- 用户或管理员立即停止。

### 5. Result Package

无人值守完成后不能只写 completed，应交付：

- 执行摘要；
- 最终 Artifact；
- Acceptance 结果；
- Evidence；
- Verification；
- 发生过的恢复和重试；
- 剩余风险；
- 建议的下一步；
- 复用候选。

### 6. 验收标准

- 关闭页面后 Work 继续运行；
- Runtime 重启后恢复；
- Worker 异常退出后 lease 可重新调度；
- 自动重试不产生重复外部副作用；
- 超预算时 Work 进入可解释状态；
- L3 完成前必须运行 Verification；
- 无法继续时 Inbox 收到可操作的 Exception。

## 五、解决问题三：轨迹可见，但因果不完整

### 1. 根因

单纯 Timeline 只能显示事件顺序，不能说明：

- 当前动作服务于哪个目标；
- 为什么选择该工具；
- 哪条 Policy 要求审批；
- 哪个副作用产生了 Artifact；
- 哪些 Evidence 支撑 Verification；
- 哪个失败触发恢复。

### 2. 解决方案：Causal Event Graph

所有领域事件采用统一 Event Envelope：

- event_id；
- event_type；
- occurred_at；
- work_id；
- run_id；
- step_id；
- actor；
- source；
- cause_event_id；
- correlation_id；
- payload_ref；
- schema_version。

cause_event_id 表示直接原因，correlation_id 将同一业务链路关联起来。

### 3. 关键事件类型

- work.created；
- work.started；
- plan.generated；
- step.started；
- tool.requested；
- policy.evaluated；
- approval.requested；
- approval.resolved；
- side_effect.recorded；
- checkpoint.created；
- artifact.created；
- evidence.attached；
- verification.completed；
- run.failed；
- run.recovery_started；
- work.completed。

### 4. 多种投影视图

同一 Event Store 可以生成不同视图：

- Timeline：按时间查看过程；
- Task Status：聚合当前状态；
- Approval Queue：查看需要处理的事项；
- Audit View：查看权限和副作用；
- Causal Graph：查看事件因果；
- Cost View：查看资源消耗；
- Recovery View：查看失败点和可恢复 Checkpoint。

这些是同一事实源的投影，不是多个独立状态系统。

### 5. 根因分析

失败后自动生成 Failure Summary：

- failure_event；
- 直接原因；
- 上游依赖；
- 最近成功 Checkpoint；
- 已发生副作用；
- 可安全重试范围；
- 推荐恢复策略；
- 是否需要人工接管。

### 6. 验收标准

- 每个 Tool Call 可以追溯到 Step 与 Goal；
- 每个 Approval 可以追溯到 Policy Decision；
- 每个 Artifact 可以追溯到产生它的事件；
- Verification 可以列出使用的 Evidence；
- 失败页面能显示直接原因和推荐恢复点；
- Timeline、Audit 和 Status 不产生互相矛盾的事实。

## 六、解决问题四：回复和结果缺少稳定交付位置

### 1. 根因

Chat、Task 和 Automation 都可能异步产生结果，但如果直接把所有内容放回
原页面：

- 用户离开后难以发现；
- Approval 和 Exception 分散；
- Timeline 噪声过多；
- Artifact 退化为附件；
- 用户需要重新阅读过程才能理解结果。

### 2. 解决方案：Inbox Delivery

Inbox 是 Delivery Projection，不是新的内容事实源。

Inbox Item 包含：

- inbox_item_id；
- item_type；
- source_type 与 source_ref；
- work_id；
- title；
- summary；
- action_required；
- action_refs；
- delivery_status；
- read_at；
- resolved_at。

Inbox 接收：

- Chat Reply；
- Task Result；
- Approval Request；
- Exception；
- Artifact Ready；
- Verification Result；
- Automation Result。

Inbox 不接收：

- 每次 Tool Call；
- 普通 Step 更新；
- 自动重试的每个过程；
- 全量 Timeline；
- 无需用户关注的成功事件。

原则：

> Timeline 记录所有事实；Inbox 只投递回复、结果和需要行动的事项。

### 3. Chat 与 Inbox 去重

- 用户正在 Chat 页面时，Reply 直接显示并将投递标记为已读；
- 用户离开时，Reply 完成后进入 Inbox；
- 点击 Inbox Item 返回原 Chat 或 Work；
- Inbox 只保存摘要和引用，不复制完整回答；
- Chat、Task 和 Inbox 共享 read/resolved 状态。

### 4. 解决方案：Artifact Registry

Artifact 必须是一等领域对象：

- artifact_id；
- artifact_type；
- work_id、run_id、step_id；
- version；
- status；
- storage_ref；
- checksum；
- evidence_refs；
- verification_ref；
- created_by；
- created_at；
- supersedes。

生命周期：

    draft
      ↓
    ready_for_review
      ↓
    verified
      ↓
    delivered
      ↓
    superseded / archived

异常状态：

- incomplete；
- verification_failed；
- blocked；
- invalidated。

### 5. Artifact 操作

- 预览和下载；
- 查看来源 Work 和 Timeline；
- 查看 Evidence 与 Verification；
- 比较版本；
- 重新验证；
- 接受、拒绝或要求修改；
- 导出和分享；
- 提升为复用候选；
- 标记替代和归档。

### 6. 验收标准

- 离开 Chat 后仍能收到异步 Reply；
- Task 和 Automation 完成后生成 Result Inbox Item；
- Approval 和 Exception 可以从 Inbox 直接处理；
- 普通 Timeline 不进入 Inbox；
- Artifact 具备来源、版本和验证状态；
- Inbox 删除或已读不会删除事实源内容。

## 七、解决问题五：一次任务无法形成能力复利

### 1. 根因

任务完成后缺少结构化复盘、候选分类、发布门槛和使用反馈，导致：

- 同类任务反复从零开始；
- Prompt、命令和经验散落；
- Workflow 与 Automation 混淆；
- Tool 与 Skill 边界不清晰；
- Plugin API 很广但治理不足。

### 2. 解决方案：Retrospective

Work 完成后生成结构化复盘：

- Goal、Acceptance 与结果；
- 实际执行路径；
- 成功步骤；
- 失败尝试和恢复；
- Artifact、Evidence 与 Verification；
- 环境、权限和依赖；
- 适用与不适用条件；
- 可复用候选。

### 3. 复用资产分类

| 类型 | 适合内容 |
|---|---|
| Knowledge | 稳定事实、规范和决策 |
| Template | 稳定输入输出结构 |
| Skill | 需要上下文和语义判断的经验 |
| Workflow | 可重复的多步骤过程 |
| Tool | 输入输出明确的确定性动作 |
| Automation | Trigger 与 Workflow 的绑定 |
| Plugin | 能力的安装、分发和治理载体 |

评分决定是否值得沉淀，类型规则决定形式，验证门槛决定能否发布。

### 4. Capability Registry

每项能力至少记录：

- capability_id；
- type；
- version；
- scope；
- source_work_ids；
- permissions；
- inputs 与 outputs；
- compatibility；
- verification；
- reuse_count；
- success_rate；
- last_verified_at；
- lifecycle_status。

### 5. 发布门槛

#### Knowledge、Template

- 标注来源和适用范围；
- 不包含敏感数据；
- 至少一次后续复用验证。

#### Skill、Workflow、Automation

- 输入和输出明确；
- 包含失败和退出条件；
- 有成功与失败样例；
- 副作用声明权限和审批；
- 产生 Artifact 和 Verification。

#### Tool、Plugin

- 稳定 API Schema；
- 单元测试和契约测试；
- 最小权限；
- 可禁用、回滚和查看健康状态；
- 不依赖来源 Task 的隐式上下文；
- 经多次真实任务验证。

### 6. 稳定 Plugin Contract

插件 Manifest 应声明：

- plugin_id 和 version；
- api_version 与 manifest_version；
- contributions；
- permissions；
- dependencies；
- activation_events；
- hot_reload_policy；
- health_check；
- compatibility；
- migrations。

标准 Contribution：

- Tool；
- Skill；
- Memory；
- Channel；
- Trigger；
- Workflow；
- Executor；
- UI Slot；
- PawApp。

### 7. 安装即生效

安装即生效的前提：

- 插件贡献通过 Manifest 静态声明；
- Loader 在独立 generation 中装配；
- 验证成功后原子切换 Registry；
- 失败时保持旧 generation；
- 长连接和不可热替换资源明确要求重启；
- Console 能查看激活状态、错误与健康信息。

### 8. 验收标准

- Work 完成后生成 Retrospective；
- 复用候选可追溯到来源 Work；
- 至少一个任务完成“执行—沉淀—复用—反馈”；
- 失败或过期能力可以降级和停用；
- 官方 Tool、Memory、Channel 和 PawApp 遵循同一插件契约；
- 支持安装、热生效、禁用和回滚。

## 八、工作台自动化

Automation 是 Work Source，不是平行 Runtime。

### 1. 定时与事件必须区分

- Time Trigger：按时间创建 Work；
- Event Trigger：按 PR、Issue、Push 等事件创建 Work；
- Internal Trigger：按 Work 完成、Verification 失败等内部事件创建 Work；
- Manual Trigger：用户主动运行成熟 Workflow。

### 2. 统一事件入口

外部事件先完成：

- 签名验证；
- 身份与来源解析；
- 标准化 Event；
- 过滤与匹配规则；
- 幂等去重；
- 创建 Work；
- 异步执行。

Webhook 不直接调用 Harness 或 Tool。

### 3. 自动化治理

- 触发原因可见；
- 事件去重；
- 同一资源并发控制；
- Workflow 版本固定；
- 权限和预算明确；
- 外部写入受 Policy 或 Approval 控制；
- 可暂停、恢复、禁用和人工接管；
- 运行结果进入 Inbox 和 Artifact。

## 九、模块组织与边界

建议按领域职责组织，而不是按页面或具体 Provider 组织：

    qwenpaw/
    ├── kernel/
    │   ├── contracts/
    │   ├── events/
    │   ├── lifecycle/
    │   └── errors/
    ├── work/
    │   ├── domain/
    │   ├── application/
    │   ├── runtime/
    │   └── persistence/
    ├── governance/
    │   ├── policy/
    │   ├── approvals/
    │   ├── audit/
    │   └── checkpoints/
    ├── delivery/
    │   ├── inbox/
    │   └── notifications/
    ├── artifacts/
    │   ├── registry/
    │   ├── storage/
    │   └── verification/
    ├── automation/
    │   ├── triggers/
    │   ├── rules/
    │   └── scheduler/
    ├── capabilities/
    │   ├── retrospective/
    │   ├── registry/
    │   └── scoring/
    ├── plugins/
    │   ├── contract/
    │   ├── loader/
    │   ├── generations/
    │   └── sdk/
    └── editions/
        ├── lite/
        ├── workstation/
        └── hub/

边界原则：

- kernel 只放稳定契约和事件，不依赖业务实现；
- work 是唯一任务事实源；
- governance 通过 Work Event 关联，不直接拥有第二套 Task；
- delivery 只做投影和投递；
- artifacts 管理长期结果；
- automation 只创建 Work；
- capabilities 只管理复用资产；
- plugins 贡献能力，不修改核心领域规则；
- editions 只做能力装配，不复制核心实现。

## 十、Lite、Workstation 与 Hub

### Lite

默认包含：

- Chat、Task、Inbox、Artifact；
- L1 和部分 L2；
- 简单定时任务；
- 轻量 Tool、Skill 与 Workflow；
- 本地存储与单用户权限。

默认不包含：

- 公网 Webhook；
- 多用户；
- 远程队列；
- 组织级能力分发。

### Workstation

在 Lite 基础上增加：

- 多仓库和项目工作区；
- Git、IDE、Terminal 与文件事件；
- L2 和成熟 Workflow 的 L3；
- Coding Harness；
- 插件开发、调试和热安装；
- 内网服务和安全 Relay；
- 作为 Hub 执行节点。

### Hub

在共享核心上增加：

- 公网 Webhook；
- 多用户、多仓库和多项目；
- 持久队列、调度和 Worker；
- 团队 Policy、Approval、Audit、配额；
- 组织级 Inbox 与 Artifact；
- Automation、Workflow 和 Plugin 分发；
- 节点健康、故障转移和成本治理。

## 十一、分阶段实施路线

### P0：用户工作闭环

交付范围：

- Work、Run、Step 核心模型；
- Chat 升级 Task；
- Task Overview、Plan、Timeline、Conversation；
- Approval 投影；
- Inbox Reply、Result、Approval、Exception；
- Artifact Registry 与 Verification 引用；
- 重启恢复基础。

退出条件：

- 状态只有一个事实源；
- Chat、手动 Task 和 Cron 均创建或关联 Work；
- Approval、Artifact、Checkpoint 可追溯；
- 用户离开后能收到结果；
- 一个真实任务完成端到端闭环。

### P1-A：可靠无人值守与因果图

交付范围：

- Execution Contract；
- L1–L3；
- 持久队列、lease、heartbeat；
- idempotency_key 与 Side Effect Record；
- 预算、超时与 Kill Switch；
- Causal Event Graph；
- Failure Summary 和 Recovery View。

退出条件：

- 关闭页面和 Runtime 重启均可恢复；
- Worker 异常退出可重新调度；
- 不重复外部副作用；
- 完成前自动 Verification；
- 失败原因和恢复点可解释。

### P1-B：插件契约与自动化

交付范围：

- Manifest 与 SDK；
- 标准 Contribution；
- generation 原子切换；
- Tool、Memory、Channel、PawApp 真实迁移；
- Time、Git、File 与 Webhook Trigger；
- Automation 管理页面。

退出条件：

- 安装按声明生效；
- 失败不破坏旧 Registry；
- 插件可禁用和回滚；
- Trigger 创建标准 Work；
- 外部写入不绕过治理链。

### P2-A：能力复用飞轮

交付范围：

- Retrospective；
- Candidate Scoring；
- Capability Registry；
- 从 Task 创建 Workflow 与 Automation；
- 复用指标和生命周期治理。

退出条件：

- 至少一个真实流程完成执行、沉淀、复用和反馈；
- 来源、版本和权限可追溯；
- 过期能力可以停用和重新验证。

### P2-B：Hub 与团队场景

交付范围：

- 多用户、多仓库；
- 公网 Webhook；
- 集中队列与多节点；
- 团队 Policy、Approval、Audit 与配额；
- 组织能力分发；
- 代码仓库维护场景包。

退出条件：

- Hub 持续接收事件并创建 Work；
- Workstation 可作为受治理节点；
- 团队审批和 Artifact 可追溯；
- 故障转移不产生重复副作用；
- 组织能力可以分发、禁用和回滚。

## 十二、关键指标

### 用户开始

- 首次完成真实工作所需步骤；
- Chat 升级 Task 成功率；
- 创建 Task 所需配置数量；
- 首次成功时间。

### 可靠完成

- Task 验收通过率；
- 无人工介入完成率；
- 平均人工介入次数；
- 失败恢复率；
- 重复副作用发生率；
- 超时和僵尸 Work 比例。

### 可信结果

- Artifact 可追溯率；
- Verification 覆盖率；
- Approval 与 Audit 覆盖率；
- 根因定位时间；
- 用户理解结果所需时间。

### 结果交付

- Inbox 投递成功率；
- Inbox 噪声率；
- Approval 响应时间；
- Artifact 验收率。

### 能力复用

- 候选采纳率；
- 复用成功率；
- Automation 成功率；
- 节省时间和 Token；
- 插件安装、升级和回滚成功率。

## 十三、主要风险与控制

### 风险一：重新造一套 Task Runtime

控制：

- 复用现有 AgentScope Runtime；
- Work 只负责统一身份、状态和治理；
- Harness 通过 Executor Contract 接入。

### 风险二：Inbox 变成通知垃圾场

控制：

- 只投递 Reply、Result、Approval、Exception；
- 普通 Event 留在 Timeline；
- 设立噪声率指标。

### 风险三：因果图过度复杂

控制：

- Event Envelope 保持最小稳定字段；
- 使用投影生成视图；
- 先覆盖 Tool、Approval、Side Effect、Artifact、Verification 主链。

### 风险四：无人值守扩大安全风险

控制：

- L3 必须具备 Acceptance、Policy、Budget 和 Exit Conditions；
- 外部副作用使用幂等键；
- 高风险动作仍需 Approval；
- 全局 Kill Switch。

### 风险五：能力库产生大量低质量资产

控制：

- 候选不自动发布；
- 设置真实复用和验证门槛；
- 记录成功率和最后验证时间；
- 支持降级、归档和停用。

### 风险六：Lite、Workstation、Hub 形成代码分叉

控制：

- 共享 Domain、Runtime 与 Contract；
- editions 只负责配置和装配；
- 用插件和 Provider 提供差异能力。

## 十四、最终判断

QwenPaw 的解决方案不应是再增加一个 Chat 页面、Task 页面或自动化模块，而是
围绕同一 Work 建立完整用户价值链：

> 用户通过 Chat 探索，通过 Task 交付，通过 Automation 持续触发工作；
> Reliable Work Runtime 负责完成，Causal Work Graph 负责解释；
> Inbox 负责投递回复和结果，Artifact 负责承载可验证价值；
> Capability Flywheel 负责让成功经验转化为下一次能力。

最终目标可以概括为：

> 让用户容易开始，让 QwenPaw 可靠完成，让结果清楚到达，让成功经验持续
> 复用。
