# QwenPaw 现状问题、解决思路与迭代方向

## 一、核心判断

QwenPaw 当前不是能力不足，而是能力很多，但没有形成清晰、可靠、可验证的
产品闭环。

项目已经具备 AgentScope 原生 Agent、工具、记忆、Skills、插件、Coding、
Goal、Cron、Harness、权限、Approval、Audit 和 Shadow Git Checkpoint。
这些能力构成了有价值的 Agent Runtime 基础，但用户看到的仍然是 Chat、
功能菜单和多个并行概念。

下一阶段不应继续横向堆功能，而应完成一个核心转变：

> 从“拥有很多能力的 AI 助手”，转向“能够主动承接工作、可靠交付结果，
> 并把成功经验转化为持续能力的 Agent 工作台”。

## 二、QwenPaw 当前的主要问题

### 1. 能力分散，工作闭环不完整

Chat、Goal、Mission、Coding、Cron、Harness、Approval 与 Checkpoint
各自承载部分状态，用户需要理解系统内部概念，才能判断一项工作在哪里、
是否完成以及结果是什么。

### 2. 产品仍以 Chat 为中心

Chat 适合交流和探索，但不适合独自承载长期任务的目标、计划、审批、恢复、
产物和验收。复杂工作容易退化成长消息流，“模型停止”也容易被误认为
“任务完成”。

### 3. 长任务和无人值守不够可靠

工具调用和 Harness 已经存在，但后台执行、进程重启恢复、幂等重试、预算、
退出条件和异常接管还没有形成统一运行契约。

### 4. 轨迹可见，但因果和结果不清晰

Policy、Approval、Audit、Checkpoint、Artifact 与 Verification 各自存在，
却没有形成完整因果链。用户难以回答为什么执行、修改了什么、如何恢复、
根据什么证据判断完成。

### 5. 一次任务难以转化为长期能力

成功流程、失败经验和验证方法容易散落在对话中。Workflow、Automation、
Skill、Tool 和 Plugin 缺少统一的沉淀、验证、分发和反馈机制。

## 三、用户真正需要什么

用户不关心内部 Runtime 有多少模式，而关心三个问题：

### 如何方便地开始

- 简单问题直接 Chat；
- 明确交付直接创建 Task；
- Chat 中出现复杂工作时可无损升级为 Task；
- 重复工作可以从成功 Task 创建 Automation；
- 系统自动建议模式，用户不必先判断复杂度。

### 如何可靠地完成

- 简单工作即时完成；
- 复杂工作在后台持续执行；
- 成熟流程可以无人值守；
- 正常情况不打扰，异常和越权时请求接管；
- 失败可重试、恢复或回到 Checkpoint；
- 完成前必须执行 Acceptance 和 Verification。

### 如何清楚地查看结果

- Chat 查看即时回答；
- Task 查看目标、计划和执行状态；
- Timeline 查看发生了什么；
- Causal Graph 和 Evidence 解释为什么；
- Inbox 接收异步回复、结果、审批和异常；
- Artifact 查看最终交付物、版本与验证状态。

## 四、QwenPaw 应如何解决

### 方向一：建立 Chat 与 Task 双入口、统一 Work

Chat 和 Task 并列，不互相替代：

- Chat 用于交流、探索和快速执行；
- Task 用于目标明确、可计划、可恢复和无人值守的工作；
- Automation 通过定时或事件创建标准 Work；
- 所有复杂工作共享 Goal、Acceptance、Plan、Run、Approval、Artifact、
  Evidence、Verification 和 Timeline。

Chat 是交互方式，Task 是工作入口，Cron 与 Webhook 是触发方式，Harness
是执行后端，Work 才是唯一事实源。

### 方向二：建设可靠运行与因果可观测

QwenPaw 需要从一次 Agent Run 升级为对完整 Work 负责：

- 后台持久运行与重启恢复；
- 超时、重试、退避和幂等副作用；
- 时间、Token、费用和权限预算；
- L1 交互、L2 受控后台、L3 无人值守；
- Tool、Policy、Approval、Side Effect、Audit、Checkpoint、Artifact 与
  Verification 形成统一因果链。

Timeline 说明发生了什么，Causal Graph 说明为什么发生以及如何恢复。

### 方向三：让结果成为一级产品对象

Inbox 不取代 Chat，而负责结果投递：

- 异步 Chat Reply；
- Task 和 Automation Result；
- Approval；
- Exception；
- Artifact Ready 与 Verification Result。

Inbox 不记录每个 Tool Call 和普通 Step，避免退化成通知中心。

Artifact 也不只是附件或文件列表，而应包含：

- 来源 Work、Run 和 Step；
- 版本与状态；
- Evidence 与 Verification；
- 验收、修改、分享、归档与复用入口。

### 方向四：建立能力复用与稳定扩展

任务完成后生成结构化复盘，将经验转化为：

- Knowledge；
- Template；
- Skill；
- Workflow；
- Tool；
- Automation；
- Plugin。

评分决定是否值得沉淀，类型规则决定沉淀形式，验证与权限门槛决定能否
发布。插件需要版本化 Manifest、标准 Slot、权限声明、热加载边界、健康
检查和回滚机制。

## 五、迭代路线

### P0：完成用户工作闭环

目标：

- Chat 与 Task 双入口；
- 统一 Work 身份和状态；
- Chat 可升级为 Task；
- Inbox 可以接收回复、结果、审批和异常；
- Artifact 可以追溯到来源和验证结果；
- 刷新或重启后任务能够恢复。

用户价值：

> 更容易开始，离开后不丢结果，回来即可继续处理。

### P1：加强无人值守与可信执行

目标：

- 后台持久运行；
- L1–L3 自治等级；
- 重试、预算、退出和人工升级条件；
- 幂等副作用；
- 完整 Causal Work Graph；
- 完成前自动验证。

用户价值：

> 不必持续盯守，同时知道系统做了什么、为什么做、结果是否可信。

### P1：稳定插件契约与工作台自动化

目标：

- 版本化 Manifest、SDK 与标准 Slot；
- 官方 Tool、Memory、Channel 和 PawApp 迁移；
- Workstation 支持插件开发、热安装和定点验证；
- 定时、Git、本地事件和 Webhook 均创建标准 Work；
- 外部 Harness 和 Plugin 不绕过治理链。

用户价值：

> 能力安装即可使用，开发者更容易扩展，升级和运行风险更可控。

### P2：形成能力复用飞轮与 Hub

目标：

- 任务复盘与复用候选；
- Capability Registry；
- 使用反馈驱动升级、降级和废弃；
- Hub 支持外部事件、多用户、多仓库和多执行节点；
- 团队审批、审计、配额与能力分发。

用户价值：

> QwenPaw 越用越懂用户，成功工作可以自动转化为下一次能力。

## 六、Lite、Workstation 与 Hub

三者是同一 Runtime 的产品配置，不是三套代码。

| 配置 | 核心定位 | 主要价值 |
|---|---|---|
| Lite | 轻量个人 Agent | 快速 Chat、轻量 Task、个人 Inbox 与 Artifact |
| Workstation | 本地与内网开发工作台 | 仓库、Coding、后台任务、本地自动化和插件开发 |
| Hub | 外部事件与团队协作中枢 | Webhook、集中调度、团队治理和能力分发 |

插件主要在 Workstation 开发和验证，在 Hub 分发和治理，Lite 以安装和使用
为主。

## 七、QwenPaw 的差异与用户价值

### 与普通 Chat 助手相比

普通 Chat 以消息为中心；QwenPaw 同时支持 Chat 与 Task，并具备后台执行、
恢复、Artifact 和 Verification。

用户价值：

> 不只是获得回答，而是得到可以验收和交付的结果。

### 与单一 Coding Agent 相比

Coding Agent 聚焦代码执行；QwenPaw 提供本地优先的统一 Work Runtime，
能够承载代码、文档、知识工作、自动化和长期任务。

用户价值：

> 不需要为不同任务切换多套孤立工具。

### 与多 Agent 聚合或控制台相比

QwenPaw 不把“接入更多 Agent”作为核心价值，而强调统一 Work、可信执行、
因果可观测和结果验证。

用户价值：

> 用户不需要管理 Agent，只需要管理目标、例外和结果。

### 与普通自动化平台相比

传统自动化强调固定流程；QwenPaw 将 Agent 判断、Workflow、Policy、
Approval、Recovery、Artifact 和 Verification 放进同一闭环。

用户价值：

> 自动化不仅能运行，还能解释、恢复、验收并沉淀经验。

## 八、衡量是否成功

- 首次完成真实工作所需步骤；
- 任务验收通过率；
- 后台和无人值守完成率；
- 失败恢复率；
- 重复副作用发生率；
- Inbox 结果投递成功率与噪声率；
- Artifact 可追溯率与 Verification 覆盖率；
- 用户理解结果所需时间；
- 能力复用率与复用成功率；
- 插件安装、升级和回滚成功率。

## 九、现阶段明确不做什么

- 不用 Inbox 替代 Chat；
- 不强迫所有问题创建 Task；
- 不让 Chat 继续承担全部状态和产物管理；
- 不新建平行的 Task Runtime 或 Automation Runtime；
- 不把 Cron 等同于完整自动化；
- 不让 Webhook、Plugin 或 Harness 绕过治理链；
- 不把 Inbox 做成全量通知中心；
- 不把 Artifact 做成缺少来源和验证的文件列表；
- 不维护 Lite、Workstation、Hub 三套代码分叉；
- 不以长尾集成或插件数量作为主要竞争力。

## 十、最终判断

QwenPaw 应围绕一条简单的用户价值链建设：

> 让用户容易开始，让 QwenPaw 可靠完成，让结果清楚到达，让成功经验持续
> 复用。

对应的产品关系是：

    Chat 与 Task 并列
    Automation 创建工作
    Work Runtime 统一执行
    Causal Graph 解释过程
    Inbox 投递回复和结果
    Artifact 承载可验证价值
    Capability Flywheel 形成持续复利
