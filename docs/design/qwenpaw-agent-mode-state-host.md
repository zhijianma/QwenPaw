# QwenPaw Agent Mode State Host 契约

## 1. 定位

`AgentModeSession` 是一次 Invocation 的行为快照，不应承担跨 Invocation 持久化。
需要延续的交互模式状态必须通过 `AgentModeHost` 写入 Host-owned State Store，插件不得
直接打开 `.qwenpaw/lite` 数据库，也不得借用 transport `session_id` 建立第二套主键。

稳定归属为：

```text
provider_id + agent_id + ChatSpec.id + state_key
  -> AgentModeState revision N
  -> Invocation pinned generation writes revision N+1
```

## 2. Kernel 契约

`AgentModeState` 使用 `qwenpaw.agent-mode-state.v1`，保存：

- Host 绑定的 provider、Agent 和 `ChatSpec.id`；
- provider 自有 `state_key` 与 JSON value；
- provider 数据结构版本 `state_schema_version`；
- Host 并发版本 `revision`；
- 最后写入该值的 `writer_registry_epoch_id + writer_generation`。

单值序列化后最多 64 KiB。大内容必须成为 Artifact 或 provider 自有的内容寻址对象，
Mode State 只保留引用。无稳定 `ChatSpec.id` 的兼容 Channel 不开放持久 Mode State。

## 3. Host API

插件只获得：

```python
current = await host.read_state("lifecycle")
saved = await host.write_state(
    {"phase": "review"},
    expected_revision=current.revision if current else 0,
    state_key="lifecycle",
    state_schema_version=1,
)
cleared = await host.clear_state(
    expected_revision=saved.revision,
    state_key="lifecycle",
)
```

Host 自动绑定 provider、Agent、Chat 和当前 pinned generation。插件不能指定这些身份，
也不能拿到底层 `AgentModeStateStore`。陈旧 revision 以
`AgentModeStateConflictError` 失败，不做 last-write-wins。

`clear_state()` 写入空 JSON 并递增 revision，不物理删除记录。这样 `/clear`、`/new`
与并发旧 Session 之间仍保留 CAS 栅栏，不会因删除后 revision 回到 0 产生 ABA 覆盖。
Provider 的 `reset_conversation()` 应清理每个已知 state key；Host 不允许插件枚举或清理
其他 Provider 的 namespace。

## 4. 热替换语义

热替换后的新 Invocation 使用新 generation 的 Provider 代码，但读取同一 provider ID
命名空间。兼容版本可继续写入；需要迁移时应读取旧 `state_schema_version`，在一次 CAS
写入中升级结构。同一 Registry epoch 内 writer generation 只能单调前进；旧 Session
即使重新读取最新 revision，也不能降级新 Provider 状态。进程重启产生新 epoch 后，
generation 可从 1 重新开始，不会阻断持久恢复。

Provider ID 变化视为新命名空间，不隐式迁移。卸载插件不删除状态；重新安装相同
Provider ID 可恢复。状态清理与显式迁移属于后续受治理管理能力，不由插件直接删库。

## 5. Lite Adapter 与安全边界

Lite 使用 `.qwenpaw/lite/mode-state.db`，SQLite WAL 与 CAS revision 保证单机并发，
POSIX 数据库/WAL/SHM 文件限制为 owner-only。SQL 查询全部使用参数绑定；状态经过
Kernel JSON 和大小校验后才写入。

内置与插件 Provider 使用同一 Host 方法和 Store，但 provider namespace 相互隔离。
现有内置 Goal 的专用 `GoalExecutionStore` 暂不强行迁移：它包含 Outcome prepare /
declare / finalize 状态机，不能退化成无类型 JSON。Mode State 用于 provider 自有的
轻量交互状态，不取代领域专用 Store。

## 6. 验收

- Provider/Agent/Chat/state-key 之间不可串读；
- clear/reset 保留 revision 栅栏，旧 writer 不能在清理后复活状态；
- 新进程和新 Provider generation 可恢复相同 namespace；
- 陈旧 writer 无法覆盖新 revision；
- 系统与插件 Mode Host 使用同一 API 和 Adapter；
- Provider 只能通过 Host 访问，SDK 不导出 Store；
- 超过 64 KiB 的单值在落盘前拒绝；
- 示例插件真实执行 turn-start/reset 后可跨 Session 读取计数。
