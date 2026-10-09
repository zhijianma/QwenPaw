# QwenPaw Capability Conformance Matrix

> 状态：Lite 基础设施契约 v1
> 范围：内置能力、插件能力与兼容边界
> 管理 API：`GET /api/plugins/capability-conformance`

## 1. 目的

QwenPaw 不能把“插件能安装”当成“插件和内置能力行为一致”。Capability
Conformance Matrix 为每个稳定 Slot 绑定系统实现、插件夹具和仓库内证据，新增
Slot 如果没有相应条目会直接触发测试失败。

这个矩阵是 Host 持有的只读声明，不由插件自行上报。API 返回的
`semantics=declared_evidence_not_runtime_health` 明确表示它不是实时健康状态，也不表示
当前部署已经运行过引用的测试。真实运行健康仍由 Capability Registry、promotion
evidence 和 observation 分别负责。

## 2. 四种证据等级

| Slot stability | 证据等级 | 当前含义 |
|---|---|---|
| `public` | `behavior_contract` | 系统实现和插件实现通过同一行为套件 |
| `system` | `system_lifecycle` | Host 私有边界完成系统装配与生命周期验证 |
| `experience` | `activation_contract` | 只证明热激活和投影，不宣称 UI 行为对齐 |
| `compatibility` | `migration_boundary` | 只为旧注册路径保留，必须迁往公开 Provider Slot |

等级由 `SlotContract.stability` 唯一决定，不能将 activation 测试包装成行为合同。

## 3. 当前结论

- 16 个公开运行 Slot 已具备行为合同：Mode、Command、Hook、Stop Gate、Planner、
  Strategy、Tool、Runner、Harness、Driver、Memory、Prompt、Sensor、Scheduler、
  Delivery 和 Artifact Renderer。
- `agent.factory` 是 Lite Host 私有系统边界，第三方插件不得接管 Agent 构造。
- `engine`、`tool`、`memory`、`scheduler` 是兼容边界，不属于新插件 API 的行为对齐
  范围。
- 5 个 `ui.*` Slot 目前只完成 activation/projection 证据。按照 Chat-first 顺序，
  Task Workbench 行为合同继续后置，矩阵不会把它们标成已完成。

## 4. 代码与门禁

- `src/qwenpaw/capabilities/conformance.py` 是唯一声明源。
- `CAPABILITY_CONFORMANCE` 必须与 `SLOT_CONTRACTS` 精确同集合。
- 公开 Slot 必须同时声明系统实现、插件夹具和 `tests/contract/os/` 行为测试。
- Experience Slot 必须声明 Host 投影实现、插件夹具和 activation 证据。
- 仓库测试会校验所有证据路径存在，并覆盖缺失 Slot、伪装证据等级等失败场景。

二次开发者可以读取管理 API 判断 Slot 的成熟度，但应运行对应 node id 才能证明
本地版本通过合同。插件 promotion evidence 仍用于证明某个具体候选版本的准入结果，
两者不能互相替代。
