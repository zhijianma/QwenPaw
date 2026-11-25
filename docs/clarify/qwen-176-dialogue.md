# QWEN-176 澄清台账

## 已确认结论

### 第 1 轮：2026-11-25，恢复环境并选择样板页

- 此前环境阻塞轮未形成代码结论或台账；本次为首次有效代码澄清。
- 人类回复“已关联到 QwenPawCodex 项目”。本轮 issue get 已返回项目
  `23fb825d-29fb-44d8-858d-e998b4a6c002`，状态从 blocked 恢复为 in_progress。
- 已实测：`git rev-parse --abbrev-ref HEAD` 输出
  `agent/codex/qwen-176`；`git rev-parse HEAD` 输出
  `13c976b2d2c7265cb1df9443f1bc3f496b9cee7e`。
- 不沿用上轮环境缺失结论；本轮已取得受管代码起点。
- 已确认方向来自 issue 与历史评论：暖中性色、品牌橙、克制层级，
  参考 Codex / Multica；首批现状盘点、组件使用表、设计 checklist、
  一个真实配置页，首批确认后才推进卡片列表与对话页。
- Architectural 档；使用 brainstorming 与 paw-requirement-dialogue。
- 后续只在此受管分支提交 docs，不修改产品代码、不跑测试、不启动服务。

本轮代码证据（静态读取；运行时行为未在本地执行验证）：

- `design.md:23` 要求复用既有模式且不引入平行设计系统；
  `design.md:64` 要求自定义主题优先、复用既有 token 机制；
  `design.md:99` 的 Minimal visible copy 要求次要说明转上下文帮助，
  必要状态保持可见；`design.md:162` 定义页面状态契约。
- `console/src/components/PageHeader/index.tsx:8` 已有 current、extra、
  center、subRow 等插槽；语音页 index.tsx:87、环境变量页 index.tsx:245
  均消费 current 和 extra。不得假定需要新增页头。
- `console/src/components/interaction/SettingsField.tsx:12` 继承 Form.Item
  属性，替换 tooltip 为 InlineHelp；:19 根据 valuePropName 处理 toggle。
- `console/src/pages/Settings/Security/components/ToolGuardTab.tsx:77`
  消费 name、valuePropName、tooltip；:114 消费 name、style 和 Select。
  公共字段的演化不能破坏 Form.Item 绑定语义。
- `console/src/pages/Settings/VoiceTranscription/components/TranscriptionModelCard.tsx:17`
  使用 Card、h3、常驻说明和 Input；ProviderSelectCard.tsx:20 同样独立
  排列 Card、标题、说明和 Select，:28 处理无提供商警告。
- `console/src/pages/Settings/VoiceTranscription/index.tsx:43` 接入
  useAutoSave，:55 处理加载错误与重试，:109 起按条件显示提供商设置。
- `console/src/pages/Settings/VoiceTranscription/useVoiceTranscription.ts:205`
  保存前校验 agent 归属；:224 起并行提交多个设置请求；:257 定义条件可见性。
  不能把视觉统一解释为新增整体原子保存或改成手动保存。
- `console/src/pages/Settings/Environments/index.tsx:117` 校验并保存变量，
  :153 使用自动保存，:193 区分可编辑与只读，:362 使用 SettingsDrawer，
  :373 新建显示创建按钮，:438 仅已有变量编辑触发 schedule。

复核读取命令（均在上述代码 SHA，退出码 0）：

```sh
nl -ba console/src/pages/Settings/VoiceTranscription/index.tsx
nl -ba console/src/components/interaction/SettingsField.tsx
nl -ba console/src/components/PageHeader/index.tsx
nl -ba console/src/pages/Settings/VoiceTranscription/components/TranscriptionModelCard.tsx
nl -ba console/src/pages/Settings/VoiceTranscription/components/ProviderSelectCard.tsx
nl -ba console/src/pages/Settings/Security/components/ToolGuardTab.tsx
nl -ba design.md | sed -n '1,210p'
```

需求合理性：合理（静态推理，未在本地执行验证）。既有规范和组件已覆盖
部分能力，但字段组合仍有页面私有实现；配置主题不能改变 JSX 组合及帮助
呈现方式。应复用并补齐，而非另建视觉体系。未验证：实际渲染、焦点、
对比度和保存时序，不能据源码声称这些行为已通过或存在运行时故障。

### 第 2 轮：2026-11-25，确认样板并澄清消费边界

- 人类已选择 A：语音转录页，为首批唯一真实配置页。
- 已实测：`git rev-parse --abbrev-ref HEAD` 为 `agent/codex/qwen-176`；
  `git rev-parse HEAD` 为 `1303fc168b35660cdf61cc3edb50023eeaa92bf2`。
- 已实测：`git diff 13c976b2d2c7265cb1df9443f1bc3f496b9cee7e HEAD --stat`
  仅列出台账 95 行新增。按本轮起点复核了代码差异范围，未直接沿用上轮
  勘察结论；产品文件无变化，既有证据引用第 1 轮台账，不重复读取。
- 新增静态事实（未在本地执行验证运行时行为）：
  - `console/src/plugins/hostExternals.ts:57` 的 HostExternals 提供 React、
    ReactDOM、antd 等依赖；:258 的安装逻辑未暴露 PageHeader/SettingsField。
  - `console/src/plugins/pawapp-sdk/ui.tsx:115` 的 UI namespace 提供
    registerPage 和 chat；页面接收 component 或 mount，两者必须恰有一个。
  - `console/src/plugins/dynamicModuleRegistry.ts:24` 与 :84 的自动发现
    范围是 pages，不包含 components，不能视作公共组件正式导出机制。
  - `src/qwenpaw/plugins/frontend/package.json` 已有 @qwenpaw/plugin-sdk，
    exports 为根入口及 globals；不据此断言插件无法复用任何宿主代码。
- 证据读取命令：`nl -ba console/src/plugins/hostExternals.ts`（分两段
  1–220、221–368 行读取）、`nl -ba console/src/plugins/pawapp-sdk/ui.tsx`、
  `nl -ba console/src/plugins/dynamicModuleRegistry.ts`、
  `cat src/qwenpaw/plugins/frontend/package.json`，退出码均为 0。
- 推导：库内复用和独立插件复用是不同契约。首批若包含后者，需要显式的
  组件导出、类型、样式与宿主依赖约定；不能把库内路径当成稳定公开 API。
- 提案 A（推荐）：首批服务修改仓库的外部贡献者及 AI Agent，提供可导入
  组件、使用表、示例和 checklist；独立插件沿用现有能力，不新增组件 SDK。
- 提案 B：独立插件/PawApp 也须在首批直接复用同一套组件，则首批设计增加
  公开组件契约与插件消费验证。该选择尚待人类确认。
- 未验证：插件实际挂载与主题联动；本轮没有据此声称其通过或失败。

### 第 3 轮：2026-11-25，总体方案比较

- 人类确认第 2 轮 A：首批服务参与仓库开发的外部贡献者和 AI Agent。
  独立插件/PawApp 的新增公共组件入口不在首批范围。
- 已实测：`git rev-parse --abbrev-ref HEAD` 输出 `agent/codex/qwen-176`；
  `git rev-parse HEAD` 输出 `1f6c9d6ae12b926c536a9c505e2c050e4d06c09f`。
  `git diff 1303fc168b35660cdf61cc3edb50023eeaa92bf2 HEAD --stat`
  仅有台账 35 行新增、8 行删除。按本轮起点复核差异范围，不直接沿用
  旧结论；产品文件未变，引用前两轮证据，避免重复探测。
- 已确认需求语义全部收敛。本轮为独立方案轮，尚未获得方案选择。
- 三案共同边界：首批交付盘点、组件使用表、规范 checklist 和语音转录页；
  保留现有主题、业务字段、权限、保存时机与请求拆分，提供前后截图比对；
  不新增独立插件接口，不推广其他业务页，不改变既有 Form.Item 消费契约。
- A：现有基础组件加组合规范。重排样板，复用 PageHeader、InlineHelp、
  适用的 SettingsField 与现有控件，分组和状态布局仍由页面编排。
  替换页面重复排版；不新增共享配置布局抽象。成本最低，跨页面一致性
  依赖规范及评审，页面仍可能复制布局。
- B（推荐）：共享小型配置组合。复用上述组件，按现有组件盘点结果补齐
  必要的分组/字段布局能力；展示层接收标题、帮助、控件和状态插槽，
  不拥有业务值或保存。先将样板重复排版迁至组合；保留业务 hook 和回调。
  布局一致性由共享实现承担，业务一致性由原有状态与请求契约及特征测试
  约束。成本中等；需验证已有组件消费者不受样式与属性变更影响。
- C：统一配置页模板。集中页头、内容宽度、分区和页面级状态插槽，业务
  内容仍通过插槽传入，不使用 schema 表单引擎，不接管请求。
  替换语音页外层状态分支与布局组合；基础组件保持复用。
  页面结构一致性更强，成本较高；首批只有一个真实样板，模板适用边界
  缺少第二页验证，不应承诺所有配置页都适用。
- 三案均不提供新增原子保存保证；语音页多个保存请求的现有语义保持。
  原子性不因视觉封装而产生，不增加统一提交或事务。
- 推荐 B 的理由：在本轮三个选项中，它把已发现的重复字段/分组布局
  收敛为共享实现，同时不必把页面状态编排迁入统一模板；与单页验证、
  真实可复用组件、业务保存不变这三项要求最匹配。
- 不选 B 的后果：A 更轻但约束更多依赖人为执行；C 更统一但迁移面更大。
- 以上均为静态设计推理，未在本地执行验证；视觉收益须由实现后的
  配对截图及交互验证证明，不声称当前已完成视觉或性能改进。
- 选定后按 I 架构边界、II 状态与保存、III 接口及前端职责、
  IV 迁移与验收逐节确认，再独立计划交接。

## 人类编号约束台账

- 本轮没有新增编号约束；人类已完成项目关联。
- 本轮进行中追加约束：验收不要使用修改前后页面截图做比对。
  此条已被人类紧接着的明确更正覆盖，不再执行。
- 本轮最新更正（有效）：验收必须提供修改前后页面截图做比对。
  实施前记录原页面，实施后在相同路由、视口、主题、语言和代表性数据
  状态下截图，成对提供；截图比对与状态契约、定点交互判据共同验收。
  本轮为静态澄清，尚未采集页面截图；实施计划必须包含改动前采集步骤。
- 历史边界保持：业务功能、字段、权限和保存语义不变；不新增产品能力。
- 保留品牌与既有主题能力；首批只改一个真实配置页；不提前推广。
- 第 2 轮已确认：首批样板为语音转录页（A）。
- 第 3 轮已确认：首批面向仓库贡献者和 AI Agent（第 2 轮 A）；
  此前的字母选择不代表已选择本轮架构方案。

## 已确认节清单

- [x] 启动视觉方向与首批交付边界（历史已同意）
- [x] 首个真实配置页：语音转录页
- [x] 首批公共组件消费边界：仓库贡献者和 AI Agent
- [ ] 总体方案选择
- [ ] I 架构边界、组件职责与不变量
- [ ] II 状态与保存契约
- [ ] III 接口及前端职责
- [ ] IV 迁移、实施范围与验收
- [ ] 独立计划交接

## 待确认问题队列

1. 本轮等待总体方案：A 现有组件加组合规范、B 共享小型配置组合（推荐）、
   C 统一配置页模板。需求语义剩余 0 项，不再询问已确定边界。
2. 选定后下一轮只给第 I 节完整设计及两个核心不变量，逐节确认。
   不重复询问既定视觉方向、不把等待选择标为 blocked。
