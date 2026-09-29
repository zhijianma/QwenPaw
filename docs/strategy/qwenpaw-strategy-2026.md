# QwenPaw 2026–2028 深度战略研究

> 版本：第二轮研究稿（2026-09-21）
> 目的：回答 QwenPaw 后续“为谁解决什么问题、靠什么形成优势、哪些事情不做”。
> 方法：竞品功能与架构对比、公开市场研究、QwenPaw Issue 标题聚类、仓库资产与变更结构分析。
> 注意：GitHub star、代码量和 Issue 关键词聚类仅用于判断关注度与维护面，不等同于产品质量、活跃用户或市场份额。

## 一、执行结论

QwenPaw 不应继续以“功能最全的开源个人 Agent OS”为主叙事，也不应把“轻量”“多 Harness 控制平面”“长期记忆”中的任何一个单独作为战略终点。这些方向要么已经拥挤，要么容易被上游框架和模型能力商品化。

更有机会的定位是：

> **QwenPaw 是本地优先的 AI 工作空间：接住用户的文件、环境、权限和长期任务，调度可替换的 Agent 引擎，最终交付可检查、可继续、可追溯的工作成果。**

一句更短的产品原则：

> **QwenPaw owns the workspace and deliverables; models and harnesses are replaceable engines.**

这不是简单改一句 Slogan，而是一次产品重心迁移：

- 从“聊天入口”迁移到“项目 / 任务 / 成果入口”；
- 从“拥有最多能力”迁移到“交付闭环最可靠”；
- 从“自己实现所有 Agent 能力”迁移到“定义工作空间和引擎契约”；
- 从“所有功能默认捆绑”迁移到“精简内核 + 按需 App Pack”；
- 从“消息数、工具数、模型数”迁移到“每周已验证成果数、任务恢复率、返工率”。

首个切入人群不建议选大型企业，也不建议与 Codex、Claude Code、Cursor 正面争夺专业编码主入口。优先服务：

1. 有大量本地文件和敏感资料的技术型个人用户；
2. 需要连续数小时或数天完成研究、数据整理、报告和内容生产的小团队；
3. 已经同时使用多个模型或 Coding Agent，但缺少统一任务上下文与成果归档的高级用户。

首个可验证场景应是“给定本地资料与目标，连续完成研究 / 数据 / 文档类交付”，而不是再造一个通用聊天机器人。Coding Harness 作为执行引擎接入，而不是 QwenPaw 的唯一产品身份。

## 二、市场发生了什么

### 2.1 供给侧：Agent 能力正在快速商品化

2026 年的竞争单位已从“模型 + 聊天框”变为四层：

1. **模型层**：推理、工具调用、长上下文和多模态持续升级；
2. **Harness 层**：Codex、Claude Code、Gemini CLI、Qwen Code、OpenCode 等负责 Agent Loop；
3. **控制与治理层**：OpenHands、Paperclip、JetBrains Central、Factory 等管理并行任务、成本、权限和团队；
4. **工作与成果层**：IDE、代码仓、文件夹、文档、数据集和业务系统才是用户真正要完成工作的地方。

前三层都在加速拥挤。QwenPaw 如果继续用“我也有记忆、MCP、多 Agent、渠道、定时任务”参与功能清单竞争，会同时面对开源项目的迭代速度和商业产品的资金投入，且难以形成清晰购买理由。

### 2.2 需求侧：使用率上升，但信任和组织收益没有同步上升

公开研究呈现出一个一致矛盾：

- JetBrains 2026 年调研显示，AI 编码工具已进入专业开发者的常规工作，但多工具并存也带来了成本、权限和管理问题；
- Stack Overflow 2025 调研中，84% 的受访者正在使用或计划使用 AI 工具，但对准确性“不信任”的比例高于“信任”；87% 担心准确性，81% 担心安全与隐私；只有 17% 认为 Agent 改善了团队协作；
- DORA 2025 将 AI 定义为组织能力的“放大器”：它同时放大已有优势和已有混乱；
- METR 对成熟开源仓库的随机对照研究中，开发者主观认为 AI 提速，实际样本却出现 19% 的完成时间增加。该研究不能外推到所有开发，但足以证明“用户感觉快”不等于“真实交付快”。

因此，下一阶段真实需求不是“再多一个会做事的 Agent”，而是：

- 我能否知道它现在在做什么；
- 中断、崩溃或换模型后能否继续；
- 它是否重复执行、越权、泄露资料或无限消耗；
- 输出如何被检查、修改、签收和复用；
- 多个 Agent 的工作是否沉淀到同一个项目，而不是散落在聊天记录中；
- 团队能否看到成果、证据、成本和责任边界。

### 2.3 用户关注点的优先级

| 优先级 | 用户真正关心的问题 | 常见产品误解 |
|---|---|---|
| P0 | 任务不丢、不重复、不失控，停止命令真的停止 | 只要模型更强就会自然解决 |
| P0 | 文件、密钥、权限、网络和执行环境可控 | 有一个 allow/deny 页面就算安全 |
| P1 | 长任务可以恢复、接管、回看和校验 | 保存聊天记录等于保存任务状态 |
| P1 | 输出是可交付的文件、代码、报告或数据 | 一段看起来不错的回答就是成果 |
| P1 | 安装、升级、诊断和资源占用可接受 | “开源可部署”就等于容易使用 |
| P2 | 可更换模型、Harness 和工具而不丢上下文 | 支持很多 Provider 就等于可迁移 |
| P2 | 团队能治理预算、策略和共享资源 | 多用户登录就等于团队协作 |

## 三、竞品地图：38 个代表产品，8 个竞争簇

### 3.1 Coding Harness 与终端 Agent

| 产品 | 核心占位 | 对 QwenPaw 的含义 |
|---|---|---|
| OpenAI Codex | CLI / SDK / App Server，线程恢复、审批、沙箱 | Agent 引擎趋向标准组件，QwenPaw 不必复制 Loop |
| Claude Code | 深度可定制：规则、Skills、Subagents、Hooks | “可扩展 Harness”已不是差异点 |
| Gemini CLI | MCP、扩展、checkpoint、sandbox、企业能力 | 开源 CLI 已覆盖恢复与治理基础能力 |
| Qwen Code | Auto-Memory、Skills、Teams、渠道、Desktop/Web/IDE | 与 QwenPaw 同生态重叠最大，必须明确分工 |
| OpenCode | plan/build Agent、桌面端、多 Provider | 通用 Harness 的进入门槛继续下降 |
| Aider | Git 原生、命令行、模型广泛兼容 | 轻量与模型中立已有成熟代表 |
| Cline | IDE/CLI/Desktop/SDK、checkpoints、团队、调度 | 从插件向完整 Agent 平台扩张 |
| Roo Code | IDE Agent、多模式和丰富配置 | 功能广度不构成长期壁垒 |
| Continue | IDE 内 Agent、规则、模型与上下文 | IDE 分发入口竞争激烈 |
| Goose | Rust 本地运行、多 Provider、MCP 扩展 | 本地、轻量、开放生态已有强对手 |
| Kimi CLI | Shell、ACP、MCP | 单一 CLI 产品易被更大产品线吸收 |
| ZCode | Desktop/Web/Terminal 一体的 AI 编程工作台与自研 Agent Runtime | 已直接占据 Workspace、Task、Goal Mode、远程开发和浏览器联调场景 |
| DSH | “Everything is a plugin”的可组合 Harness | 插件化本身不能成为终局定位 |
| Pi | Runtime / State，强调容器隔离 | 极简内核仍需外部产品层承接安全与体验 |

ZCode 尤其值得单独看待：它不是一个轻量 CLI，而是 Electron Desktop、Web、终端 Agent、后端和共享 UI 组成的完整 Agent Development Environment。其工作空间任务列表、Git 分支、SSH/Docker 远程环境、内置浏览器、终端、变更审阅、Goal Mode、状态恢复和 Subagent 已经覆盖了“AI 编程工作台”的主要对象。

**判断：** 不建议 QwenPaw 争夺“最强 Coding Harness”或“通用 AI 编程工作台”。应把 Codex、Qwen Code、Qoder、DSH 等视为可替换引擎，用统一契约承接任务、文件、权限、事件、成本和成果。QwenPaw 的 Workspace 战略若成立，必须向本地私有的研究、数据、文档和混合知识工作延伸，不能把“有 Workspace”本身当成差异。

### 3.2 自治软件工程与云任务

| 产品 | 核心占位 | 对 QwenPaw 的含义 |
|---|---|---|
| OpenHands | 自托管 Agent Canvas、Agent Server、多执行后端 | 已从单 Agent 转向多 Agent 工作台 |
| SWE-agent / mini-SWE-agent | 用极简 Harness 完成软件工程任务 | 证明“更多框架代码”不必然带来更好效果 |
| Cursor Cloud Agents | 隔离 VM、长任务、截图/视频/日志证据、接管 | 商业产品已占领云编码成果验证体验 |
| Factory | 完整 SDLC、Missions、企业部署和气隙环境 | 大企业软件工厂需要重投入销售与交付 |
| JetBrains Central | 多 Agent 控制与执行平面、成本和治理 | “开发团队多 Harness 控制台”已出现平台级玩家 |

**判断：** 多 Harness 控制平面是需求，但不是空白市场。QwenPaw 可做个人和小团队的“工作空间级引擎切换”，不应直接对标大型软件工厂。

### 3.3 个人 Agent OS 与多渠道助手

| 产品 | 核心占位 | 对 QwenPaw 的含义 |
|---|---|---|
| OpenClaw | 本地 Gateway、工具、会话、20+ 渠道、ClawHub | “本地 + 渠道 + Skill”竞争激烈 |
| Hermes Agent | 记忆、自动生成 Skill、终端后端、渠道和 Cron | 自我进化叙事已被占用 |
| nanobot | 小内核、工具、记忆、MCP、渠道、Web UI | 轻量通用个人 Agent 已有直接对手 |
| Open Interpreter | 本地自然语言控制计算机 | 计算机操作不是独占能力 |
| browser-use | 浏览器 Agent 基础设施 | 浏览器执行更适合作为组件，而非主定位 |

**判断：** “更轻的 OpenClaw”不是足够强的战略。轻量化必须服务于明确场景，例如 10 分钟内从本地文件夹产出第一个可验证成果。

### 3.4 记忆、身份与连续性

| 产品 | 核心占位 | 对 QwenPaw 的含义 |
|---|---|---|
| ReMe | 本地文件原生 Markdown 记忆、来源保留、跨 Agent | 是 QwenPaw 的上游资产，不宜重复造轮子 |
| Mem0 | 用户 / 会话 / Agent 多层记忆，云与自托管 | 记忆 API 已高度产品化 |
| Letta / Letta Code | Stateful Agent、自修改记忆、Git-backed MemFS | “有身份、会成长的 Agent”已有清晰品牌 |
| Supermemory | 跨 Claude/Cursor/Codex/OpenCode 等的统一记忆 | 跨 Agent 便携记忆也已出现专门产品 |
| Zep | 面向 Agent 的记忆与知识图谱 | 企业记忆基础设施已有成熟竞争者 |

**判断：** 记忆是 QwenPaw 的关键基础设施和体验加速器，但不应单独作为市场定位。差异应落在“任务连续性 + 成果来源 + 可编辑工作空间”，而不是抽象的“永不遗忘”。

### 3.5 多 Agent、组织与治理

| 产品 | 核心占位 | 对 QwenPaw 的含义 |
|---|---|---|
| Paperclip | Org chart、预算、治理、审批、审计、恢复、插件 | 多 Agent 公司控制平面已相当完整 |
| Multica | 多 Agent 并发和任务编排 | 并行 Agent 不是独特卖点 |
| AutoGen | 多 Agent 对话和应用框架 | 框架层竞争成熟 |
| CrewAI | Role / Crew / Flow | 角色编排已有广泛认知 |
| LangGraph | 图状态、持久化、Human-in-the-loop | 状态机与流程编排已有事实标准候选 |
| AgentScope 2.0 | Agent、Team、Service、Sandbox、Memory、Channels | QwenPaw 应消费其通用能力，而非重复实现 |

**判断：** QwenPaw 的多 Agent 应被降级为“完成一个工作空间任务的执行方式”，而不是产品首页上的核心卖点。

### 3.6 企业 Agent 与工作流平台

| 产品 | 核心占位 | 对 QwenPaw 的含义 |
|---|---|---|
| Dify | Workflow、RAG、Agent、模型管理、观测、企业版 | 通用企业 AI 平台竞争强 |
| n8n | 1500+ 集成、9000+ 模板、可视化流程、RBAC | 集成与工作流数量难以追赶 |
| Flowise | 可视化 LLM / Agent Flow | 低代码流程已高度同质化 |
| MaxKB | 企业知识库与问答 | 知识库本身不是新市场 |
| AgentScope | 多租户 Agent Service 与资源共享 | 企业 Runtime 能力应由上游承接 |

**判断：** 不应把 QwenPaw 演变成另一套 Dify。企业能力只在个人/小团队交付闭环被验证后逐层增加。

### 3.7 本地知识与私有助手

| 产品 | 核心占位 | 对 QwenPaw 的含义 |
|---|---|---|
| AnythingLLM | 本地私有 Chat、Agent、Flow、MCP、多人 | “本地私有知识助手”已有强认知 |
| Ollama 生态 | 本地模型分发和运行事实入口 | QwenPaw 应优化消费体验，不必拥有模型运行层 |

**判断：** 本地优先是必要条件，不是充分定位。用户买的是“私密资料变成成果”，不是“本地部署”四个字。

### 3.8 QwenPaw 相邻的垂直应用

QwenPaw 已经出现 Creator、Data、Mail、Insight 等 PawApp 方向。它们比继续增加底层 Agent 功能更接近用户愿意付费的结果，但必须避免把每个 App 都做成一个维护沉重的半成品。

建议把垂直应用定义为可安装的 **Outcome Pack**：包含任务模板、所需 Skill、权限策略、成果 Schema、验收规则和示例数据，而不是仅增加一个新路由和新聊天提示词。

但 Research、Data 和 Artifact 也不是空白市场：NotebookLM 已将多来源研究推进到带引用的报告、表格和下载型 Artifact；ChatGPT Deep Research 已支持文件、网页、MCP/App、进度跟踪和中途干预；Manus 用 Wide Research 并行化通用 Agent；Genspark 已把 SecondBrain、Super Agent、Slides/Sheets/Docs/Mail/Code 和团队协作组合为完整 AI Workspace；Perplexity 也在把 Research、文件/应用创建和长期 Memory 汇合。

因此，QwenPaw 不能只说“我们也能生成报告和表格”。更窄的可验证差异是：**开源、自托管、本地私有资料、开放文件格式、可替换引擎、可回放执行和中国生态触达**。如果目标用户不重视这些属性，QwenPaw 很难在结果质量和成品体验上正面对抗头部闭源产品。

## 四、QwenPaw 自身：不是能力不足，而是能力没有形成尖锐闭环

### 4.1 已有资产

从 README、依赖、路由和代码结构看，QwenPaw 已具备：

- Scroll + ReMe 的多层上下文与长期记忆；
- 本地文件工作空间、预览、编辑、Diff、上传下载；
- Sandbox、Tool/File Guard、审批与治理；
- Checkpoint、后台任务、Cron、Heartbeat；
- MCP、ACP、Plugin、Skill、Marketplace；
- Console、TUI、Desktop 和多个 IM Channel；
- 多 Agent、Codex/Qoder 等 Harness 接入；
- Hub、多用户与 PawApp 平台；
- 本地模型与 QwenPaw-Flash 小模型。

这些资产足以支撑“AI 工作空间”，也说明没有必要再从零建一套 Agent Framework。

### 4.2 维护面与用户痛点

仓库方向性统计：

- 2026-07-01 以来约 802 个提交中，`fix` 约 429 个，`feat` 约 192 个；
- 后端主要目录合计约 33.6 万行 Python，Console 约 21 万行 TS/TSX；
- 同时已有约 870 个 Python 测试文件和 385 个 Console 测试文件，不能简单归因于“没有测试”；
- 近期 1000 条 Issue 的标题关键词重叠聚类中，稳定性 / 错误 / 丢失 / 卡死 / 恢复相关约 494 条，安装升级约 137 条，Provider 约 119 条，记忆上下文约 109 条，Console/Desktop/UI 约 100 条，插件/MCP/Skill 约 93 条。

Issue 中反复出现的高价值问题包括：停止按钮未真正停止、Tool Result 丢失或重复派发、长会话无响应、压缩后工具调用结构丢失、Cron 结果缺失、内存耗尽、同步调用冻结实例、会话丢失、上下文中无界 base64、子 Agent 完成状态不被主 Agent 感知、密钥掩码与危险指令绕过等。

这说明当前的主要矛盾不是缺少新功能，而是以下五个闭环尚未成为稳定产品承诺：

1. **任务状态闭环**：开始、暂停、停止、失败、恢复、完成的语义一致；
2. **成果闭环**：每个任务有明确产物、证据、版本和签收状态；
3. **资源闭环**：时间、Token、内存、并发和文件体积都有预算；
4. **权限闭环**：谁在什么环境以什么权限执行，用户始终看得懂；
5. **诊断闭环**：出现问题后可以一键导出脱敏任务包并定位失败阶段。

### 4.3 资产成熟度与去留建议

| 能力 | 资产匹配 | 当前风险 | 战略处理 |
|---|---:|---:|---|
| 文件工作空间 / Artifact | 高 | 目前被聊天入口遮蔽 | 提升为主产品对象 |
| Scroll / ReMe | 高 | 长上下文、压缩、召回可靠性仍有痛点 | 作为连续性底座，不单独营销 |
| Sandbox / Governance | 高 | 边界和解释性仍需强化 | 作为可信执行底座 |
| Checkpoint / Session | 高 | 恢复、停止、重复执行问题直接伤信任 | 最高优先级修复 |
| 多 Harness 接入 | 中高 | 易滑向重型控制平面 | 做窄契约，不做全套 Harness |
| Channel | 中 | 适配维护成本高 | 保留高频渠道，其他插件化 |
| Provider | 中 | 长尾兼容成本高 | 收敛认证矩阵，其余社区维护 |
| Plugin / MCP / Skill | 高 | 供应链、安全、质量不一致 | 权限清单、签名、兼容性分级 |
| PawApp | 中高 | 可能继续膨胀核心体积 | 拆成 Outcome Pack，按需安装 |
| Hub / 企业治理 | 中 | 与成熟企业平台正面竞争 | 在小团队需求被验证后再扩张 |
| 自研通用 Agent Loop | 低 | 与 AgentScope/Qwen Code 重复 | 逐步下沉或替换为上游能力 |

## 五、八条候选战略的比较

评分采用 1–5 分，权重为：未满足需求 25%、资产匹配 20%、可防御性 20%、交付速度 15%、商业化 10%、维护友好 10%。这是基于当前证据的决策假设，不是精确财务模型。

| 战略 | 需求 | 匹配 | 防御 | 交付 | 商业 | 维护 | 加权分 | 裁决 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| 通用 Agent OS 继续横向扩张 | 2 | 4 | 1 | 2 | 2 | 1 | 2.10 | 不建议 |
| 极致轻量个人 Agent | 3 | 3 | 1 | 4 | 2 | 4 | 2.75 | 作为版本，不作终局 |
| 最强自研 Coding Harness | 2 | 3 | 2 | 2 | 3 | 2 | 2.30 | 不建议 |
| 多 Harness 团队控制平面 | 3 | 4 | 2 | 3 | 4 | 2 | 3.00 | 只做工作空间级子集 |
| 企业治理 / 私有平台 | 4 | 3 | 3 | 2 | 5 | 1 | 3.10 | 延后 |
| 垂直 Outcome Pack | 4 | 4 | 3 | 4 | 4 | 3 | 3.70 | 第二增长曲线 |
| 私有可验证 Artifact Workspace | 4 | 5 | 3 | 3 | 4 | 3 | **3.75** | 仍是主战略候选，需先证明隐私/开放性楔子 |
| 独立跨 Agent 记忆产品 | 3 | 5 | 2 | 3 | 3 | 3 | 3.20 | 作为底座 |

### 5.1 为什么不是“轻量化优先”

轻量化能改善安装、启动和资源占用，却不能回答“为什么用户要长期用 QwenPaw”。nanobot、Goose、Aider 已证明小而快可以被快速复制。正确做法是：

- 提供 **QwenPaw Lite** 产品配置，而不是把“轻量”当品牌终点；
- 默认只启用工作空间、一个 Agent、核心文件工具、ReMe 和一种交互界面；
- Channel、Creator、Data、Browser、Computer Use、Hub 按需安装和延迟加载；
- 用“首次成果时间”和“空闲/峰值资源”衡量，而非只宣传包大小。

### 5.2 为什么不是“技术领先优先”

底层模型和 Harness 的领先窗口很短。QwenPaw 更可防御的技术不是再写一个 Loop，而是：

- 跨引擎稳定的 Task / Event / Artifact 契约；
- 文件、任务、记忆和证据之间可追溯的 provenance graph；
- 崩溃恢复、幂等工具调用和可验证停止；
- 本地环境、权限、凭据和预算的统一隔离；
- 将一次成功工作固化为可安装 Outcome Pack 的机制。

这些技术直接服务产品承诺，且不容易被一次模型升级抹平。

### 5.3 为什么不是“企业治理优先”

企业治理有付费能力，但销售、认证、部署、审计、SLA 和集成成本都很高。QwenPaw 当前仍有明显的个人端稳定性债务。过早进入企业市场会把团队拖进定制交付。

正确顺序是：个人可靠性 → 小团队共享与审批 → 部门级策略和审计 → 企业部署。每一级必须由上一层的留存和付费证据解锁。

## 六、推荐产品架构

### 6.1 五层边界

```text
┌──────────────────────────────────────────────────────────┐
│ Outcome Packs: Research / Data / Creator / Dev / Mail    │
├──────────────────────────────────────────────────────────┤
│ QwenPaw Experience: Desktop / Web / TUI / Core Channels  │
├──────────────────────────────────────────────────────────┤
│ Workspace Kernel                                         │
│ Project · Task · Artifact · Event · Policy · Environment │
│ Checkpoint · Budget · Provenance · Approval · Diagnosis  │
├──────────────────────────────────────────────────────────┤
│ Engine Adapters: AgentScope / Qwen Code / Codex / Qoder  │
│ ACP-compatible and future third-party harnesses           │
├──────────────────────────────────────────────────────────┤
│ Substrates: ReMe · Sandbox · local/cloud models · MCP     │
└──────────────────────────────────────────────────────────┘
```

### 6.2 与 AgentScope、ReMe、Qwen Code 的分工

| 项目 | 应负责 | 不应负责 |
|---|---|---|
| AgentScope | 通用 Agent Runtime、Team、Service、消息和执行原语 | QwenPaw 的产品 UI 与垂直工作流 |
| ReMe | 记忆抽取、检索、来源、文件化和评测 | 项目任务状态、审批和成果生命周期 |
| Qwen Code | 编码 Agent Loop、代码理解、Shell、IDE/CLI 编码体验 | 通用个人工作空间和所有业务场景 |
| QwenPaw | 工作空间、任务、成果、权限、环境、连续性、交互与应用包 | 再造通用 Framework 或追逐每个模型特性 |

判定规则：一项能力若能被多个产品无差别复用，应优先下沉 AgentScope/ReMe；若只为了让用户完成、检查和复用具体工作，应留在 QwenPaw。

### 6.3 核心数据模型

QwenPaw 需要把 `chat/session` 从顶层对象降为任务的一种交互视图，核心对象改为：

- **Workspace**：文件、环境、成员、策略、共享记忆的边界；
- **Task**：目标、计划、状态机、预算、所用引擎和验收条件；
- **Run**：一次具体执行，拥有事件日志、资源消耗和恢复点；
- **Artifact**：文件、报告、数据、代码变更、截图等可交付物；
- **Evidence**：来源、命令输出、测试结果、引用和人工确认；
- **Policy**：文件、网络、工具、凭据和审批规则；
- **Recipe**：一次已验证任务固化后的可复用执行模板。

成功状态不能再等同于“Agent 返回 final message”，而应是：验收条件满足、Artifact 存在、Evidence 完整、预算未越界、用户或自动规则签收。

### 6.4 引擎契约只做必要子集

不要企图抹平所有 Harness 差异。v0 契约只定义：

1. start / pause / cancel / resume；
2. 输入文件和工作目录；
3. 流式事件与结构化工具调用；
4. 权限申请和审批回执；
5. checkpoint / continuation token；
6. usage / cost / duration；
7. artifact 与 evidence 回传；
8. capability discovery。

不支持某能力时必须显式降级，不通过猜测或 Prompt 模拟一致性。

## 七、首个产品楔子

### 7.1 推荐：本地资料到可验证成果

典型任务：

> 用户选择一个包含 PDF、文档、表格、代码或网页摘录的本地工作空间，给出目标；QwenPaw 规划并执行数十分钟到数小时，期间可暂停、换引擎和人工审批；最终交付报告、表格、演示文稿、代码补丁或资料包，并保留来源、过程、版本和后续待办。

为什么该场景适合 QwenPaw：

- 文件工作空间、ReMe、Creator、Data、Browser、Checkpoints 已有资产可组合；
- 本地隐私是真需求，且比纯编码覆盖更广；
- 成果与证据可以客观验收，便于建立质量指标；
- 可以自然扩展到研究、运营、产品、数据和开发多个 Outcome Pack；
- Channel 成为发起、通知和审批入口，而不是产品本体。

### 7.2 建议的两个首发 Outcome Pack

1. **Research Pack**：资料摄取、来源卡片、冲突识别、引用、研究报告、更新监测；
2. **Data Pack**：CSV/XLSX/数据库读取、清洗、分析、图表、结论、可复现步骤。

Creator 和 Dev 可作为随后验证的 Pack。Mail 更适合作为输入/输出 Channel 或业务连接器，除非有明确高频的邮件工作闭环证据。

## 八、路线图

### 0–3 个月：止血、收敛、建立可测基线

- 冻结非关键 Channel、Provider 和底层 Loop 的横向扩张；
- 定义 Task / Run / Artifact / Evidence 状态模型和引擎契约 v0；
- 优先修复 cancel、幂等工具调用、崩溃恢复、长会话资源界限、任务结果丢失；
- 为错误建立统一分类：用户输入、模型、工具、策略、环境、资源、产品 Bug；
- 建立脱敏诊断包与任务回放；
- 测量安装时间、首次成果时间、空闲/峰值资源、任务恢复率和重复执行率；
- 完成 20–30 名目标用户访谈和 10 个真实任务观察；
- 设计 Lite / Workstation / Hub 三种产品配置，但只实现 Lite 的最小拆包。

**阶段门：** 如果稳定性问题不能显著下降，停止新增 Outcome Pack，继续偿还核心可靠性债务。

### 3–6 个月：工作空间产品 MVP

- 首页从 `/chat` 转为 Workspace / Task / Artifact；
- Chat 保留为任务内交互视图；
- 提供任务时间线、当前状态、预算、权限、产物和证据面板；
- 接入至少三个差异明显的引擎，并完成显式能力发现与降级；
- 发布 Research Pack 和 Data Pack 内测版；
- 将非核心 Channel、Provider、PawApp 改为按需安装 / 延迟加载；
- 提供“把一次成功任务保存为 Recipe”。

**阶段门：** 目标用户必须在不读长文档的情况下，于 10 分钟内完成首个可验证成果；工作空间版相对 Chat 基线显著减少返工。

### 6–12 个月：复用与小团队协作

- Workspace 分享、角色、审批和 Artifact Review；
- Recipe / Outcome Pack 的版本、依赖、权限清单和兼容性；
- 团队预算、引擎策略、凭据隔离和审计事件；
- 可选 Hub 同步，但保持 local-first 和离线可用；
- 建立高质量 Pack 市场，不追求数量，展示成功率、成本和权限；
- 与 AgentScope / ReMe 明确公共能力的上游贡献边界。

**阶段门：** 至少 10 个设计伙伴持续 8 周使用，且其中 3 个愿意为团队能力付费或签署付费意向。

### 12–24 个月：从工作台扩展到可信工作网络

- 部门级策略、集中审计、可选远程执行节点和私有部署；
- 跨设备 / 跨环境的 Workspace Continuation；
- Artifact 与企业内容系统、代码仓、数据仓的受控同步；
- 引擎与 Pack 认证体系；
- 只在付费证据成立后投入 SSO、SLA、合规认证和大规模多租户。

**阶段门：** 企业能力的 ARR / 续费潜力必须覆盖其专属维护与交付成本，否则保持社区版 + 小团队版，不进入重交付模式。

## 九、指标体系

### 9.1 北极星指标

**Weekly Verified Artifacts per Active Workspace（每个活跃工作空间每周已验证成果数）**

“已验证”要求 Artifact 满足预设验收条件并被用户接受或被自动验证通过。该指标比消息数、Token 数、工具调用数更接近真实价值。

### 9.2 核心护栏

| 维度 | 指标 |
|---|---|
| 激活 | 安装到首个已验证成果的时间；无需文档帮助的成功率 |
| 可靠 | 任务成功率、恢复成功率、重复副作用率、取消生效时延、崩溃自由会话率 |
| 质量 | Artifact 首次验收率、平均返工轮次、引用/测试证据完整率 |
| 连续 | 24 小时后任务恢复率、跨引擎继续成功率、上下文压缩后事实保持率 |
| 效率 | 每个已验证成果的时间、Token、费用、内存峰值 |
| 留存 | W4 / W8 活跃 Workspace 留存、Recipe 复用率、Pack 重复使用率 |
| 安全 | 越权拦截率、误拦截率、敏感数据外发事件、危险操作人工确认覆盖率 |
| 维护 | 核心外新增适配器缺陷率、版本升级失败率、长尾集成维护工时 |

不要用“支持 N 个模型 / N 个渠道 / N 个 Skill”作为主 KPI，这会诱导继续扩张维护面。

## 十、可证伪实验

| 假设 | 3–8 周实验 | 成功条件 | 失败 / 退出条件 |
|---|---|---|---|
| Workspace 比 Chat 更有价值 | 20 个真实任务做交叉对照 | 完成率提升 ≥20%，返工轮次下降 ≥25% | 无显著改善，重新评估信息架构 |
| 用户愿为连续任务而迁移 | 30 名目标用户连续使用 4 周 | ≥40% 每周产出 1 个已验证成果 | W4 留存 <20%，缩小或更换 ICP |
| 多引擎是体验优势 | 同一任务在 3 个引擎间启动/恢复/切换 | ≥80% 任务可继续，且无数据丢失 | 适配成本持续高于用户使用价值，收敛到 1–2 个引擎 |
| Research Pack 是有效楔子 | 10 个设计伙伴完成真实研究交付 | ≥6 个周复用，≥3 个愿付费 | 只有 Demo 兴趣，没有重复使用，停止独立 Pack |
| Data Pack 是有效楔子 | 10 个真实数据任务与现有方法对照 | 总耗时或返工下降 ≥30% | 结果不可复现或人工清洗成本不降，调整范围 |
| Lite 能显著改善采用 | 新旧安装和首次成果 A/B | 首次成果时间下降 ≥40%，失败率减半 | 拆包增加兼容债务且改善 <15%，停止继续拆分 |
| 小团队治理可商业化 | 5 家设计伙伴、2 个受控试点 | 2 家完成 8 周试点并给出预算 | 需求全部是一次性定制，延后企业化 |
| 可靠性是留存杠杆 | 针对 Top 5 失败类型专项修复 | 相关失败率下降 ≥70%，W4 留存同步上升 | 留存不变，说明核心价值而非可靠性才是主问题 |

所有阈值在实验前冻结，不能在结果不理想后修改口径。实验数据应按任务类型、引擎和用户层级切片，避免平均数掩盖问题。

## 十一、不做清单

未来两个季度建议明确不做：

1. 不再用“支持更多模型 / 渠道 / MCP”作为版本主叙事；
2. 不自研另一个通用 Coding Agent Loop；
3. 不把多 Agent 数量和并发数当产品价值；
4. 不把跨 Agent 记忆拆成独立主产品；
5. 不与 Dify/n8n 比拼可视化节点和连接器数量；
6. 不同时重做 Desktop、Web、TUI 和所有 Channel 的完整体验；
7. 不在个人端可靠性未过关时承诺企业 SLA；
8. 不默认捆绑所有 PawApp 和重型依赖；
9. 不用 Star、下载量和 Demo 成功率代替留存与交付指标；
10. 不继续使用无法解释用户结果的宽泛“Agent OS”定位。

## 十二、风险与蓝军反驳

### 反驳 1：“工作空间”也会被 Codex、Cursor、OpenHands 做掉

成立一半。纯编码工作空间会被它们做强，因此 QwenPaw 不能只做代码。差异在本地多类型文件、跨任务长期连续性、可插拔 Harness、中国用户常用渠道与本地模型，以及 Research/Data/Creator 等成果包。若这些差异不能在留存中体现，应及时收缩，不继续讲平台故事。

### 反驳 2：Artifact 只是换了一个 UI 名词

如果只是把聊天附件改名，确实没有价值。Artifact 必须拥有 Schema、版本、来源、验收条件、Evidence 和下游动作，且任务成功由 Artifact 状态决定，才构成产品范式变化。

### 反驳 3：收敛会伤害开源社区增长

短期可能降低“新功能新闻密度”，但更稳定的扩展契约和更小内核能降低贡献门槛。社区扩展应从改核心切换为开发 Pack / Adapter，并以兼容测试、权限清单和质量分级保护用户。

### 反驳 4：QwenPaw 用户就是想要一个通用助手

需要用行为数据验证。通用入口可以保留，但通用助手通常高频低粘性、替换成本低。若用户最终没有创建和复用 Workspace/Artifact，则说明推荐战略不成立，应转向更窄的垂直产品，而不是退回无限扩张。

### 反驳 5：现在应该先追模型和 Benchmark

模型 Benchmark 可以选择引擎，却不能衡量任务是否恢复、停止、审计和交付。QwenPaw 应建立产品级任务集：中断恢复、跨引擎继续、长上下文压缩、权限拒绝、资源超限、人工接管和 Artifact 验收。

## 十三、最终选择

QwenPaw 的主战略候选应是 **面向本地私有资料与混合知识工作的、开放且可验证的 Artifact Workspace**，而不是另一个 AI 编程工作台，也不是 NotebookLM/Genspark 的功能复制品。可信执行与任务连续性是底座，Outcome Pack 是验证场景的方法，轻量化是产品配置，多 Harness 是引擎能力，企业治理是后续商业化层。

优先级顺序：

1. **可靠状态与恢复**：先让任务可相信；
2. **Workspace / Artifact 产品重构**：再让价值可感知；
3. **Research / Data Outcome Pack**：验证垂直交付与付费；
4. **Lite 与按需安装**：扩大采用并控制维护面；
5. **小团队协作与治理**：由真实付费证据解锁；
6. **企业控制平面**：最后进入，而不是现在抢跑。

如果只能做一件事，不应再新增一个 Agent 能力，而应把一个真实的本地资料任务，从创建、执行、中断、恢复、审批到成果验收，做成全行业最清楚、最可靠的一条链路。

## 主要公开资料

- QwenPaw README 与仓库代码（本地当前版本）
- [Stack Overflow 2025 Developer Survey — AI](https://survey.stackoverflow.co/2025/ai)
- [DORA 2025 State of AI-assisted Software Development](https://dora.dev/research/2025/dora-report/)
- [METR: Measuring the Impact of Early-2025 AI on Experienced Open-Source Developer Productivity](https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/)
- [JetBrains: Which AI Coding Tools Do Developers Actually Use at Work?](https://blog.jetbrains.com/research/2026/04/which-ai-coding-tools-do-developers-actually-use-at-work/)
- [JetBrains Central](https://blog.jetbrains.com/blog/2026/03/24/introducing-jetbrains-central-an-open-system-for-agentic-software-development/)
- [OpenAI Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [OpenAI Plugin Architecture](https://developers.openai.com/plugins/concepts/plugins)
- [OpenAI Open Source](https://learn.chatgpt.com/docs/open-source)
- [OpenAI: Introducing deep research](https://openai.com/index/introducing-deep-research/)
- [OpenAI: Introducing Prism](https://openai.com/index/introducing-prism/)
- [Google: Do your best research with NotebookLM](https://blog.google/innovation-and-ai/products/notebooklm/better-research-notebooklm/)
- [Google Cloud: Gemini Notebook Enterprise overview](https://docs.cloud.google.com/gemini/enterprise/notebooklm-enterprise/docs/overview)
- [Manus: Introducing Wide Research](https://manus.im/blog/introducing-wide-research)
- [Genspark AI Workspace 6.0](https://www.genspark.ai/blog/genspark-ai-workspace-6)
- [Perplexity Max](https://www.perplexity.ai/help-center/en/articles/11680686-perplexity-max)
- [OpenHands](https://github.com/All-Hands-AI/OpenHands)
- [Paperclip](https://github.com/paperclipai/paperclip)
- [OpenClaw](https://github.com/openclaw/openclaw)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [nanobot](https://github.com/HKUDS/nanobot)
- [AgentScope](https://github.com/agentscope-ai/agentscope)
- [ReMe](https://github.com/agentscope-ai/ReMe)
- [Qwen Code](https://github.com/QwenLM/qwen-code)
- [Mem0](https://github.com/mem0ai/mem0)
- [Letta](https://github.com/letta-ai/letta)
- [Supermemory](https://github.com/supermemoryai/supermemory)
- [Dify](https://github.com/langgenius/dify)
- [n8n](https://github.com/n8n-io/n8n)
- [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)

## 附录：研究边界

- 竞品功能会快速变化，本报告是 2026-09-21 的截面；
- GitHub Star 只表示公开关注度，未用于战略评分；
- Issue 聚类基于近期 1000 条 Issue 的标题关键词，类别可重叠，不能当作精确缺陷率；
- 代码行数包含不同成熟度、生成代码和前端资源，只用于识别维护面；
- 商业产品的收入、留存和活跃用户缺少统一公开口径，因此没有伪造 TAM 或份额数字；
- 路线图必须通过可证伪实验动态调整，不能把本报告当成不可修改的长期承诺。
