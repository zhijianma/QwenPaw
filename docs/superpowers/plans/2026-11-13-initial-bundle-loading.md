# 首屏资源按需加载 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. 按用户要求由当前智能体执行，禁止 subagent。使用复选框维护进度，不自动 commit 或 push。

**Goal:** 消除首屏对聊天及统计图表实现的非必要加载，保留直接进入聊天页时的立即加载及所有现有功能。

**Architecture:** 第一阶段仅修正 KaTeX CSS、lodash 的分包归属。第二阶段保持插件初始化与注册时机，隔离同一会话 Context 的轻量依赖，并按使用加载工具卡片实现。分阶段检查构建图、网络行为和功能，不混合为一次不可归因的修改。

**Tech Stack:** React、TypeScript、Vite 6、Rollup、Vitest；使用现有依赖及 Node v24.22.1。

**Spec:** [已确认设计](../specs/2026-11-13-initial-bundle-loading-design.md)

## Global Constraints

- 不改会话状态来源、分页逻辑、插件 API、后端存储、迁移、fork、checkpoint 或 backup 语义。
- 保留 10 MiB 原始体积、3 MiB Brotli 门槛；不关闭预加载来掩盖静态依赖，不删除功能，不新增依赖或兼容层。
- 直接打开聊天页时立即加载聊天包，不人为延迟消息显示。
- 不改变公式样式的全局加载时机；不全局设置 `moduleSideEffects: false`。
- 不复制 Context，不使用 URL 或新 store 替换 SDK 状态；不升级或修改 node_modules。
- 不启动或重启已有后端。生产资源验证优先复用适合的现有服务；如开发服务不提供生产产物，单独安排临时预览，不修改 5175 的代理配置。
- 用户已确认设计与实施计划；在 perf/initial-bundle-lazy-loading 分支执行，不提交、不推送。

## Review Focus

1. 冷启动设置页并等待空闲初始化：检查实际请求，不把移除 preload 标签误当成按需加载（任务 1、4）。
2. 会话切换和 fork 后插件 hooks：必须观察到原 Context 的新值，而非另一份 Context 的默认值（任务 2、4）。
3. 卡片资源未加载完就收到流式更新或完成信号：加载后必须展示最新内容与状态（任务 3）。
4. 外部插件覆盖内置卡片、未知工具使用通用卡片：优先级和回退语义不变（任务 3）。
5. 直接访问深链、加载失败及非聊天页面使用 Markdown：保持现有错误恢复和实际使用入口（任务 3、4）。

## 文件范围

| 文件 | 责任 |
|---|---|
| `console/vite.config.ts` | 两阶段局部分包归属，不重写全局分包策略 |
| `console/src/plugins/hostSdk/hooks.ts` | 只调整 SDK hook 的导入边界，保留所有导出签名及状态来源 |
| `console/src/components/Chat/ToolCards/lazyRegistry.tsx`（新增） | 集中维护延迟卡片注册表及稳定的加载包装组件 |
| `console/src/components/Chat/ToolCards/cards/index.ts` | 如需避免重复映射，将注册表重导出到新入口；保留现有组件导出及类型接口 |
| `console/src/components/Chat/ToolCards/registerBuiltinCards.ts` | 使用轻量注册表，保留注册时机与幂等逻辑 |
| `console/src/components/Chat/ToolCards/adapters/v1Adapter.tsx` | 消除通用回退卡片的提前加载，保留 props 解析与回退行为 |
| 现有 hooks、注册表、适配器、卡片测试 | 增补必要的交互验证，不新增孤立的分包规则单元测试 |
| `console/scripts/verify/initial-bundle-loading/` | 精简构建图、体积、网络及回归记录，不放完整 dist |

不预设必须修改 `PluginContext.tsx` 或 `hostSdk/install.ts`：当前初始化次序应保留，优先调整其下游导入。

## 任务 1：静态依赖定点修正与阶段一验收

**Files:** 修改 `console/vite.config.ts`；复用 `console/scripts/verify-initial-bundle.mjs` 与 `verify-monaco-css.mjs`。

**Interfaces:** 输入为现有 `manualChunks(id)`；输出仍为分组名或 undefined，不改变其他构建接口。

- [x] 确认 HEAD、工作区状态和设计中的基线仍匹配；若代码已变化先刷新基线。将已完成对照的精简数据保存到验证目录。
- [x] 在聊天及图表规则之前，定点识别 `katex/dist/katex.min.css` 和 `lodash/` 模块（包含 CommonJS 虚拟包装 ID），分别归入 `math-styles` 与 `shared-lodash`；保持 App 的 CSS 导入和其他规则不变。
- [x] 在 `console` 执行 `npm run build`。预期退出码 0，raw ≤ 10,485,760 字节、Brotli ≤ 3,145,728 字节，Monaco 样式检查通过；不修改检查脚本来放行。
- [x] 复用诊断构建采集模块图，核查完整静态可达集合、Context 归属及循环关系，确认 CSS 不再把聊天实现带入首屏，lodash 不再把统计图表带入聊天。
- [x] 冷缓存核对公式、Mermaid、聊天深链及统计图表；记录首屏 raw/Brotli 与加载请求。阶段一允许空闲回调继续加载聊天包，明确记录这项未完成目标。
- [x] 自查 diff：若出现样式丢失、执行顺序错误或新的循环依赖，先在本任务解决，不继续叠加阶段二。

## 任务 2：保留 Context 单例，隔离插件 hooks

**Files:** 修改 `console/src/plugins/hostSdk/hooks.ts`、`console/vite.config.ts`；验证 `hooks.test.ts`、`hooks.host.test.ts`、`install.test.ts`。

**Interfaces:** 保留 `useHostCurrentSession(): HostSessionInfo | null` 及全部 host API；读取原 `ChatAnywhereSessionsContext`。

- [x] 读取 SDK 的 Sessions、Messages、Options Context 及其静态依赖；从模块图确定必须同组的循环依赖闭包。确认该闭包不要求完整渲染实现；否则按设计暂停本任务并报告，不修改 SDK 或状态来源。
- [x] 将 hook 改为 SDK 现有 `lib/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereSessionsContext` 入口导入；在 `manualChunks` 中为核实的轻量闭包设置局部分组，优先于整包聊天规则。不仅凭深层导入就判定成功。
- [x] 在现有 `hooks.test.ts` 中补充真实 SDK Provider 集成场景，验证插件 hook 与聊天使用的 Context 是同一对象，Provider 值更新后 hook 同步变化；调整必要的 mock 路径，保留原有断言。
- [x] 在 `console` 执行 `npm run test:run -- src/plugins/hostSdk/hooks.test.ts src/plugins/hostSdk/hooks.host.test.ts src/plugins/hostSdk/install.test.ts`，预期全部通过。
- [x] 构建并核查轻量组没有反向引用完整聊天渲染组；不能复制 Context 或动态替换 hook 实现来规避依赖。

## 任务 3：注册保持同步，卡片实现延迟加载

**Files:** 新增 `lazyRegistry.tsx`；修改文件范围表中的注册与适配器文件；复用现有 `registerBuiltinCards.test.ts`、`adapters/v1Adapter.test.tsx`。

**Interfaces:** 保持 `BUILTIN_CARD_REGISTRY: Record<string, BuiltinCardComponent>`、`registerBuiltinCards(): void`、`adaptRegistryForV1(...)` 和 `withGenericFallback(...)` 的现有接口与语义。

- [x] 先在现有注册及适配器测试中补充 deferred import 场景：注册后没有加载卡片实现；首次渲染才加载；加载期间 props 更新，完成后显示最新内容。确认现有实现无法满足“注册不加载”的断言。
- [x] 新增轻量注册入口，使用稳定、在模块作用域创建的 React lazy 包装组件和显式动态 import。保留工具名称、别名共用同一组件、注册幂等及覆盖优先级；只维护一份注册映射，不复制现有清单。
- [x] 包装组件保持 `BuiltinCardProps`，在卡片局部使用 Suspense 与现有加载样式；结合现有 `ChunkErrorBoundary`、`lazyWithRetry` 的可用接口复用失败恢复。不要用会吞掉加载错误的空白边界替代恢复，也不改变 Hook 调用顺序。
- [x] `registerBuiltinCards` 引用轻量入口；`v1Adapter` 的 GenericToolCard 也引用延迟包装。不要只改注册表而保留通用卡片的静态重型导入；原 props 解析和 Proxy 回退逻辑不变。
- [x] 在现有适配器测试中验证未知工具回退、外部插件覆盖、别名、延迟期间的完成/停止状态、加载拒绝后的可见恢复；不暂时展示错误类型的通用卡片。
- [x] 在 `console` 执行 `npm run test:run -- src/components/Chat/ToolCards/registerBuiltinCards.test.ts src/components/Chat/ToolCards/adapters/v1Adapter.test.tsx src/components/Chat/ToolCards/cards src/components/Chat/ToolCards/shared`，预期全部通过。
- [x] 检查 PluginProvider 的加载状态、插件加载顺序和 host API 可用时机没有改变。

## 任务 4：最终生产验证及交付

**Files:** 仅更新设计/计划复选框和验证目录证据；发现问题时回到责任任务修复，不扩大业务范围。

- [x] 在 `console` 执行 `npm run test:run` 与 `npm run build`，保存退出码及结果；失败须区分本次与既有问题，不将未通过写成完成。
- [x] 使用生产产物验证：无聊天组件的设置页冷启动，等待插件初始化结束，完整聊天及图表实现仍未请求；不能用 Vite 开发模式的请求数量作为验收证据。
- [x] 冷启动聊天深链，聊天实现按路由立即请求；消息显示、工具卡片及插件 hooks 正常。打开图表前不请求统计图表实现，打开后显示正常，重复打开无额外重复加载。
- [x] 在慢网络下验证首次工具卡片、持续流式更新、加载错误恢复、Inbox Markdown、公式、Mermaid、超过 32K 的 Markdown/代码及超过 200 行的表格。
- [x] 用内置智能体复验会话切换、双向分页、流式重连、中间 turn fork、继续对话与再次 fork；复验 checkpoint/backup 恢复后的展示和续聊。沿用已批准的测试数据边界，不清理用户数据。
- [x] 保存每项浏览器场景的实际结果、网络证据与关键截图；记录未覆盖项，不沿用旧轮次成功结果。
- [x] 对照设计逐项自查：状态来源、API、注册顺序、分页和后端均未改语义；复核 `git diff --check`、文件清单及临时配置恢复。
- [x] 汇总实际节省体积及首屏/空闲/按需三个阶段的加载差异。无证据不承诺固定节省值，不把延后几秒下载称为真正按需加载。

## 回退及执行交接

- 阶段一通过后保留独立 diff 记录；阶段二的 Context 或卡片行为有问题时只撤回对应改动，不撤销用户原有代码。
- 不使用整目录覆盖或 `git reset --hard` 回退，不自动 commit/push。
- 文档自查已覆盖设计中的两阶段、加载边界、Context 单例、卡片覆盖、异常恢复及功能回归。SDK 闭包是否可隔离是任务 2 的明确验证门槛，不是已证明的结果。
- 用户确认实施计划后，由当前智能体使用 `superpowers:executing-plans` 按任务执行；阶段一完成后先记录结果，再进入第二阶段。


## 本轮验收记录（2026-11-13）

- 基线 HEAD：`64c7f9ca56a2e7663f45a5b01eb1c86d09378a44`；实施分支：`perf/initial-bundle-lazy-loading`，未提交、未推送。
- 最终 `npm run build` 退出码 0，Monaco CSS、预压缩与原有包体积门槛通过。首屏 raw 从 10,495,667 降至 8,441,302 字节；Brotli 从 2,709,335 降至 2,251,431 字节。
- 完整前端测试：691 个文件通过；8,895 项通过、2 项 expected fail，退出码 0。命令为 `npm run test:run -- --maxWorkers=2 --exclude '**/scripts/verify/**'`。排除的是存放旧源码副本的验证归档目录，没有排除当前正式测试。
- checkpoint/backup 现有测试：305 通过、2 跳过。本轮实际恢复后 runtime 与快照完全一致；每种恢复均再次续聊、fork、续聊、再次 fork、续聊。
- 浏览器使用最终生产产物（临时 4185 代理 8003）：设置页空闲后不加载完整聊天/图表；进入聊天路由请求聊天实现；进入统计详情请求图表实现，重复进入不重复请求且三张图正常显示。
- 卡片展开才请求实现；一次 503 故障显示已有恢复入口并自动恢复。延迟期间最新 props、完成/停止状态、未知工具回退和已注册组件优先级结合现有适配器、注册及完整测试验证。
- 真实工具调用、公式、Mermaid、会话切换、滚动、首轮定位、生成中刷新、中间 Turn fork、续聊与再次 fork 已实测。分页接口四页共 18 条消息，与全文响应逐项一致且 ID 无重复。
- 大文本和 Inbox 使用临时浏览器响应样例，未写 DB：47,279 字符 Markdown、56,113 字符/330 行代码、240 行表格；末尾表格约 34 行挂载，页面无横向溢出。Inbox 的标题、粗体、公式、代码、表格正常。附件及插件卡片覆盖主要由现有自动化测试提供证据，没有声称逐一人工操作所有卡片。
- 未修改已有 5175 代理或重启后端。临时响应拦截与缓存设置已恢复，临时预览服务已停止。
- 最终审查按用户要求由当前智能体完成，未使用 subagent；不将自查描述为独立审查。

精简证据保存在 `console/scripts/verify/initial-bundle-loading/`（Git 忽略目录）：`final-size.json`、`stage2-final-static-graph.json`、`stage2-final-idle.json`、`chat-route-on-demand.json`、`chart-route-on-demand.json`、`chart-repeat.json`、`frontend-final.log`、`restore-results.json`、`pagination-api-result.json` 及截图。
