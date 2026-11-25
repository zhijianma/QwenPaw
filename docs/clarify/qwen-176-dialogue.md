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
- 推荐语音转录页只是架构师提案，尚非人类选择。

## 已确认节清单

- [x] 启动视觉方向与首批交付边界（历史已同意）
- [ ] 首个真实配置页
- [ ] 总体方案选择
- [ ] I 架构边界、组件职责与不变量
- [ ] II 状态与保存契约
- [ ] III 接口及前端职责
- [ ] IV 迁移、实施范围与验收
- [ ] 独立计划交接

## 待确认问题队列

1. 本轮：A 语音转录页（推荐）或 B 环境变量页，作为唯一首批样板。
   A 可检验字段组合、条件显隐、帮助及真实异步状态；B 可检验列表、只读
   和抽屉，但会使首批重心偏向列表编辑，增加保存路径与破坏性操作的验证。
2. 样板选定后核对公共组件适用边界：仅仓库内开发者/Agent 复用，还是
   已有插件入口也在首批消费范围。先检查公开导出契约，再据证据提问。
3. 两项语义收敛后，独立给出 2–3 个总体方案；人类选择后逐节确认。
   不重复询问既定视觉方向、不把等待选择标为 blocked。
