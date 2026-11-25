# QWEN-176 全页面结构盘点与统一配置页模板提案

状态：第 4 轮静态调研；架构第 I 节于第 5 轮确认，不是实施完成报告。
代码起点：`2ee6985f70ad5f2fce79a955e6242017c7509055`。

## 需求修正与证据边界

人类最新要求：先调研所有页面，再给统一配置页模板；倾向 C；
单个语音转录页不能作为样板，必须考虑所有页面。
该要求覆盖之前以语音转录单页验证模板适用性的约束。
首批公共组件仍面向仓库贡献者和 AI Agent，业务、权限、字段与保存语义
不变，验收必须提供改动前后截图对比。

本轮完成的是页面入口、结构和模板影响面盘点：

- 枚举 `console/src/pages` 的 34 个页面级 `index.tsx`，排除 components
  及 Chat/ModelSelector 内部入口；另纳入 GeneralSettings、NavigationSettings
  两个设置中心直接/间接承载的配置界面，共 36 个源码入口。
- 用 `console/src/layouts/registry/builtinRoutes.tsx` 核对内置路由；
  `console/src/App.tsx:41`、`:42`、`:465` 补齐 Login 与 Hub 外层入口。
- 用 `console/src/pages/SettingsCenter/index.tsx:118` 的设置分组和
  `:478` 的动态扩展收集机制核对设置中心覆盖范围。
- 索引不是 36 条独立可访问路由的宣称：嵌入页、复合页、未在内置路由表
  直接注册的入口也列出，避免将代码存在等同于当前部署可达。
- 插件可动态增加页面，其具体集合依赖安装状态。纳入宿主承载与注册契约，
  不声称已遍历所有第三方页面，也不把既定“无新增插件 SDK”改为插件改造。
- 下面行号是结构证据，不代表已经核验全部深层业务处理器。运行时可达性、
  实际布局、每个请求的完整保存契约与视觉截图仍待逐类验证。

已实测：页面枚举与源码读取成功；以下分类与模板判断属于静态推理，
未在本地执行验证。澄清阶段不修改产品代码、不跑测试、不启动服务。

## 页面使用矩阵

路径统一相对 `console/src/pages/`。类型是内容形态，不是新增产品分类。
“保留重点”是迁移检查项，不将未深入核验的行为写成已证明的不变量。

| 页面入口与证据 | 内容形态 | 模板适配与保留重点 |
| --- | --- | --- |
| `SettingsCenter/index.tsx:980` | 导航与内容宿主 | 保留搜索、Agent 选择、能力过滤、扩展来源与上下文 Provider |
| `SettingsCenter/GeneralSettings.tsx:407` | 通用偏好、主题与组合控件 | 表单分组模式；保留主题预览、即时偏好和桌面差异 |
| `SettingsCenter/NavigationSettings.tsx:244` | 拖拽编辑、预览与重置 | 自定义内容区域；保留键盘拖拽及编辑退出行为 |
| `Control/Heartbeat/index.tsx:198` | 条件表单 | 表单模式；`:150` 的保存所有权检查留在业务层 |
| `Agent/Config/index.tsx:365` | Form＋多分区工作台 | 分区模式；保留动态记忆组件、字段状态与搜索定位 |
| `Settings/VoiceTranscription/index.tsx:100` | 条件控件与插件状态 | 表单模式；不作为唯一验证依据；保留多请求保存 |
| `Settings/OffloadPolicy/index.tsx:12` | 单项策略设置 | 紧凑表单模式；`OffloadPolicyCard.tsx:18` 自有自动保存 |
| `Settings/Security/index.tsx:94` | 多 Tab、规则与表单 | 分区＋集合模式；保留各 Tab 保存回调与规则编辑层 |
| `Settings/Community/index.tsx:259` | 账号连接、凭据与开关 | 连接状态模式；显式提交不改为通用自动保存 |
| `Control/Channels/index.tsx:427` | 卡片集合＋三个抽屉 | 集合模式；保留渠道配置、访问控制、待审批的不同操作 |
| `Agent/ACP/index.tsx:720` | 卡片、运行时设置与抽屉 | 集合模式；运行时弹层与实例编辑分别保留 |
| `Agent/MCP/index.tsx:538` | 卡片、创建弹层及 Tabs | 集合模式；保留 JSON/表单入口及插件状态 |
| `Agent/Tools/index.tsx:407` | 控件卡片＋配置弹层 | 集合模式；弹层 Form 与自动保存留在原业务路径 |
| `Settings/Models/index.tsx:356` | 全局默认项、供应商组与多弹层 | 混合表单/集合模式；不能只保留供应商卡片 |
| `Settings/Agents/index.tsx:354` | Agent 集合、编辑与复制 | 集合模式；保留排序、编辑和复制的独立操作 |
| `Agent/Skills/index.tsx:284` | 技能卡片、多种编辑/转移层 | 集合模式；保留不同来源与只读能力区别 |
| `Settings/SkillPool/index.tsx:212` | 卡片/列表、批量操作 | 集合模式；保留批量选择、广播、导入与编辑 |
| `Settings/Environments/index.tsx:288` | 变量表、只读分组与编辑抽屉 | 集合模式；新建手动提交、已有项自动保存、敏感值展示分别保留 |
| `Control/CronJobs/index.tsx:858` | 表格/日历、任务抽屉 | 集合模式＋自定义日历；保留视图切换与模板选择 |
| `Settings/Backups/index.tsx:136` | 备份表、多阶段导入恢复 | 集合＋操作流程；保留信任、冲突、恢复前确认 |
| `Import/index.tsx:491` | 多步骤导入与任务进度 | 流程模式；保留目标 Agent、取消、检测失败恢复 |
| `Settings/TokenUsage/index.tsx:297` | 筛选、统计卡、图表及明细 | 数据模式；保留趋势与明细的独立加载/错误状态 |
| `Settings/AgentStats/index.tsx:391` | Agent 统计与图表 | 数据模式；保留日期范围与 Agent 归属 |
| `Settings/Debug/index.tsx:167` | 开关、过滤与日志视口 | 数据/诊断模式；日志独立滚动，不能挤成窄表单 |
| `Settings/PluginManager/index.tsx:166` | 安装列表与市场 Tabs | 集合/流程模式；保留安装、修复等操作反馈 |
| `Market/index.tsx:29` | 扩展市场与安装队列 | 浏览模式；安装队列不改成“保存配置” |
| `Market/Community/index.tsx:411` | 社区市场 Tabs | 浏览模式；与配置字段共享视觉语言，保留内容组织 |
| `AppCenter/index.tsx:394` | 应用目录及嵌入应用 | 浏览/工作区；保留 PawAppAccessGate 与嵌入页面生命周期 |
| `Settings/PawApps/index.tsx:138` | 应用列表及嵌入入口 | 浏览/工作区；不凭旧入口名称推断当前路由可达 |
| `Control/Sessions/index.tsx:483` | 会话表与编辑抽屉 | 管理集合；纳入全页视觉一致性，保持会话行为 |
| `Inbox/index.tsx:761` | 收件箱 Tabs 与访问控制 | 工作区；不套配置表单；保留消息与 MailAccessControlDrawer |
| `Agent/Checkpoints/index.tsx:449` | 快照图、详情与恢复 | 数据/工作区；保留图形空间和恢复确认 |
| `Files/index.tsx:23` | 文件工作区 | 保持 FilesWorkspace 与 Agent scope；共享基础视觉规范 |
| `Chat/index.tsx:5070` | 流式对话与复合工作区 | 保持运行时、审批与文件抽屉；不受配置模板布局选择器穿透 |
| `Login/index.tsx:211` | 身份认证表单 | 独立认证布局；共享控件规则，不纳入设置中心导航 |
| `Hub/index.tsx:1136` | 管理台、数据、审计及设置面板 | 复合管理工作区；配置面板可映射模式，外层保持专用布局 |

## 会推翻单页模板假设的反例

1. **分区切换语义不同。** Security `index.tsx:114` 触发当前分区保存后
   切换；RuntimeWorkbench `components/RuntimeWorkbench.tsx:34` 等待
   onNavigate 返回成功后才切换。模板不能自行统一成同一种导航守卫。
2. **搜索依赖 DOM 契约。** `SettingsCenter/useSettingsSearchTarget.ts:51`
   使用 data-settings-target，`:64` 使用 data-settings-open，`:88` 使用
   Tab 标识，`:151` 依赖 data-setting-block/Form.Item 定位高亮。
   重排 DOM 或改变挂载策略可能使搜索失效。
3. **已有父级跨页样式。** `SettingsCenter/index.module.less:676` 对后代
   按钮、卡片、表格施加规则，`:758` 改页头，`:819` 强制直接子元素全宽。
   同时 `SettingsCenter/index.tsx:983` 特别排除了 models 与扩展入口。
   新模板必须显式接管迁移页样式所有权，不能在旧覆盖规则上再叠一层。
4. **保存不是统一事务。** `hooks/useAutoSave.tsx:10` 定义延迟，`:26`
   串行 flush，`:102` 控制关闭失败确认；它返回调度接口（`:157`），
   没有可直接供模板消费的完整页级保存状态。不能用模板计时器伪造“已保存”。
5. **设置中心已是宿主。** `SettingsCenter/index.tsx:454` 从注册路由取组件，
   `:535` 做能力过滤，`:989` 传搜索上下文，`:1009` 传运行时配置上下文。
   无须再造第二套路由、Agent 选择或设置导航。

## C 修订提案：一套配置页骨架，按内容形态组合

本节架构已于第 5 轮确认；具体状态、接口和迁移分别由后续 II–IV 节细化。

```text
既有 SettingsCenter：搜索 / 分类导航 / Agent 上下文
└─ SettingsPage（配置内容骨架）
   ├─ PageHeader：标题、必要帮助、当前页操作
   ├─ 可选 Tabs / 工具栏：由页面控制选择与切换
   ├─ 页面状态区域：真实加载、错误、重试等
   └─ 内容区域
      ├─ 表单：SettingsSection + 既有字段/控件
      ├─ 集合：筛选/批量操作 + 卡片或表格 + 原编辑层
      ├─ 数据：筛选 + 图表/日志/明细
      └─ 流程：步骤 + 进度/结果 + 原确认操作
```

这些是组合模式，不是新增四个独立设计系统或强制 mode 参数。允许一页
组合多个区域，如 Models 的默认配置与供应商集合、Backups 的列表与流程。

- 页头复用 PageHeader，不再由业务页额外重复渲染。现有设置导航仍由
  SettingsCenter 管理。嵌入与独立路由通过同一内容组件呈现。
- 表单区域采用可读行宽，集合/数据区域可占满可用宽度；共享对齐边界、
  字体层级和间距。宽度由区域用途决定，不能全部套一个固定窄容器。
- 默认由内容区域承担页面滚动；日志、表格、工作区只保留有明确用途的
  内部滚动。具体滚动所有权在接口节冻结，不能直接增加双层 overflow。
- SettingsSection 负责标题、辅助帮助、区域状态和子内容；SettingsField
  保持 Form.Item 属性契约。不能为了视觉复用将受控控件改成新的表单状态源。
- 页面传入必要状态和动作；局部失败只标对应区域，不因一个区域失败卸载
  其他有效内容。是否保留已有数据由原业务契约决定。
- 保留搜索标识、Tab key、表单字段标识、弹层返回焦点目标和组件身份；
  禁止模板为了展示状态统一卸载所有内容或强制切换视图。
- 样式仅作用于迁入模板的区域。旧父级覆盖按页面迁移逐项退出，未迁页面
  与 Chat/Files/外部嵌入不因公共样式变化被顺带重排。
- 非配置页全部纳入盘点和视觉规则适用表，但保留专用工作区骨架。
  “考虑所有页面”不等于把登录、流式对话、文件编辑改成配置表单。

依据 `design.md`：Context-specific expression（因任务而异但不建平行体系）、
Color and brand identity / Custom themes and token precedence（语义 token）、
Typography and iconography / Minimal visible copy（层级和上下文帮助）、
Spacing, geometry, and composition（间距与响应式）、Page state contract
及 Asynchronous interaction stability（真实反馈与输入意图）。

## 两个核心不变量

1. **统一展示，不接管业务事务。** 每个页面原有字段、权限、Agent 归属、
   保存触发、请求拆分、确认和失败恢复均由原业务逻辑负责；模板不发请求，
   不新增总保存按钮，不将已有显式操作改为自动提交。
2. **全类型覆盖，不依赖单页通过。** 全部 36 个入口必须有适配或专用布局
   说明。模板的验证至少覆盖表单、带切换保存的多分区、集合编辑、数据/
   日志和多步骤操作；语音转录页只是其中一例，不能作为总体验收依据。

## 后续设计与验收输入

- 建议代表集：Heartbeat、Agent Config/Security、Models/Environments、
  TokenUsage/Debug、Import/Backups，加通用主题/导航配置。最终迁移批次
  在第 IV 节确认；本轮不把全页面调研自动解释为一次性全量改造。
- 全页组件使用表需记录：页面入口、模式、已有组件、私有样式、状态来源、
  保存/关闭处理器、搜索标识、迁移映射与例外原因；以上矩阵为结构底稿。
- 前后截图在任何产品修改前先记录基线，保持路由、视口、主题、语言、
  数据状态一致；不能只交一对语音页截图。每个改动页面和代表性状态都要
  对比，敏感信息不进入交付截图；截图与交互判据分别提供。
- 状态及接口深层盘点在后续设计节继续，特别是保存、Tab 切换、搜索展开、
  弹层关闭和 Agent 切换；不将本轮入口盘点包装成完整行为验收。
- 未验证：运行时插件清单、各入口部署可达性、实际渲染与各页完整交互。
  当前无性能改善主张；实施验收不因模板统一就宣称性能更好。

## 本轮证据命令

```sh
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git diff 1f6c9d6ae12b926c536a9c505e2c050e4d06c09f HEAD --stat
rg --files console/src/pages -g 'index.tsx'
cat console/src/layouts/registry/builtinRoutes.tsx
nl -ba console/src/pages/SettingsCenter/index.tsx
nl -ba console/src/pages/SettingsCenter/useSettingsSearchTarget.ts
nl -ba console/src/pages/Agent/Config/components/RuntimeWorkbench.tsx
nl -ba console/src/hooks/useAutoSave.tsx
```

长文件按行号分段读取；入口 JSX/保存调用以 Python 只读提取进行全量索引。
上述结论只引用实际读到的代码区段，不把索引当成运行时验证。

文档覆盖核验（已实测 36/36，所有矩阵行号存在；不属于产品测试）：

```sh
python3 - <<'PY'
from pathlib import Path
import re
root = Path('console/src/pages')
expected = {str(p.relative_to(root)) for p in root.rglob('index.tsx')
            if not any(x in p.parts for x in ('components', 'ModelSelector'))}
expected.update(['SettingsCenter/GeneralSettings.tsx',
                 'SettingsCenter/NavigationSettings.tsx'])
doc = Path('docs/design/qwen-176-page-survey.md').read_text()
rows = re.findall(r'^\| `([^`]+):(\d+)` \|', doc, re.M)
assert {p for p, n in rows} == expected
for p, n in rows:
    assert 0 < int(n) <= len((root / p).read_text().splitlines())
print(f'Inventory coverage: {len(rows)}/{len(expected)}')
PY
```
