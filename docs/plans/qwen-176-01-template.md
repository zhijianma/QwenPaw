# QWEN-176 P1：统一模板与跨类型页面

**Goal:** 用宿主及十个真实页面验证统一模板，并交付贡献指南首版和全部入口的处置矩阵。

**Architecture:** 在现有 SettingsCenter 内组合 SettingsPage/SettingsSection；保留业务 hook 和控件。基线、组件、页面与对应样式隔离同批落地，之后独立验收。

**Tech Stack:** React 18、TypeScript、现有 antd/Agentscope、CSS Modules/Less、Vitest/Testing Library、现有浏览器检查工具。

**Spec:** docs/design/qwen-176-page-survey.md；docs/design/qwen-176-state-contract.md；docs/design/qwen-176-component-contract.md；docs/design/qwen-176-migration-acceptance.md。

## Global constraints

使用 qwenpaw-codex 环境与受管 worktree；禁止全量 build/format/test；不新增依赖、不为旧模型加兼容层；保持 Windows/Linux/macOS 兼容；Python 每行 ≤79 字符，代码注释/docstring 英文；Lucide-React 图标，无 Emoji。只改本计划涉及内容，保留业务功能、字段、权限、保存与关闭语义；不新增插件 SDK；主干 push/merge/force-push 需人类同意。项目环境直接使用已注入值，不另起无必要服务。

本文件是静态计划，未执行产品验证。截图与浏览器标准见已确认第 IV 节及交接总览“各批统一证据包”；它们是本计划的必需验收项。实施前先核对一手素材和依赖，不能把报告文件存在当作视觉通过。每项开发由前端角色完成、独立评审角色验收，协调者推进下一批。

## Files/modules

- 新建 `console/src/components/settings/index.ts`、`SettingsPage.tsx`、`SettingsSection.tsx`、`settings.module.less`、`SettingsPage.test.tsx`、`SettingsSection.test.tsx`。
- 修改 `console/src/pages/SettingsCenter/index.tsx`、`index.module.less`；复用 `console/src/components/PageHeader/index.tsx` 和 `interaction/SettingsField.tsx`、`SettingsDrawer.tsx`、`InlineHelp/index.tsx`（路径由执行者开工核对，勿假定必须修改）。
- 页面 index.tsx 及对应私有布局：`Control/Heartbeat`、`Agent/Config`、`Settings/Security`、`Settings/VoiceTranscription`、`Settings/Models`、`Settings/Environments`、`Settings/TokenUsage`、`Settings/Debug`、`Settings/Backups`、`Import`（均相对 console/src/pages）。只改适配需要的子组件/样式。
- 新建 `docs/development/settings-page-guide.md`、`docs/verification/qwen-176/p1.md`；维护全入口矩阵。新增页面适配回归 `console/src/pages/SettingsCenter/template-contract.test.tsx`，优先补现有业务测试而非复制测试套件。

## Key interfaces

- 产出 `SettingsPage(props: SettingsPageProps)` 和 `SettingsSection(props: SettingsSectionProps)`，props 严格按第 III 节：header/navigation/toolbar/feedback/children；id/title/help/actions/feedback/width/children 及两个显式搜索属性。返回 React 元素。
- PageHeaderProps 原样透传；无 value/onSave/onNavigate/agentId/endpoint。readable 为 max-width:48rem 且不超容器，默认 full。
- 宿主显式识别新模板以退出该页面的旧覆盖；稳定搜索值由页面传入，不从标题推导。数据、Form、保存和弹层控制器仍属原页面。
- 下游 P2/P3 消费同一导出与贡献指南，不直接导入模板私有样式。

## Task checklist

- [ ] 建立 36 入口处置矩阵并核对实际可达性；为本批 10 页及宿主受影响场景采集真实原版，记录服务代码身份。未取得基线的页面不得开始展示改动。
- [ ] 读取各页保存/关闭/搜索调用点，在 p1.md 记录真实请求契约及状态。补原有特征测试：旧 Agent 草稿、Config/Security 切换差异、Models 部分失败、变量新建/编辑、先备份取消与成功推进。先让这些断言在当前未迁移实现上通过。
- [ ] 在 SettingsPage.test.tsx 新增 `keeps_editor_mounted_during_feedback_updates`：rerender feedback 后输入值和控件节点身份保持；`renders_one_existing_header`：data-page-header 数量为 1。
- [ ] 在 SettingsSection.test.tsx 新增 `preserves_explicit_search_targets`：传入 target/labels 原值保留；`does_not_register_another_form_item`：已有 Form.Item 的输入绑定与错误关联不重复。运行新文件，确认因组件缺失而失败，非依赖或配置错误。
- [ ] 实现纯布局组件，复用 PageHeader/InlineHelp，保留分区语义；不注册保存或取数副作用。按页接入，保持原组件 key、Form 和弹层挂载条件。
- [ ] 在 template-contract.test.tsx 覆盖搜索打开隐藏分区后找到原 target；异步反馈变化不替换表单；未迁移宿主页面保留旧样式归属。断言实际定位/节点身份，不能只比 class 字符串。
- [ ] 每迁一页只退出该页对应的宿主旧覆盖；跑对应页面测试与下面公共集。全部通过再删除该页无消费者私有布局。
- [ ] 浏览器逐页验证滚动、搜索、键盘和弹层，尤其 SettingsDrawer 在移动端失败关闭后的焦点；完成前后图片附件与 p1.md，按第 IV 节逐项给结论。
- [ ] 指南写出表单、集合、数据、流程四种可复制组合示例，列出禁止接管业务生命周期、搜索标记、主题、滚动和截图规则。示例只消费本批导出的真实接口。
- [ ] 独立提交组件及页面迁移的可审阅增量；批次验收通过后由协调者放行 P2。

## Verification

在 console 目录执行；新增测试文件须在实现步骤创建后运行。阈值：退出码 0、失败 0，新增契约用例不跳过。

```sh
node --max-old-space-size=4096 ./node_modules/vitest/vitest.mjs run \
  src/components/settings/SettingsPage.test.tsx \
  src/components/settings/SettingsSection.test.tsx \
  src/pages/SettingsCenter/template-contract.test.tsx \
  src/pages/SettingsCenter/useSettingsSearchTarget.test.tsx \
  src/hooks/useAutoSave.test.tsx \
  src/pages/Control/Heartbeat/index.test.tsx \
  src/pages/Agent/Config/components/RuntimeWorkbench.test.tsx \
  src/pages/Settings/Security/useSecurityPage.test.ts \
  src/pages/Settings/VoiceTranscription/index.test.tsx \
  src/pages/Settings/Models/useProviders.test.ts \
  src/pages/Settings/Environments/index.test.tsx \
  src/pages/Settings/TokenUsage/index.responsive.test.tsx \
  src/pages/Settings/Debug/components/LogViewer.test.tsx \
  src/pages/Settings/Backups/restore/useRestoreFlow.test.ts \
  src/pages/Import/index.test.tsx
```

若具体子组件有额外修改，再加对应文件，不扩大到全部前端。浏览器与截图的完成阈值是总览规定的所有适用项通过、缺失必需配对 0、十个页面加宿主均有结果。新增业务请求与持续轮询 0。

## Review Focus

旧 Agent 请求完成不能污染新实体；默认模型失败不清空供应商；搜索定位与 Form 身份不被 feedback 更新破坏；桌面/移动弹层的关闭仍调用原保存确认。对应断言已分配到上述特征测试与 template-contract 测试，真实焦点再由浏览器验证。

## 深度简报草稿

- 关键风险：若状态分支卸载 Form，则输入和校验丢失；若按当前 Agent 补发旧草稿，则请求目标改变；若旧宿主样式继续命中新模板，则宽度/间距被覆盖。
- 一手素材：console/src/hooks/useAutoSave.tsx；console/src/pages/SettingsCenter/useSettingsSearchTarget.ts；console/src/pages/SettingsCenter/index.module.less；console/src/pages/Settings/Models/useProviders.ts；console/src/pages/Settings/Backups/restore/useRestoreFlow.ts；design.md:526。
- 方法与工具：重构特征测试固定原行为；Testing Library 验证控件身份/搜索；浏览器验证真实滚动、弹层与主题，修改前后截图验证视觉差异。
- 外部判据：执行本文件 Verification 的完整定点命令，退出码 0、失败及跳过的新契约用例 0；结合 p1.md 全部必需浏览器项及配对附件通过后停止，不扩展业务范围。
