# QWEN-176 P3：专用布局检查与规范收口

**Goal:** 为最后 10 个专用布局入口完成视觉一致性处置与宿主隔离验收，收口全页面使用表和贡献规范。

**Architecture:** 依赖 P2 验收通过。保留专用骨架，复用公共视觉语言与适用控件；只修正本需求范围内已证实的不一致，不强套配置模板或重写业务流程。

**Tech Stack:** React 18、TypeScript、现有 antd/Agentscope、CSS Modules/Less、Vitest/Testing Library、现有浏览器检查工具。

**Spec:** docs/design/qwen-176-page-survey.md；docs/design/qwen-176-state-contract.md；docs/design/qwen-176-component-contract.md；docs/design/qwen-176-migration-acceptance.md。

## Global constraints

使用 qwenpaw-codex 环境与受管 worktree；禁止全量 build/format/test；不新增依赖、不为旧模型加兼容层；保持 Windows/Linux/macOS 兼容；Python 每行 ≤79 字符，代码注释/docstring 英文；Lucide-React 图标，无 Emoji。只改本计划涉及内容，保留业务功能、字段、权限、保存与关闭语义；不新增插件 SDK；主干 push/merge/force-push 需人类同意。项目环境直接使用已注入值，不另起无必要服务。

本文件是静态计划，未执行产品验证。截图与浏览器标准见已确认第 IV 节及交接总览“各批统一证据包”；它们是本计划的必需验收项。实施前先核对一手素材和依赖，不能把报告文件存在当作视觉通过。每项开发由前端角色完成、独立评审角色验收，协调者推进下一批。

## Files/modules

- 检查以下 index.tsx 及其直接消费的共享视觉样式（相对 console/src/pages）：`Market`、`Market/Community`、`AppCenter`、`Settings/PawApps`、`Inbox`、`Agent/Checkpoints`、`Files`、`Chat`、`Login`、`Hub`。
- `console/src/pages/SettingsCenter/index.module.less` 中剩余旧选择器仅在无消费者证据成立后清理。原 `console/src/plugins/hostExternals.ts`、`pawapp-sdk/ui.tsx` 仅为契约核对材料，不改公开接口。
- 新建宿主隔离测试 `console/src/pages/SettingsCenter/specialized-isolation.test.tsx`；优先复用各页面已有测试。更新指南及矩阵，新增 `docs/verification/qwen-176/p3.md`。

## Key interfaces

专用页面保持当前路由、认证/访问门禁、Agent scope、嵌入挂载与卸载、流式及恢复协议。可复用 PageHeader/现有控件，不要求 SettingsPage。第三方页面只验证宿主兼容；本计划不新增插件组件导出。交付为每页改动或不改动的证据、公共规则消费者清单和最终开发规范。

## Task checklist

- [ ] 核对 P2 验收及剩余消费者；逐项检查专用布局，记录不一致与例外理由，先取得计划改动页面的原图。
- [ ] 在 specialized-isolation.test.tsx 增加 `does_not_apply_settings_layout_to_embedded_content`：外部嵌入根保持原布局归属；`keeps_context_and_instance_on_settings_feedback`：宿主反馈变化不重挂嵌入组件、不重置上下文。以实例挂载次数和可见控件状态断言，不仅检查 CSS 字符串。
- [ ] 复用 Chat 的 sdkSessionLifecycle.integration.test.tsx 和 Files/index.test.tsx，锁定模板相关宿主切换不破坏原会话生命周期和 Agent scope；不为外观改动重写流式逻辑。
- [ ] 对经核实的一致性问题作最小展示修正，未发现问题的页面记录保留理由。每页运行受影响测试并核对图片；不要为了显示“全部迁移”替换专用骨架。
- [ ] 验证 Inbox 访问控制、Checkpoints 恢复确认、Login 焦点与认证错误、Hub 复合面板以及应用嵌入生命周期。部署不可达入口记录证据缺口，不能冒充运行时通过。
- [ ] 在浏览器检查 Chat 流式输出、Files 工作区、嵌入应用与配置模板并存时主题和滚动隔离；当前安装插件清单只用于界定已验证范围，未知插件不记通过。
- [ ] 按使用关系搜索旧选择器与组件消费者，最后删除确实无消费者的布局规则；保留仍需的公共控件、历史入口和插件契约。删除后再跑受影响文件。
- [ ] 完成 36 入口对账、所有改动页面的图片索引与指南：组件使用表、四类示例、例外、状态/保存边界和开发 checklist。每条未验证说明缺少何种证据。
- [ ] 独立提交，交评审与人类视觉验收；不由实施者自行豁免图片、行为或兼容性要求。

## Verification

在 console 执行；阈值退出码 0、失败 0、新契约用例不跳过。页面未改且没有共享影响时无需反复跑其测试；下列宿主隔离与关键专用链路为本批收口集。

```sh
node --max-old-space-size=4096 ./node_modules/vitest/vitest.mjs run \
  src/pages/SettingsCenter/specialized-isolation.test.tsx \
  src/pages/Market/index.test.tsx \
  src/pages/Market/Community/index.test.tsx \
  src/pages/AppCenter/index.test.tsx \
  src/pages/Inbox/index.test.tsx \
  src/pages/Agent/Checkpoints/RestoreModal.test.tsx \
  src/pages/Files/index.test.tsx \
  src/pages/Chat/sdkSessionLifecycle.integration.test.tsx \
  src/pages/Login/index.test.tsx \
  src/pages/Hub/index.test.tsx
```

Settings/PawApps 的宿主嵌入隔离纳入 specialized-isolation.test.tsx；实际可达性由浏览器证据确定。最终 36 入口的适配/例外记录完整率为 100%，所有可达受影响页面的必需浏览器项和图片配对通过；未验证不得算通过，缺少证据不能用测试文件数量抵消。

最终核对命令（仓库根目录）：`git diff --check`，退出码 0。配对附件与人工视觉签收另行审阅，此命令不代表视觉通过。新增模板业务请求/持续轮询数 0；不宣称未经测量的性能收益。

## Review Focus

插件与嵌入内容不被设置 CSS 穿透；流式对话和文件工作区不新增主滚动层；恢复与认证确认不被视觉按钮绕过；最后清理规则时不误删仍有消费者的旧路径。前两项由隔离测试与浏览器共同证明，后两项由现有业务测试及消费者检索取证。

## 深度简报草稿

- 关键风险：若设置选择器穿透嵌入根，则应用布局改变；若以反馈更新为 key，则会话或应用重挂；若只看配置路由就删旧样式，则专用工作区失去布局。
- 一手素材：console/src/pages/SettingsCenter/index.module.less；console/src/pages/AppCenter/index.tsx；console/src/pages/Files/index.tsx；console/src/pages/Chat/index.tsx；console/src/plugins/hostExternals.ts；design.md:600。
- 方法与工具：隔离特征测试固定生命周期，浏览器验证嵌入、流式与工作区滚动；删除前搜索真实消费者，全入口矩阵逐项对账。
- 外部判据：执行本文件 Verification 的定点命令和 git diff --check，退出码均 0；36 项处置完整、适用验收通过、必需截图缺失 0 后交付，未知第三方页面明确排除在已验证声明外。
