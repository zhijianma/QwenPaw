# QwenPaw 竞品技术、能力与社区画像证据册

> 数据截面：2026-09-21。
> 本文是《QwenPaw 2026–2028 深度战略研究》的证据附录。
> GitHub 指标会持续变化；Issue / PR / Fork 用于推断社区结构和使用问题，不代表活跃用户、收入或产品质量。

## 一、研究问题与判读规则

对每个代表产品至少回答五个问题：

1. **技术核心是什么**：Runtime、IDE 插件、工作台、Gateway、状态机、记忆层还是工作流平台；
2. **一等状态对象是什么**：Chat、Session、Task、Graph、Workspace、Artifact、Memory 还是 Organization；
3. **交互围绕什么展开**：终端对话、IDE 文件、工作空间任务、IM 渠道、API 或可视化流程；
4. **谁最可能使用**：个人开发者、Agent 高级用户、框架开发者、自托管管理员、知识工作者或企业平台团队；
5. **GitHub 行为说明什么**：Issue 主题揭示真实阻塞，PR 结构揭示扩展方式，Fork 揭示自部署、二开或关注意愿。

判读限制：

- Fork 可能只是收藏、镜像或短期实验，不能直接等同二次开发；
- PR 数可能被 Bot、依赖升级、自动生成和低质量提交放大；
- Issue 数受迁移、关闭策略和外部工单系统影响；
- ZCode 在数据截面前一天才开放当前仓库，0 Issue / 0 PR 是观察窗口缺失，不是产品没有问题；
- Letta、Zep 等项目的公开 Issue 为 0，可能使用其他追踪系统，不能据此判断稳定性；
- OpenClaw、Hermes、Paperclip 等爆发式仓库的 PR 量存在明显自动化或信号污染，必须结合主题判断。

## 二、技术与目标用户矩阵

### 2.1 编码 Harness、IDE Agent 与 AI 编程工作台

| 产品 | 技术与一等对象 | 核心能力 | 主要交互 | 目标用户 / GitHub 画像 |
|---|---|---|---|---|
| Codex | Rust CLI + App Server + SDK；Thread | 沙箱、审批、线程恢复、SDK 嵌入、云任务 | Terminal / IDE / App / API | 专业开发者和工具集成者；大量 SDK、平台、认证和执行环境问题 |
| Claude Code | Terminal Harness；Project / Session | CLAUDE.md、Rules、Skills、Subagents、Hooks | 终端长对话与代码任务 | 愿意配置工作流的高级开发者、团队规范维护者 |
| Gemini CLI | 开源 CLI；Session / Checkpoint | MCP、Extension、Sandbox、GEMINI.md、恢复 | 终端对话 | Google 生态与多模型开发者；PR 参与广，扩展和平台兼容需求强 |
| OpenCode | TUI/Desktop/Server；Session | Plan/Build Agent、多 Provider、Plugin | 终端和桌面对话 | 模型自由度敏感的高级用户；高热 Issue 集中认证、Provider、Windows、Memory 与 TUI |
| Aider | Python CLI；Git changes | Git 原生编辑、Repo Map、多模型 | 终端结对编程 | 习惯 Git/CLI 的个人开发者；Issue 长期关注新模型、本地模型、Provider 和确认机制 |
| Cline | IDE/Desktop/CLI/SDK；Task | Plan/Act、Checkpoint、工具审批、MCP、团队 | IDE 对话和任务 | VS Code/JetBrains 开发者；终端捕获、IDE 健康检查、Provider 和跨平台最常阻塞 |
| Roo Code | VS Code Agent；Mode / Task | 多 Mode、并行 Agent、Provider、终端 | IDE 对话 | 追求高度配置与多 Agent 的开发者；冻结、循环读文件、Provider 兼容问题突出 |
| Continue | VS Code/JetBrains 插件；IDE Context | Autocomplete、Chat、Agent、Indexing、团队配置 | IDE 内对话与补全 | 企业 IDE 用户和平台团队；Issue 高度集中扩展崩溃、索引与 JetBrains/VS Code 兼容 |
| Goose | Rust Desktop/CLI/API；Session | 多 Provider、ACP、MCP Extension、本地执行 | Desktop / Terminal | 本地优先和开放模型用户；安装、Linux 打包、Keyring、Context Compact、MCP 稳定性突出 |
| Qwen Code | CLI/Web/Desktop/IDE；Session / Workflow | Auto-Memory、Skills、Subagents、Teams、Daemon、Channels | 终端为主，多端扩展 | Qwen 模型与国内生态开发者；计费/OAuth、Session、多 Workspace、TUI 是高互动主题 |
| Kimi CLI | Python CLI；Session | Shell、ACP、MCP、Kimi Coding Plan | 终端对话 | Kimi 订阅与中文开发者；登录、鉴权、UTF-8、API 错误、跨会话记忆需求明显 |
| ZCode | Electron + Web + Agent CLI/Runtime；Workspace / Task / Goal | Goal Mode、1M Context、Git 分支、SSH/Docker、Browser、Terminal、Review、Subagent | 工作空间任务，而非单纯聊天框 | GLM Coding Plan、前后端和远程开发用户；818 Fork 但当前开源仓库几乎无 Issue/PR 历史，画像需结合产品社区而非仓库工单 |
| Pi | 极简 Runtime / Extension；Session | Provider/Tool/Extension、状态管理、Project Trust | 终端对话 | 喜欢可黑客化内核的高级用户；连接可靠性、Windows、登录、本地模型和项目可信是高互动主题 |
| DSH | 插件化 Harness / Cordis；Plugin | 一切插件化、可组合 Agent Runtime | 终端 / 开发接口 | Harness 开发者与实验用户；处于开发者预览，产品成熟度证据有限 |

**关键分界：** Harness 的一等对象通常是 Session 或代码修改；ZCode 已把 Workspace / Task / Goal 做成一等对象。QwenPaw 若只把 Chat 换成 Workspace，无法形成差异，必须把非代码文件、跨领域 Artifact、长期个人资料和多渠道连续性做成核心。

### 2.2 自治软件工程与多 Agent 控制

| 产品 | 技术与一等对象 | 核心能力 | 主要交互 | 目标用户 / GitHub 画像 |
|---|---|---|---|---|
| OpenHands | Agent Server + Canvas；Conversation / Agent / Workspace | Local/Docker/VM/Cloud backend、Automation、多 Agent | Web 控制中心 | 自托管开发团队、Agent 平台开发者；高 PR 合入量，部署、执行后端与 UI 都有持续贡献 |
| SWE-agent | Python Agent + SWE-bench Environment；Issue / Patch | Repo Issue 到 Patch、评测环境 | CLI / Benchmark | Agent 研究者和 Benchmark 使用者；安装、Docker、环境复现、Patch 落盘是主要问题 |
| Paperclip | Organization Control Plane；Company / Agent / Task / Budget | Org chart、预算、审批、审计、恢复、Adapter、Plugin | Web 组织控制台 | 多 Agent 创业者与团队运营者；本地模型、工作空间配置、Adapter、Serverless/Auth 是实际需求 |
| AutoGPT | Agent Platform；Agent / Block / Workflow | Agent Builder、Marketplace、Blocks | Web / API | 低代码 Agent 构建者；早期社区聚焦自治、模型与 Memory，后期平台化 |
| Multica | 多 Agent Workspace / Orchestration；Task | 并发 Agent、任务分派与结果聚合 | Web 工作台 | 同时运行多个 Coding Agent 的高级用户；与 QwenPaw“控制平面”假设直接重叠 |
| JetBrains Central | 企业控制与执行平面；Task / Agent / Cost | 多 Agent 接入、团队可见性、成本和治理 | Web + IDE 生态 | 已使用多个 Coding Agent 的研发组织和平台团队 |
| Factory | Software Factory；Mission | SDLC Mission、企业部署、K8s/VM/Airgapped | Web / CLI / 企业系统 | 有预算、合规和交付流程的大型研发组织 |

### 2.3 个人 Agent OS、渠道与本地助手

| 产品 | 技术与一等对象 | 核心能力 | 主要交互 | 目标用户 / GitHub 画像 |
|---|---|---|---|---|
| OpenClaw | Local Gateway；Session / Channel / Tool | 20+ Channels、Skills、Plugins、Host Tools、ClawHub | IM 渠道 + Web | 自托管个人用户、渠道插件作者；消息丢失、Channel、身份信任、桌面平台是高互动主题 |
| Hermes Agent | Python Agent + Gateway；Session / Skill / Memory | 自动 Skill、Memory、Cron、Subagents、多终端后端 | Terminal + Channel | 高自主性 Agent 用户与插件作者；安装、Provider、流式响应、Fleet/Gateway 恢复问题多 |
| nanobot | 小型 Python Core；Session / Tool | Memory、MCP、Routing、Delegation、Cron、Channels、WebUI | Channel + Web + CLI | 想快速自托管的个人与中文用户；飞书/WhatsApp、Provider、Prompt Cache 和消息队列最受关注 |
| Open Interpreter | 本地 Computer Agent；Conversation | Shell/Computer control、本地模型 | Terminal | 想用自然语言控制电脑的个人用户；GPU、本地模型、安装和“无响应”问题突出 |
| AnythingLLM | Desktop/Web local AI；Workspace / Thread | RAG、Agent、Flow、MCP、多用户、Scheduled Tasks | Chat Workspace | 隐私敏感的知识库用户和小团队管理员；部署、模型、文档摄取与集成是主需求 |
| browser-use | Browser Runtime；Browser Session / Step | Playwright/CDP、浏览器 Agent、云浏览器 | Python/API | 构建浏览器 Agent 的开发者；浏览器版本、CDP、空白页面和解析失败是主要阻塞 |

### 2.4 记忆与状态基础设施

| 产品 | 技术与一等对象 | 核心能力 | 主要交互 | 目标用户 / GitHub 画像 |
|---|---|---|---|---|
| ReMe | File-native Memory Engine；Memory / Source | Markdown、来源保留、检索、更新、跨 Agent 工作区 | SDK / 文件 / 插件 | QwenPaw/Hermes/Codex 等集成开发者；评测复现、Embedding、向量库和合并逻辑是核心问题 |
| Mem0 | Memory API / Platform；User / Agent / Session Memory | 抽取、更新、过滤、图记忆、云/自托管 | SDK / API | Agent 应用开发者；静默丢记忆、冲突、时间语义、元数据过滤和评测可信度最受关注 |
| Letta / Letta Code | Stateful Agent Server；Agent Identity / Memory | 自修改 Memory、MemFS、Git 历史、持久 Agent | CLI / API / Channel | 想维护长期 Agent 身份的开发者；公开仓库 Issue 已迁移，需看文档与社区而非 0 Issue |
| Supermemory | Cross-agent Memory Service；Memory / Profile | 多工具同步、用户画像、矛盾/过期、Memory Graph | API / Plugin / Consumer App | 同时使用多个 AI 工具的个人与应用开发者；本地运行、多模态、PDF、SDK 兼容和移动端需求明显 |
| Zep | Memory / Knowledge Graph Platform；Graph / Session | Temporal KG、长期记忆、Agent Context | SDK / API | 构建生产 Agent 的后端团队；公开 Issue 为 0，无法用 GitHub Issue 推断稳定性 |

### 2.5 Agent Framework 与企业工作流

| 产品 | 技术与一等对象 | 核心能力 | 主要交互 | 目标用户 / GitHub 画像 |
|---|---|---|---|---|
| AgentScope 2.0 | Python Agent Framework / Runtime；Agent / Team / Service | Loop、HITL、Memory、Sandbox、Multi-tenant Service、Channels | SDK / Console | Agent 应用工程师；结构化输出、模型适配、中断恢复、Memory 与 MCP 是主要问题 |
| AutoGen | Multi-agent Framework；Agent / Conversation | Agent chat、distributed runtime、extensions | SDK | 研究与企业 Agent 开发者；治理、Guardrail、Goal integrity 和分布式协议讨论增多 |
| LangGraph | Stateful Graph Runtime；Graph / State / Checkpoint | Durable execution、HITL、Persistence、Cloud | SDK / API | 生产级 Agent 后端工程师；长工具重试、Checkpoint、取消与审批是高价值问题 |
| CrewAI | Crew / Flow Runtime；Crew / Task / Flow | Role、Task、Guardrail、Enterprise control plane | SDK / Web | 快速构建业务 Agent 团队的开发者；幂等、权限、治理与工具真实性是高互动问题 |
| Dify | LLM App Platform；App / Workflow / Dataset | Workflow、RAG、Agent、Model、Observability | 可视化工作流 + API | 企业 AI 应用团队和自托管管理员；升级、Docker、模型密钥、RAG 与 Agent 节点问题突出 |
| n8n | Workflow Automation；Workflow / Node / Credential | 1500+ integrations、Code、Approval、RBAC | 可视化流程 | 自动化工程师和业务技术人员；升级、连接、文件内存、MCP 节点兼容是主要痛点 |
| Flowise | Visual Agent Flow；Flow / Node | LLM/RAG/Agent visual builder | 可视化流程 | 低代码 Agent 构建者；Agent 未调用、向量清理、GraphRAG 和路径部署是主需求 |
| MaxKB | Knowledge App Platform；Knowledge Base / App | RAG、Workflow、Agent、Model | Web Console | 中文企业知识库管理员；PR 高合入、Issue 几乎清空，体现集中维护而非无问题 |

## 三、GitHub 社区结构快照

下表使用 `Issues` 与 `PRs` 的累计总量，不把 PR 混入 Issue。`Open` 是当前未关闭数量。Stars 省略，因为本研究不使用 Star 评分；Fork 保留用于观察二开 / 自部署倾向。

| 项目 | Fork | Issues | Open issues | PRs | Merged PRs | Open PRs | 社区信号 |
|---|---:|---:|---:|---:|---:|---:|---|
| QwenPaw | 3,119 | 3,877 | 657 | 3,821 | 2,495 | 341 | 产品用户与核心开发并存；可靠性、安装、Provider、记忆、Console 均有压力 |
| Codex | 19,514 | 29,093 | 17,875 | 16,553 | 11,202 | 169 | 用户量大且反馈入口拥堵；Fork 与集成开发者多 |
| Gemini CLI | 14,608 | 14,423 | 575 | 12,878 | 6,707 | 263 | Issue 关闭率高，外部协作强 |
| OpenCode | 27,512 | 27,196 | 4,451 | 22,385 | 9,135 | 1,511 | 爆发式高级用户与 Provider/平台长尾需求 |
| Aider | 4,984 | 4,441 | 1,379 | 1,219 | 288 | 504 | 用户反馈强于代码合入，核心维护较集中 |
| Cline | 7,465 | 4,643 | 809 | 7,852 | 4,788 | 587 | IDE 产品与生态贡献并重 |
| Roo Code | 3,422 | 3,562 | 550 | 7,696 | 4,326 | 483 | 高配置 IDE 用户与活跃扩展开发 |
| Continue | 5,409 | 6,705 | 450 | 5,944 | 4,078 | 517 | IDE/企业用户问题明确，贡献面成熟 |
| Goose | 6,284 | 3,159 | 266 | 8,728 | 6,209 | 136 | PR 合入活跃，基金会型协作特征较强 |
| Qwen Code | 3,078 | 5,014 | 1,185 | 7,041 | 5,474 | 325 | 产品快速扩张，国内模型与多端用户混合 |
| Kimi CLI | 1,327 | 1,057 | 490 | 1,335 | 741 | 304 | 产品转型期；鉴权与 API 用户问题占比高 |
| ZCode | 818 | 0 | 0 | 0 | 0 | 0 | 当前仓库仅开放约 1 天；数据窗口不足，不能横比 |
| Pi | 13,627 | 6,263 | 147 | 3,206 | 993 | 65 | Fork 极高、Issue 关闭快；可黑客化用户多 |
| OpenHands | 11,666 | 4,836 | 443 | 11,768 | 7,724 | 426 | 自托管平台与研究/工程贡献者并重 |
| SWE-agent | 2,230 | 646 | 45 | 890 | 640 | 71 | 研究、Benchmark、环境复现型社区 |
| OpenClaw | 82,052 | 54,997 | 5,245 | 96,912 | 43,341 | 2,952 | 超大自托管与插件生态；自动化/噪声也高 |
| Hermes | 52,067 | 29,498 | 13,714 | 87,806 | 15,376 | 29,281 | 爆发式生态但待处理和自动化 PR 极多，信噪比较低 |
| nanobot | 8,556 | 1,502 | 214 | 3,993 | 1,741 | 573 | 轻量自托管与国内渠道用户明显 |
| Paperclip | 14,915 | 3,221 | 2,319 | 10,272 | 2,538 | 3,247 | 新兴项目需求爆发，未处理 Issue/PR 高，不宜只看热度 |
| AgentScope | 3,532 | 1,087 | 274 | 1,510 | 1,003 | 138 | 框架开发者社区，API/模型/HITL 问题主导 |
| AutoGPT | 46,007 | 3,995 | 314 | 9,193 | 5,130 | 267 | 历史品牌带来大量 Fork，产品已多次转型 |
| AutoGen | 9,240 | 3,076 | 554 | 3,852 | 2,537 | 535 | 框架与治理讨论活跃 |
| LangGraph | 7,097 | 1,627 | 561 | 5,839 | 3,950 | 248 | 生产状态机、Checkpoint 与 Cloud 用户为主 |
| CrewAI | 8,526 | 2,302 | 171 | 4,888 | 2,261 | 262 | 业务 Agent 开发者多，治理需求上升 |
| Dify | 24,702 | 19,062 | 402 | 18,389 | 14,242 | 681 | 大型自托管/企业应用社区，升级与集成压力显著 |
| n8n | 60,814 | 10,373 | 409 | 28,439 | 21,769 | 774 | 成熟自动化生态，集成开发者和运维管理员并重 |
| Flowise | 25,034 | 2,670 | 699 | 2,772 | 1,821 | 341 | 高 Fork 的可视化自部署社区；仓库已归档需注意迁移 |
| MaxKB | 3,157 | 2,334 | 21 | 3,843 | 3,512 | 1 | 集中治理、高合入率的中文企业知识库社区 |
| AnythingLLM | 7,376 | 3,860 | 302 | 2,313 | 1,657 | 29 | 桌面/自托管最终用户多，代码合入较集中 |
| Open Interpreter | 5,884 | 1,109 | 1 | 741 | 455 | 1 | Issue 大量关闭；不能仅凭 Open=1 判断稳定 |
| browser-use | 12,718 | 1,671 | 143 | 3,755 | 1,963 | 319 | Browser Agent 基础设施开发者活跃 |
| Mem0 | 7,721 | 2,195 | 323 | 4,879 | 2,557 | 438 | 记忆 API 集成者与评测/数据质量用户混合 |
| Zep | 653 | 0 | 0 | 494 | 349 | 31 | Issue 使用外部渠道或被禁用，公开 PR 仍有贡献 |
| Supermemory | 2,677 | 384 | 30 | 1,306 | 842 | 84 | 新兴跨工具记忆与消费产品社区 |
| ReMe | 302 | 131 | 20 | 425 | 334 | 10 | 规模较小但 PR 合入高，主要是框架集成与研究复现 |

### 3.1 近 90 天新增 Issue / PR：区分历史热度与当前负载

累计量会偏向老项目。以下为 2026-06-23 至 2026-09-21 的创建量；它衡量的是公开仓库流量，不是已合入贡献，也会受到 Bot 和自动化 PR 影响。

| 项目 | 新 Issue | 新 PR | 初步解释 |
|---|---:|---:|---|
| QwenPaw | 1,025 | 1,439 | 产品与工程都在高速变化，维护负载已经较高 |
| Codex | 13,209 | 1,167 | 用户反馈远高于外部 PR，典型大规模产品支持入口 |
| Gemini CLI | 451 | 833 | PR 多于 Issue，协作型开源项目特征明显 |
| OpenCode | 8,231 | 8,458 | 用户和贡献同时爆发，生态高速扩张 |
| Cline | 639 | 1,773 | IDE 产品外部贡献活跃 |
| Qwen Code | 2,358 | 4,258 | 产品面快速扩张，PR 活动很高 |
| ZCode | 0 | 0 | 当前开源仓库只覆盖约一天，不能比较 |
| OpenHands | 651 | 1,326 | 平台工程贡献持续高于工单新增 |
| OpenClaw | 14,619 | 43,767 | 极高流量，混合真实生态、自动化和噪声 |
| Hermes | 17,089 | 49,377 | 同样存在严重信号膨胀，人工评估不能依赖数量 |
| nanobot | 237 | 1,034 | 轻量项目但代码贡献活跃 |
| Paperclip | 876 | 4,301 | 新项目快速扩张，积压风险高 |
| AgentScope | 261 | 460 | 稳定的框架开发节奏 |
| Dify | 1,031 | 3,532 | 成熟平台仍有高集成和交付活动 |
| n8n | 917 | 5,295 | 成熟节点生态带来大量持续 PR |
| Mem0 | 456 | 1,122 | 记忆集成和 SDK 更新活跃 |
| Supermemory | 105 | 436 | 新兴产品处于扩展期 |
| ReMe | 38 | 234 | 小规模上游，但代码更新明显高于用户工单 |

对 QwenPaw 的直接含义：其 90 天公开 Issue 流量已经超过 AgentScope、OpenHands、Cline、Dify 和 n8n 中的多数单项目水平。继续增加长尾功能会把团队进一步推向支持与兼容性工作；路线图必须把“减少故障面和适配面”作为产出，而不只是新增能力。

## 四、从社区数据得到的用户分层

### 4.1 五类用户，不应共用一个首页和一个路线图

| 用户层 | 典型产品 | 最关心 | 最常见失败 | 对 QwenPaw 的启示 |
|---|---|---|---|---|
| CLI/Harness 高级开发者 | Codex、OpenCode、Aider、Pi | 模型自由、速度、Token、Git、Shell | Provider/认证、终端、跨平台、Context | 用 Adapter 服务，不争夺唯一入口 |
| AI 编程工作台用户 | ZCode、Cline、OpenHands、Cursor | Workspace、任务、分支、远程环境、审阅 | IDE/环境、任务恢复、浏览器/终端联动 | 已是红海，不能把 Workspace 当独家概念 |
| 个人常驻 Agent 用户 | OpenClaw、Hermes、nanobot | 渠道、记忆、Cron、持续在线 | 消息丢失、Gateway、安装、Provider | QwenPaw 有基础，但需收敛渠道与可靠性 |
| Agent 应用工程师 | AgentScope、LangGraph、CrewAI、Mem0 | API、状态、Checkpoint、HITL、部署 | 幂等、恢复、数据质量、SDK 兼容 | 通用原语应下沉上游，不留在产品层重复维护 |
| 企业工作流/平台管理员 | Dify、n8n、Factory、Central | RBAC、升级、审计、成本、SLA | 部署升级、节点兼容、密钥、观测 | 付费强但交付重，应晚于个人/小团队验证 |

QwenPaw 推荐新增并优先验证第六类：

> **本地资料密集型的专业知识工作者和小团队**：他们不想搭 Agent Framework，也不只写代码；他们需要让 AI 在私有文件、网页、表格、文档和代码之间持续工作，最终交付可检查的成果。

这类用户仍需进一步访谈验证，不能因为竞品较少就直接假设需求成立。

### 4.2 Issue 主题揭示的共同底层缺口

跨项目重复出现的问题具有高度一致性：

1. **认证与 Provider 漂移**：OAuth、订阅额度、模型协议和 API 错误；
2. **环境碎片化**：Windows、WSL、Linux 包、IDE Host、Docker、SSH；
3. **状态不可靠**：取消后丢状态、Checkpoint 重放导致重复工具调用、消息静默丢失；
4. **长任务资源失控**：Context 不压缩、内存爆炸、流式输出卡死、无限循环；
5. **工具副作用无幂等**：重试导致重复发邮件、付款、写文件或执行命令；
6. **记忆质量不可见**：静默丢记忆、冲突、过期、垃圾记忆、评测无法复现；
7. **治理需求上升**：HITL、Guardrail、身份、审计和 action receipt；
8. **输出缺少客观完成标准**：Agent 声称完成不等于 Patch、文件或业务结果可用。

因此，QwenPaw 的技术领先应定义为“状态、证据、幂等和成果契约领先”，而不是 Tool 数量或 Agent 数量领先。

## 五、ZCode 对 QwenPaw 战略的具体修正

ZCode 已经证明以下概念不是空白：

- Workspace 和 Task Sidebar；
- 任务分组、时间线、状态点和代码变更量；
- 目标管理、完成校验、长任务恢复；
- 本地 / SSH / Docker 工作区；
- 内置终端、浏览器、DevTools 和页面元素上下文；
- Git 分支上下文、文件与历史会话引用；
- Desktop/Web/CLI 共用 Agent Runtime；
- 自定义 Subagent、Skill 与项目 `AGENTS.md`。

这迫使 QwenPaw 把差异再向前推进：

| 维度 | ZCode 强项 | QwenPaw 应验证的不同方向 |
|---|---|---|
| 工作对象 | 代码仓、分支、终端、网页预览 | PDF/文档/表格/邮件/网页/代码的混合资料 |
| 用户 | 软件开发者、GLM Coding Plan 用户 | 技术型知识工作者、研究/数据/内容小团队 |
| 结果 | 代码变更与开发任务 | 多类型 Artifact、来源、验收与后续复用 |
| 记忆 | 项目指令、会话引用、长上下文 | ReMe 文件化长期资料与跨任务连续性 |
| 触达 | Desktop/Web/CLI、Mobile Remote | 本地工作台 + 核心 IM 发起/审批/通知 |
| 引擎 | 自研 ZCode Agent 深度适配 GLM | 多引擎可替换，但只做必要能力契约 |
| 部署 | Local/Remote Dev | 本地私有资料、可选 Hub、小团队共享 |

如果 Research/Data 两个 Outcome Pack 不能产生明显留存，QwenPaw 的 Workspace 战略就缺乏足够差异，应退回更窄的垂直场景，而不是与 ZCode 正面比拼编码体验。

## 六、结果型闭源产品：对 Research / Data / Artifact 假设的二次反证

开源仓库分析会天然漏掉最终用户真正每天使用的成品。加入结果型产品后，“AI 工作空间”和“生成 Artifact”同样已经拥挤。

| 产品 | 一等对象 / 技术路径 | 已形成的能力闭环 | 主要用户 | 对 QwenPaw 的压力 |
|---|---|---|---|---|
| NotebookLM | Notebook / Source；基于来源的检索与生成 | PDF/Docs/Slides/URL/视频来源、引用问答、研究、报告、表格、音视频 Artifact | 学生、研究者、知识工作者、企业资料用户 | “本地资料到带引用成果”已有极强成品体验 |
| ChatGPT Deep Research | Research Run / Report；网页、文件、Browser、Python、MCP/App | 规划、进度、中途干预、数百来源综合、引用与分析产物 | 金融、科学、政策、工程和高价值消费决策用户 | 通用研究质量和连接器投入难以正面对抗 |
| OpenAI Prism | Scientific Project / Paper | 论文结构、公式、参考文献、协作与模型共处同一项目 | 科研人员 | 垂直工作空间必须比“通用 Artifact”更具体 |
| Perplexity | Research / Computer / Project | 搜索、Research、文件与应用创建、多模型、长期 Brain | 研究型个人、学生、企业信息工作者 | 搜索分发和答案体验强，QwenPaw 不能只做 Deep Research |
| Manus | Agent Task / Project | Chat/Agent Mode、Wide Research、网站/Slides/Video 等交付 | 想委托完整任务的个人与团队 | 多 Agent 并行和结果交付已有强品牌 |
| Genspark | Workspace / SecondBrain / Suite | Memory + Super Agent + Slides/Sheets/Docs/Mail/Code + GenTeam | 知识工作者、内容创作者、小团队 | 与“记忆 + 工作空间 + 多 App + 团队”路线几乎全面重叠 |
| Cursor Cloud Agents | Code Task / VM / Evidence | 隔离 VM、后台长任务、日志/截图/视频证明、人工接管 | 专业软件团队 | 编码 Artifact 与证据链已被成熟产品覆盖 |
| Factory | Mission / SDLC | 从任务到验证、企业环境和部署的一体化 | 大型研发组织 | 企业软件交付控制平面需要巨额投入 |

新的结论不是放弃 Artifact，而是把主张从“我能生成成果”进一步收紧为：

> **从用户拥有的私有本地文件夹出发，用可替换 Agent 引擎完成任务，过程可暂停、回放和审计，成果保持开放格式并留在用户环境。**

这个主张的潜在用户不是所有知识工作者，而是同时满足三个条件的人：

1. 资料敏感、离线或不能默认上传 SaaS；
2. 任务跨越多种本地文件和较长时间，普通聊天难以维持；
3. 需要可重复、可检查、可移交的成果，而非一次性回答。

若访谈显示目标用户愿意直接把资料交给 NotebookLM、ChatGPT 或 Genspark，且不在意本地、开放与可回放，那么这个差异不成立。此时 QwenPaw 应选择更窄的中国本地部署行业或回到基础设施角色。

## 七、后续仍需用一手用户研究补齐的证据

公开仓库只能看到主动反馈者，无法回答沉默流失用户。进入开发前必须补齐：

1. 访谈 10 名 QwenPaw 高频用户、10 名安装后流失用户、10 名竞品迁移用户；
2. 对每人还原最近一次真实任务，而不是询问“想要什么功能”；
3. 记录任务输入、切换工具、等待、失败、人工返工和最终交付；
4. 比较 Coding、Research、Data、Content、Personal Automation 五类任务的留存；
5. 区分个人免费用户、模型订阅用户、小团队管理员和企业平台团队；
6. 以每周已验证 Artifact、返工时间和任务中断率决定 ICP，不以问卷偏好决定。

只有当其中一个人群在相同任务上表现出稳定、高频、可付费的痛点，产品路线才从“战略假设”升级为“已验证方向”。
