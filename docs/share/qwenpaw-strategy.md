# QwenPaw 产品定位、能力建设与迭代路线

## 一、结论先行

QwenPaw 当前的核心问题不是能力不足，而是已有能力尚未形成清晰、可靠、
可恢复、可验证并且能够持续复用的产品闭环。

main 分支已经具备 AgentScope 原生 Agent、工具调用、记忆、Skills、插件、
渠道、Coding、权限控制、审计、Approval 与 Shadow Git Checkpoint 等基础
能力。它真正有价值的资产，是一个可以继续演进的 Agent Runtime，而不是
一个单纯的聊天应用、外部 Coding Agent 启动器或多 Agent 控制台。

下一阶段不应继续横向堆叠 Channel、Provider、Mode 和孤立功能，而应完成
五个产品转变：

1. 从 Chat Session 中心转向统一 Work/Task 中心；
2. 从模型停止输出转向结果经过验证；
3. 从分散的安全组件转向端到端可信执行；
4. 从一次性完成任务转向知识、流程和能力持续复用；
5. 从被动等待对话转向能够承接定时和事件驱动工作的 Agent 工作台。

建议的统一定位是：

> QwenPaw 是基于 AgentScope、本地优先、能够持续完成并验证真实工作的
> Agent 工作台。它既能服务个人和本地开发，也能通过 Hub 承接外部事件与
> 团队协作，并为二次开发者提供稳定、可治理的扩展平台。

QwenPaw 的差异不应建立在“功能更多”，而应建立在“对真实工作结果负责”。

## 二、已经拥有的真实资产

### 1. 原生 Agent 与 Runtime

- QwenPawAgent 基于 AgentScope，已覆盖模型、工具、记忆、Scroll、
  Tool Guard 与 Coding 等能力；
- Runtime 已具备分阶段生命周期、Hook、Prompt Contributor、工具、模式与
  中间件等扩展点；
- 这些能力使 QwenPaw 可以形成自己的 Agent Runtime，而无需退化为外部
  Harness 的壳。

### 2. 安全、治理与恢复基础

- 已存在 Sandbox、Tool/File Guard、Access Policy、Approval 与 Audit；
- Shadow Git Checkpoint 可以为文件操作提供追踪和恢复；
- 现有组件虽然尚未串成完整体验，但已经具备构建可信执行差异的基础。

### 3. 扩展与生态基础

- 已支持 Skills、插件、渠道、模型和应用类扩展；
- 已存在较广泛的 Plugin API 与 Registry；
- 测试资产并不薄弱：基线中约有 785 个 Python 测试文件和 385 个 Console
  测试文件。

### 4. 工作与自动化基础

- 已有 Goal、Mission、Cron、Harness、Coding 和 Checkpoint 等工作能力；
- 已经具备定时执行与外部 Harness 适配基础；
- 当前缺口不是重新发明自动化，而是将这些入口统一到同一 Work、审批、
  Artifact、Evidence 和 Verification 链路中。

## 三、用户真正需要什么

### 1. 个人用户

个人用户需要的不是更多菜单，而是能够把一件事情放心交给 QwenPaw：

- 说清目标后看到计划与验收条件；
- 知道任务为何运行、当前进行到哪里以及为何暂停；
- 在必要时完成审批或人工接管；
- 失败或重启后能够恢复；
- 最终获得可打开的 Artifact 与验证证据；
- 成功经验能够在后续任务中复用。

### 2. 本地与内网开发者

开发者需要稳定边界、低成本调试和升级确定性：

- 本地仓库、IDE、终端、Git Hook 和文件系统能够自然接入；
- Plugin Manifest、Slot、权限和生命周期有稳定契约；
- 插件可以开发、调试、热安装、禁用和回滚；
- 外部 Harness 作为受治理的执行器，而不是绕过 Runtime；
- 定时任务、仓库事件和内部事件最终都形成可查看的 Work。

### 3. 团队与外部协作

团队需要一个持续在线的入口承接外部开发事件：

- 接收 GitHub、GitLab、Gitee、CI/CD、PR 和 Issue 事件；
- 对事件进行身份校验、权限判断、队列调度和并发控制；
- 将工作分配给合适的执行节点；
- 集中展示审批、审计、Artifact 和 Verification；
- 在组织范围内分发经过验证的 Workflow、Automation 和 Plugin。

## 四、四个主要矛盾

### 矛盾一：能力丰富，但工作闭环不完整

用户仍然难以判断目标、进度、暂停原因、待审批事项、产物以及验收结果。
QwenPaw 必须从“功能是否存在”转向“工作是否闭环”。

### 矛盾二：扩展面广，但公共契约不稳定

插件能接入不等于能够放心发布。版本、权限、Slot、生命周期、兼容性、
健康检查和开发工具链尚未成为稳定产品能力。

### 矛盾三：安全组件较强，但可信体验割裂

Guard、Policy、Approval、Audit 和 Checkpoint 各自存在，但用户还看不到
完整因果链。每次副作用都应回答：谁发起、为何允许、批准了什么、修改了
什么、如何恢复、结果是否验证。

### 矛盾四：任务可以完成，但经验无法持续复利

任务总结、有效流程、失败经验和验证方法容易散落在对话中。定时任务也只是
按时间重复执行，尚未形成事件驱动、可复用、可治理的 Automation。

## 五、统一产品与能力边界

Lite、Workstation 和 Hub 应当是同一 Runtime、同一领域模型和同一插件契约
下的三种产品配置，而不是三套平行产品或代码分叉。

### Lite：轻量个人 Agent

Lite 面向个人和轻量任务，强调安装后即可使用：

- 对话和手动任务；
- 统一任务视图、审批、Artifact 和 Verification；
- 简单定时任务；
- 轻量 Skill、Tool 与 Workflow；
- 本地优先，不承担公网 Webhook 和组织级调度。

### Workstation：本地与内网开发工作台

Workstation 面向专业开发者和本地技术团队：

- 多仓库、IDE、终端和本地 Git 集成；
- Git Hook、文件变化、本地事件和定时任务；
- 长任务、后台恢复、Sandbox 与 Checkpoint；
- Coding Agent 与 Harness 作为可替换执行后端；
- 插件开发、调试、热安装和健康检查；
- 可连接内网 GitLab、CI 或通过安全 Relay 接收外部事件。

### Hub：外部事件与团队协作中枢

Hub 面向持续在线的外部集成和团队治理：

- 公网 Webhook 与外部平台连接；
- 多用户、多仓库、多项目；
- 集中队列、并发调度和执行节点管理；
- 团队权限、审批、审计和配额；
- Automation、Workflow 与 Plugin 的组织级分发；
- 将任务分配给 Hub Worker 或 Workstation 执行。

三者的区别是部署方式、信任边界和运行规模，而不是核心能力模型不同。

## 六、统一工作闭环

所有工作来源都必须进入统一 Work/Task：

    用户对话 / 手动创建 / 定时任务 / Webhook
    Git 或文件事件 / QwenPaw 内部事件 / 插件事件
                              ↓
                         Unified Work
                              ↓
         Goal → Acceptance → Plan → Run → Recovery
                              ↓
       Policy → Approval → Side Effect → Audit → Checkpoint
                              ↓
              Artifact → Evidence → Verification
                              ↓
        Retrospective → Reuse Candidate → Capability Registry

Chat、Goal、Mission、Coding、Cron 和 Harness 可以保留为交互入口、触发方式或
执行策略，但不能继续维护彼此独立的事实源。

## 七、七个迭代方向

### 方向一：加强 Agent 原生完成能力

保留 AgentScope 和 QwenPawAgent 作为核心：

- 将用户意图转为明确目标和验收条件；
- 生成可更新、暂停和恢复的计划；
- 按当前步骤注入上下文，控制上下文膨胀；
- 工具失败、网络中断和进程重启后能够继续；
- 完成前主动验证，而不是以模型停止作为完成；
- 外部 Harness 由原生 Runtime 统一治理。

### 方向二：建立统一 Work/Task 对象

在现有 Runtime 内建立唯一工作身份与生命周期：

- Goal 与 Acceptance；
- Plan 与当前步骤；
- Run 状态与恢复点；
- Approval；
- Artifact；
- Evidence 与 Verification；
- Event Timeline。

工作台成为所有真实工作的统一入口；Chat 是工作对象的一种交互视图。

### 方向三：串联可信执行链

形成统一链路：

    Work ID → Tool Call → Policy → Approval → Side Effect
            → Audit → Checkpoint → Verification

重点建设审批持久化、重启恢复、审计关联 Work/Run/Artifact、Checkpoint 与
任务阶段绑定，以及结果验证记录可查看。

### 方向四：收敛为稳定插件契约

建立版本化 Plugin Manifest 与 SDK，明确：

- API 与 Manifest Schema 版本；
- Tool、Memory、Channel、Trigger、UI Slot、PawApp 与 Executor；
- 权限和资源声明；
- 安装即生效、热重载和必须重启的边界；
- 健康检查、隔离、禁用和回滚；
- 兼容性测试与官方示例。

应迁移真实官方 Tool、Memory、Channel 和 PawApp，以证明内置与外部插件遵循
同一契约。

### 方向五：建设工作台自动化

自动化是工作台能力，不是另一套 Runtime。它负责从时间和事件中创建标准
Work，并继续使用相同的审批、恢复、Artifact 与 Verification。

必须明确区分：

- 定时任务：由时间触发，适合日报、周期检查和定期维护；
- 事件自动化：由 PR、Issue、Push、Review Request、文件变化、任务完成或
  验证失败等事件触发；
- Workflow：定义工作按哪些步骤执行；
- Tool：完成单个确定动作。

Lite 支持手动和简单定时；Workstation 重点支持本地、Git 与内网事件；
Hub 重点支持公网 Webhook、外部开发平台和团队级调度。

### 方向六：建立知识与能力复用闭环

任务完成后不只交付 Artifact，还应形成结构化复盘：

- 目标、结果和验证证据；
- 成功步骤与失败原因；
- 可复用规则、命令、模板和流程；
- 适用条件、不适用条件和依赖；
- Knowledge、Checklist、Template、Skill、Workflow、Tool 或 Automation
  候选。

评分只决定是否值得进入候选池；资产类型由输入输出确定性、语义判断强度和
副作用决定；正式发布还必须通过验证和权限门槛。

后续应以复用次数、成功率、节省时间、人工修改率和失效率持续调整推荐、
升级或废弃。

### 方向七：降低默认使用与安装成本

- 默认入口只突出开始工作、查看进行中、处理审批和查看产物；
- 模型、Agent、Harness 和插件细节进入高级设置；
- 长尾渠道、浏览器集成、外部 Harness 和重型依赖按需安装；
- Lite、Workstation 和 Hub 共享核心，通过配置与插件形成差异；
- 先收敛可靠闭环，再扩展场景数量。

## 八、分阶段路线与验收

### P0：统一身份、状态与任务视图

目标：对话、计划、执行、审批、Checkpoint、Artifact 和 Verification 贯穿
同一 Work。

验收：

- 状态只有一个事实源；
- 刷新或重启后仍能恢复；
- 待审批事项在任务视图可见；
- Artifact 可追溯到执行步骤和证据；
- 手动任务与定时任务使用同一 Work 模型。

### P1：原生完成能力与可信执行

目标：QwenPawAgent 能够持续完成长任务，并对结果进行验证。

验收：

- 工具失败可以恢复；
- 计划可以调整；
- 完成前执行 Acceptance；
- 每个副作用能够关联 Policy、Approval、Audit 和 Checkpoint；
- 外部 Harness 不绕过治理链。

### P1：稳定插件契约

目标：二次开发者无需理解核心实现即可发布可靠扩展。

验收：

- 版本、权限、Slot、生命周期、健康与回滚规则明确；
- 至少四类真实官方能力完成迁移；
- 插件按 Manifest 声明安装后生效；
- Workstation 可完成开发、调试和定点验证；
- Hub 可以进行组织级分发与治理。

### P1：工作台自动化基础

目标：时间和外部事件可以创建标准 Work。

验收：

- 定时任务与事件自动化在产品概念上明确区分；
- 首批支持 PR、Issue、Push 和 QwenPaw 内部事件；
- 用户能够看到触发来源和原因；
- 相同事件不会重复创建 Work；
- 自动化可暂停、恢复和人工接管；
- 外部写入仍经过策略或审批。

### P2：知识复用与能力飞轮

目标：经过验证的任务经验能够安全转化为复用资产。

验收：

- 任务结束生成结构化复盘和复用候选；
- Knowledge、Skill、Workflow、Tool 与 Automation 边界清晰；
- 候选可以追溯到来源任务和证据；
- 至少一个真实任务完成“执行—沉淀—复用—反馈”闭环；
- 过期或失败资产能够降级、重新验证或停用。

### P2：Hub 与场景包

目标：在核心闭环稳定后承接团队和外部开发事件。

验收：

- Hub 可以持续接收 Webhook 并集中调度；
- Workstation 可以作为受治理的执行节点；
- 团队审批、审计和能力分发生效；
- 围绕代码仓库维护和个人知识工作形成少量完整场景包。

## 九、产品度量

评估 QwenPaw 不应只看模型回答质量或任务数量，还应关注：

- 任务验收通过率；
- 失败恢复率；
- Artifact 可追溯率；
- 副作用审批与审计覆盖率；
- 自动化成功率和人工接管率；
- 复用候选采纳率与后续成功率；
- 首次完成真实任务所需时间；
- 平均上下文成本和执行成本；
- 插件安装、升级与回滚成功率。

## 十、现阶段明确不做什么

- 不继续把增加长尾 Channel、Provider 和 Mode 当作主要竞争力；
- 不把 QwenPaw 做成多 Agent 控制面板或聚合应用；
- 不新建与现有 Runtime 平行的任务或自动化 Runtime；
- 不把定时任务等同于完整工作台自动化；
- 不让 Webhook 或 Harness 绕过统一 Work、安全与审计链；
- 不与 DSH 比拼插件数量，先证明契约稳定和真实能力迁移；
- 不维护 Lite、Workstation、Hub 三套代码分叉；
- 审批仍为临时状态时，不宣称具备完整审计闭环；
- 官方能力未遵循统一契约前，不宣称 SDK 已稳定；
- 未经过真实复用和验证，不把任务总结自动升级为正式工具或插件。

## 十一、最终判断

QwenPaw 的核心差异应该由以下组合构成：

1. AgentScope 驱动的原生 Agent；
2. 本地优先、长期运行、可恢复；
3. 从目标到验证的统一 Work 闭环；
4. 从权限、审批、审计到 Checkpoint 的可信执行；
5. 稳定、平等、受治理的插件扩展；
6. 定时与事件驱动的工作台自动化；
7. 从任务复盘到能力复用的持续飞轮；
8. Lite、Workstation、Hub 在同一核心上的渐进部署。

最重要的产品转变是：

> 从一个拥有很多能力的 AI 助手，转向一个能够主动承接工作、可靠交付结果、
> 并把每次成功转化为下一次能力的 Agent 工作台。
