# QWEN-176 P2：配置与管理集合推广

**Goal:** 将已验证模板推广到剩余 15 个配置/管理入口，每页保留独有操作并提供配对验收。

**Architecture:** 依赖 P1 验收通过；只消费稳定模板接口，按页迁移和退出旧覆盖。通用主题、导航拖拽、日历和集合编辑沿用各自业务实现。

**Tech Stack:** React 18、TypeScript、现有 antd/Agentscope、CSS Modules/Less、Vitest/Testing Library、现有浏览器检查工具。

**Spec:** docs/design/qwen-176-page-survey.md；docs/design/qwen-176-state-contract.md；docs/design/qwen-176-component-contract.md；docs/design/qwen-176-migration-acceptance.md。

## Global constraints

使用 qwenpaw-codex 环境与受管 worktree；禁止全量 build/format/test；不新增依赖、不为旧模型加兼容层；保持 Windows/Linux/macOS 兼容；Python 每行 ≤79 字符，代码注释/docstring 英文；Lucide-React 图标，无 Emoji。只改本计划涉及内容，保留业务功能、字段、权限、保存与关闭语义；不新增插件 SDK；主干 push/merge/force-push 需人类同意。项目环境直接使用已注入值，不另起无必要服务。

本文件是静态计划，未执行产品验证。截图与浏览器标准见已确认第 IV 节及交接总览“各批统一证据包”；它们是本计划的必需验收项。实施前先核对一手素材和依赖，不能把报告文件存在当作视觉通过。每项开发由前端角色完成、独立评审角色验收，协调者推进下一批。

## Files/modules

- 修改 `console/src/pages/SettingsCenter/GeneralSettings.tsx`、`NavigationSettings.tsx` 及各自必要样式。
- 修改以下 index.tsx 及必要私有布局（相对 console/src/pages）：`Settings/OffloadPolicy`、`Settings/Community`、`Settings/Agents`、`Settings/SkillPool`、`Settings/AgentStats`、`Settings/PluginManager`；`Control/Channels`、`Control/CronJobs`、`Control/Sessions`；`Agent/ACP`、`Agent/MCP`、`Agent/Tools`、`Agent/Skills`。
- 宿主样式边界仍在 `console/src/pages/SettingsCenter/index.module.less`；新增必要适配回归 `console/src/pages/SettingsCenter/rollout-contract.test.tsx`。
- 更新 `docs/development/settings-page-guide.md`、全入口处置矩阵；新增 `docs/verification/qwen-176/p2.md`。

## Key interfaces

消费 P1 的 SettingsPage/SettingsSection；不改变其 props 或建立第二套 wrapper。导航拖拽和日历保留原事件/键盘处理；原弹层、自动保存与手动提交分别保持。下游 P3 依赖本批完成的样式消费者清单和组件使用规范。

## Task checklist

- [ ] 核对 P1 提交与验收记录，取得本批 15 页原版和实际运行上下文；沿用全入口矩阵，不重新从零盘点。
- [ ] 逐页先记录状态、请求/关闭语义和搜索标记，核对既有测试断言；对涉及的行为补特征测试，在迁移前当前提交通过。
- [ ] NavigationSettings.test.tsx 补拖拽重排后编辑目标、键盘顺序及稳定标识断言；GeneralSettings.language.test.tsx 覆盖语言切换后实际标签与字段关联，不能仅 mock 返回翻译 key。
- [ ] 在 rollout-contract.test.tsx 增加 `preserves_manual_submit_on_community_connection`：输入不提前触发连接，原提交动作触发一次；`retains_editors_when_feedback_changes`：反馈变化不清除渠道/工具编辑器草稿。先跑并记录未满足的目标断言，再最小迁移使之通过；已有行为断言不要求人为制造失败。
- [ ] 按通用配置、集合编辑、日历/运行时设置依次迁移；每页保持原表单、数据/操作 handler，不为使用模板拆改业务 hook。
- [ ] 每页退出对应旧样式后跑该页测试及公共模板集；再删除此页无消费者的重复布局。仍服务专用页面的规则保留。
- [ ] 检查权限差异、只读/敏感值、批量操作、导入与编辑、插件安装反馈；在浏览器执行移动弹层长内容、搜索定位和失败关闭场景。
- [ ] 逐页附前后截图，记录视口、主题、语言和真实状态；更新指南的特殊内容组合例子。验收通过后独立提交，由协调者放行 P3。

## Verification

console 目录运行下列定点文件及本次修改的其他已有测试；阈值退出码 0、失败 0、新契约断言不跳过。

```sh
node --max-old-space-size=4096 ./node_modules/vitest/vitest.mjs run \
  src/components/settings/SettingsPage.test.tsx \
  src/components/settings/SettingsSection.test.tsx \
  src/pages/SettingsCenter/rollout-contract.test.tsx \
  src/pages/SettingsCenter/GeneralSettings.language.test.tsx \
  src/pages/SettingsCenter/NavigationSettings.test.tsx \
  src/pages/Settings/Community/index.test.tsx \
  src/pages/Settings/Agents/useAgents.test.ts \
  src/pages/Settings/SkillPool/components/BroadcastModal.test.tsx \
  src/pages/Settings/AgentStats/index.test.tsx \
  src/pages/Settings/PluginManager/components/InstalledPluginList.test.tsx \
  src/pages/Control/Channels/index.draft.test.tsx \
  src/pages/Control/CronJobs/index.test.tsx \
  src/pages/Control/Sessions/useSessions.test.ts \
  src/pages/Agent/ACP/index.test.tsx \
  src/pages/Agent/MCP/index.test.tsx \
  src/pages/Agent/Tools/ToolsPage.test.tsx \
  src/pages/Agent/Skills/SkillsPage.load-recovery.test.tsx
```

OffloadPolicy 的单项策略需在 rollout-contract.test.tsx 增加 `preserves_offload_save_schedule`，输入变化后的 schedule 次数和原策略值一致，不增加模板保存。上面集合是本批的最小契约集，不据现有文件名假定覆盖全部新场景。

浏览器验收按总览与第 IV 节，15 项均有处置及结果，每个实际改动页所需截图缺失 0，保存/导航/权限差异 0。p2.md 记录命令、实施 SHA、人工检查和附件索引；任一必需项未验证不进入下一批。

## Review Focus

导航拖拽后的稳定搜索标识；主题/语言变化导致标签关联丢失；渠道/工具编辑失败时草稿被重新挂载清除；手动连接意外变自动提交；插件安装接受被误报完成。对应 rollout-contract、NavigationSettings 及页面测试锁定，浏览器再验证真实焦点与主题。

## 深度简报草稿

- 关键风险：若所有表单都注册模板自动保存，则连接动作重复；若拖拽后按索引生成 id，则搜索定位错误；若收紧全页宽度，则日历/长列表重要操作不可达。
- 一手素材：console/src/pages/SettingsCenter/NavigationSettings.tsx；console/src/pages/Settings/Community/index.tsx；console/src/pages/Control/Channels/index.tsx；console/src/pages/Control/CronJobs/index.tsx；design.md:545。
- 方法与工具：迁移前特征测试＋每页小步回归；浏览器验证拖拽、键盘、日历、主题与长列表；保留真实前后图。
- 外部判据：执行本文件 Verification 的定点命令，退出码 0、失败 0；15 页的适用浏览器项和必需图片配对全部通过，停止本批，不顺带改专用布局。
