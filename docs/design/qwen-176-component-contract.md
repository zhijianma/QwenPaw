# QWEN-176 第 III 节：组件接口与前端职责

状态：第 I、II 节已确认；本节于第 7 轮获人类确认，尚未实现 API。
代码起点：`abaf4ed77f697c723e388f336d040594e2e2d1d4`。
依赖：全页面矩阵 `qwen-176-page-survey.md`、状态契约
`qwen-176-state-contract.md`。以下为静态推理，未在本地执行验证。

## 接口边界

在 `console/src/components/settings/` 提供库内组合入口，导出
SettingsPage、SettingsSection。不新增插件 SDK，不替换已有 interaction
控件，不建立 schema 表单引擎。这个目录和名称是本节提案。

```tsx
type SettingsPageProps = {
  header: PageHeaderProps;
  navigation?: ReactNode;
  toolbar?: ReactNode;
  feedback?: ReactNode;
  children: ReactNode;
};

type SettingsSectionProps = {
  id: string;
  title: ReactNode;
  help?: ReactNode;
  actions?: ReactNode;
  feedback?: ReactNode;
  width?: "readable" | "full";
  children: ReactNode;
  "data-settings-target"?: string;
  "data-settings-labels"?: string;
};
```

以上类型引用既有 ReactNode 与 PageHeaderProps，仅表达冻结的责任边界。
不新增 value、onSave、onNavigate、agentId 或 endpoint 属性。页面使用原有
业务控件构建 navigation、toolbar、feedback 和 actions；模板不克隆这些
节点、不替换其事件、不注册副作用。缺省插槽不占空白区域。

SettingsPage 内部只渲染一个既有 PageHeader，透传 header 全部属性；
调用者移除原来的重复页头。PageHeader 自身接口及其他消费者保持兼容。
页头下依次是可选导航、工具栏、反馈、内容。表单、集合、数据和流程
是内容组合方式，不引入四套组件或 mode 分发器。专用布局页继续用现有布局。

SettingsSection 是具名分区，不隐含 Card 背景或边框，不创建 Form。
id 用于稳定标题关联；分区使用 aria-labelledby 与标题关联，并保留
data-setting-block。搜索 target/labels 是显式透传的既有定位值，不能
从译文标题、数组位置或新 id 自动推导；原来位于字段上的目标仍留在字段。
help 复用 InlineHelp；操作所需的错误、风险和恢复提示放 feedback 常显。

width 默认 full；窄表单区域可选 readable，多列集合/图表/日志使用 full。
readable 使用统一阅读宽度样式，具体尺寸在第 IV 节视觉验收中确定；
两者都不超过父容器宽度。混合页可有多个不同宽度分区，不按路由猜类型。

## 状态呈现与业务保持

第 II 节的状态模型是业务映射契约，不新增第二套状态机。
feedback 接收原有错误、警告、进度和操作反馈节点；children 接收原有
加载、空态或可用内容。业务决定哪些状态互斥、哪些共存，模板不从节点
存在与否、Promise 返回值或定时器推断成功。不为统一文案扩展 useAutoSave。

已有数据上的刷新与局部失败只更新 feedback 或对应区域；不能切换整棵
编辑器的 key 或卸载 Form。首屏确实无数据时才使用相应占位内容。
表单继续使用 SettingsField/Form.Item 和原有控件，name、rules、dependencies、
valuePropName、校验提示及受控值传递保留；不把字段包进第二层 Form.Item。

集合编辑继续调用 SettingsDrawer；其桌面 SharedModal、移动 BottomSheet、
popup 容器和 onClose 契约保留。流程页保留原确认框和推进控制器。
反馈 live region 与 aria-busy 放在真实更新的业务区域，复用已有提示语义，
避免模板增加全页重复播报。模板不得接管焦点或将整个页面禁用。

## 宿主、滚动与样式所有权

| 层 | 拥有 | 不拥有 |
| --- | --- | --- |
| SettingsCenter | 路由、设置导航、搜索、能力过滤、上下文、现有主滚动容器 | 字段值、页内事务 |
| SettingsPage | 统一页头与插槽间距、可收缩内容布局 | 新路由、新导航状态、第二层纵向主滚动 |
| SettingsSection | 分区标题、帮助、局部反馈、宽度、稳定定位标记 | 字段注册、保存状态推断 |
| 业务页面/hook | 数据、权限、作用域、表单、请求、导航/关闭回调 | 全局重定义共享控件样式 |
| 表格/日志/编辑弹层 | 必要的局部滚动及原有焦点管理 | 改写宿主滚动与搜索算法 |

模板使用 normal flow、min-width: 0，不设固定视口高度或 overflow-y:auto；
设置中心保留主滚动。已有虚拟表格、日志及弹层保留必要局部滚动。
独立路由保留原宿主滚动；专用页面不为接入模板改变其布局结构。

组件 CSS Module 使用 design.md 的主题 token、间距与响应式规则；
不硬编码品牌色覆盖用户主题。迁移页面显式标记已使用新模板，宿主旧的
宽泛后代覆盖对这些页面退出；未迁移页保留原有样式。具体选择器修改
先查匹配关系，并与迁移页同批完成，不能先全局删除后补页面。
data-page-header、data-settings-tab/open、aria-controls 及搜索目标继续
保留在实际交互节点，不通过空壳或隐藏副本“兼容”搜索。

## 静态证据与外部契约

- PageHeader/index.tsx:8 已有 header 插槽属性；:48 输出 data-page-header。
- interaction/SettingsField.tsx:12 继承 Form.Item，:17 透传 props。
- interaction/SettingsDrawer.tsx:18 定义 onClose/surfaceId；:27 popup
  容器；:36 移动分支；:50 桌面 SharedModal。按完整组件路径位于
  `console/src/components/` 下，新增完整读取命令为
  `cat console/src/components/interaction/SettingsDrawer.tsx`，退出码 0。
- `console/src/pages/SettingsCenter/useSettingsSearchTarget.ts:51`、`:64`、
  `:88`、`:151` 消费定位属性，详见全页面调研证据。
- `console/src/pages/SettingsCenter/index.module.less:676`、`:758`、`:819`
  有共享后代覆盖；迁移必须明确新旧样式的边界。

规范依据：design.md 的 Context-specific expression、Color and brand、
Spacing、Page state contract、Asynchronous interaction stability。
所有页面必须在矩阵中落实组合或例外；动态插件只遵循原宿主契约。
本轮仅文档检查，运行时搜索、焦点、滚动、保存及截图仍需实施后验证。

## 本节两个不变量

1. 共享组件只有展示插槽与定位元数据，不拥有业务值、权限、请求或
   保存/导航生命周期；现有公共组件消费契约保持兼容。
2. 新旧页面可并存；迁移保持搜索目标、组件身份、上下文与滚动归属，
   不靠全局 CSS 覆盖获得一致性而破坏未迁移页面。

确认后第 IV 节明确迁移批次、全部入口的验收映射和配对截图要求，
不以本节确认替代实施范围确认。
