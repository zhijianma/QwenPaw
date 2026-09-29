# QwenPaw Detailed Solution 与当前 Goal 差异审计

- 日期：2026-09-24
- 对比输入：`docs/share/qwenpaw-detailed-solution.md`
- 当前执行基线：`docs/design/qwenpaw-os-infrastructure-migration-plan.md`
- 当前顺序：Chat-first，冻结 Task Workbench 新功能

## 1. 结论

详细方案应被吸收为 QwenPaw OS 的产品与可靠性上位设计，但不能直接照搬其
`work/` 目录和 `Work` API。当前 Kernel 已经以 `Task` 作为唯一持久目标聚合，
再引入一套 `Work` 模型会违反“单一事实源”和增量迁移原则。

本轮采用以下规则：

1. `Work` 保留为产品概念，代码和公共 R0 API 继续使用 `Task`。
2. 吸收可靠运行、因果追踪、交付和验证语义，不进行一次性目录大迁移。
3. 当前先完成无 UI 的稳定 Kernel、Port、Lite Adapter 和 Chat 兼容路径。
4. Task Workbench、Inbox 页面和自动化页面只在基建门禁通过后恢复。
5. Workstation/Hub 本轮冻结公共契约，不提前实现远程队列或多租户控制面。

## 2. 当前 Goal 与详细方案的差异

| 主题 | 当前 Goal / 代码 | 详细方案新增 | 裁决 |
|---|---|---|---|
| 核心聚合 | `Task -> Run -> Plan/Checkpoint` 已落地 | 使用 `Work` 名称统一所有入口 | 保留 `Task` 为唯一代码聚合；文档注明概念映射 |
| 运行装配 | InvocationScope、generation、Slot、Lease 已落地 | Reliable Work Runtime | 继续沿现有 Runtime Assembly 演进，不新建平行 Runtime |
| 执行约束 | constraints、acceptance、approval level 已有 | autonomy、budget、retry、timeout、exit、required artifacts、verification policy | 吸收为类型化 Execution Contract，不塞入自由 metadata |
| 事件 | ExecutionEvent 有 task/run/sequence/actor | step、cause、correlation、source | 吸收到 Kernel 事件信封，并提供旧事件兼容读取 |
| 幂等 | Task 命令/API 与 SideEffect Record 已分离；工具按 effect 显式声明 | 外部副作用去重 | 已接真实 Tool 路径；后续把 Driver/MCP 原生调用统一接入同一 Broker |
| 审批 | 单一 Task Approval Broker、审批 Checkpoint、orphan recovery 与旧交互投影 | 严格模式真实端到端验收 | 已统一 Invocation 身份；Runtime 丢失时不会伪恢复 RUNNING |
| Artifact | 内容寻址、哈希校验、Renderer 已有 | version/status/supersedes/verification | 扩展 Artifact Registry 投影，不改变不可变 ArtifactRef |
| Evidence | EvidenceRef 已落地 | Verification 一等对象 | 新增 Verification 模型和 Port，结果完成不等于验收通过 |
| 交付 | 现有 inbox store 是独立旧实现 | Inbox 是事实源之上的 Delivery Projection | 吸收投影契约，禁止复制正文和独立改写 Task 状态 |
| 自动化 | Cron/Heartbeat 尚未统一 | Trigger 只创建标准 Work | 使用 Scheduler Port 创建 Task；Webhook 不直连 Tool/Harness |
| 能力复利 | Capability Catalog 管运行能力 | Retrospective、Candidate Scoring、复用指标 | 冻结扩展点，放到 R1/R2，不阻塞 Lite R0 基建迁移 |
| 产品配置 | Lite/Workstation/Hub Profile 已设计 | 三种自治与部署能力 | 吸收 L1/L2/L3 语义；Lite R0 只实现 L1 和受控 L2 |

## 3. 已经覆盖，无需重复建设

- `Task`、`Run`、`Plan`、`PlanStep`、`ExecutionCheckpoint` 及状态机。
- `ExecutionLedger`、SQLite Lite Adapter、投影快照和 API 幂等记录。
- `InvocationScope`、Capability Selection、generation 固定、lease 排空。
- 内置能力与插件能力共同使用 Contribution、Slot 和 Capability Catalog。
- `ArtifactRef` 内容哈希、`EvidenceRef`、Artifact Store 与安全 Renderer。
- Proposal 只能经审批转换为 TaskOrder，Sensor 不能直接执行。
- Lite / Workstation / Hub 共用 Kernel，以 Profile 和 Adapter 表达差异。

这些能力必须继续收口和迁移旧调用方，不能依据详细方案再实现一套同名系统。

## 4. 吸收到当前 R0 基建范围

### R0-A：Execution Contract

冻结类型化执行契约，至少覆盖 autonomy level、时间/Token/调用次数预算、重试、
超时、退出条件、必需 Artifact 和 Verification policy。Task、Chat 升级 Task、
Scheduler 和 Proposal 都只能引用同一契约。

当前吸收状态（2026-09-24）：Kernel 已形成唯一 `ExecutionContract`，Task API、
TaskService、Proposal/Sensor、旧 Chat 后台任务、TaskOrder 与 RuntimeContext 已贯通。
L3 对 Acceptance、Permission、时间/Token/费用/工具调用预算、Exit Conditions 与
Verification Policy 执行创建前门禁。Scheduler 尚待 I6 Port，预算计量、退出判定
与 Verification 执行仍是明确缺口，不能因 schema 存在而宣称可靠运行已完成。
其中持续时间预算已接入 Runner 的真实 deadline，并与 host/attempt timeout 取最
严格值；其余资源维度仍等待统一 Usage Meter。

### R0-B：Causal Identity

为 ExecutionEvent 增加可选的 `step_id`、`cause_event_id`、`correlation_id` 和
`source`。Invocation、Policy、Approval、Sandbox、Side Effect、Artifact 与
Verification 共享 correlation identity。新增字段保持向后兼容。

当前吸收状态（2026-09-24）：事件信封与 RunnerSignal 已包含四个可选字段，旧
事件缺失字段仍可读取；Runner、Approval 和 Side Effect 会把已有 correlation 与
source 写到事件顶层。Artifact/Verification 尚未完成领域模型，不能提前宣称整条
因果图闭环。每个新 Run 已有根 correlation，恢复 attempt 延续原链路；Ledger 对
`cause_event_id` 验证存在性、Task 归属和时间方向，只接受真实上游事件，不从事件
相邻关系猜测原因。

### R0-C：Governed Side Effects

新增 SideEffectRecord 与 Store Port，记录幂等键、输入摘要、状态、外部引用及
产生事件。高风险外部写入必须遵循：Policy -> Approval -> Sandbox/Executor ->
SideEffectRecord -> Audit Event。

### R0-D：Verification 与 Result Package

Verification 是独立领域结果，引用 Acceptance 与 Evidence。成功执行、Artifact
生成和验收通过是三个不同状态。Result Package 由投影组装，不复制事实数据。

### R0-E：Delivery Projection

冻结 Inbox Item/Delivery Port，使 Reply、Result、Approval、Exception 和
Artifact Ready 从领域事件生成投影。已读/已处理只改变投递状态，不能删除或
修改 Task、Approval、Artifact 的权威记录。

### R0-F：Artifact Registry 演进

保留不可变、内容寻址的 ArtifactRef；在 Registry Projection 中增加版本、状态、
supersedes、Verification 引用和 ownership。避免把可变生命周期塞回 ArtifactRef。

## 5. 延后但冻结边界

- Retrospective、Candidate Scoring、从 Task 发布 Workflow/Automation：R1。
- Workstation Durable Local Scheduler、Runner Pool、容器资源治理：R1。
- Hub 多租户、远程队列、RBAC、对象存储、故障转移：R2。
- Causal Graph、Recovery View、Cost View 等页面：Task UI 解冻后。
- `work/`、`delivery/` 等目录整体搬迁：不作为目标；只有新模块职责明确且旧调用方
  已迁移时才逐个建立，禁止空目录式架构重写。

## 6. 对当前 Goal 的处理

Codex Goal 的目标文本仍包含 Task Workbench 最终交付，这是整个任务的最终验收，
但当前用户优先级已经调整为“先完成基建，再恢复 Task 页面”。Goal 工具不支持在
active 状态改写 objective，因此不创建第二个 Goal，也不错误标记暂停或完成；
本文件和基建迁移计划作为当前执行顺序与新增验收的权威补充。

基建门禁通过前，不以页面组件存在、mock 数据或局部 API 可用宣称 Goal 完成。

## 7. 更新后的近端顺序

1. 完成 I1 Kernel 纯度、Session 取消/超时/close 语义和 Slot schema。
2. 完成 I2 Approval/Policy/Sandbox/Audit/Side Effect 单一治理链。
3. 完成 I3 剩余的 Scheduler 契约引用、预算/退出门禁与 Causal Event Envelope。
4. 完成 I4 Ledger、Artifact Registry、Evidence、Verification、Result Package。
5. 完成 I5 Tool/MCP/Memory/Driver/Harness 外设迁移。
6. 完成 I6 Scheduler、Chat/Channel/Cron 与 Delivery Projection。
7. 完成 I7 Plugin SDK、迁移工具和三种 Edition contract。
8. 通过基建门禁后恢复 Task Workbench。
