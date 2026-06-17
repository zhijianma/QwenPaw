# Chat 会话管理：架构设计与调用链路

---

## 一、架构概述

### 1.1 懒创建 + 双 ID 体系

Chat 页面采用**懒创建**模式 — 点击「新建」只在前端建本地 session，发消息后 POST 成功才在后端创建 chat 并获得 UUID。

| 字段 | 说明 | 示例 |
|---|---|---|
| `id`（Library ID） | 前端生成的 `timestamp-random` 格式，全生命周期不变 | `"1718444400000-4pzxbb3"` |
| `realId`（Backend UUID） | POST 成功后由 `resolveRealId` 写入，有早退保护不可覆盖 | `"a1b2c3d4-..."` |
| `sessionId` | 与 `id` 相同，POST 时作为 `session_id` 发给后端 | `"1718444400000-4pzxbb3"` |

**核心约束**：库的 SSE controller 监听 `[currentSessionId]` 变化，变化时 abort 流。因此 SSE 期间 `id` 必须保持 localId 不变，UUID 只存在 `realId` 字段中。

> `isLocalTimestamp` 正则随 ID 格式同步更新：`/^\d+-[a-z0-9]+$/`

### 1.2 组件职责

| 组件 | 文件 | 职责 |
|---|---|---|
| **SessionApi** | `sessionApi/index.ts` | 单例。Session CRUD、双 ID 映射、`triggerResolve`、`getSessionIdentity`、`findSession` |
| **ChatPage** | `index.tsx` | 注册 4 个回调；`effectiveChatId` 三级 fallback；`customFetch` 发请求 + 触发 resolve |
| **ChatSessionInitializer** | `ChatSessionInitializer/` | URL `chatId` → 库 `currentSessionId`，三级匹配（`id` → `realId` → `sessionId`） |
| **ChatSessionDrawer** | `ChatSessionDrawer/` | 会话列表渲染（过滤未解析 session）；切换时写 `lastActiveChatId` |
| **ChatActionGroup** | `ChatActionGroup/` | Header 区操作按钮（含新建聊天） |
| **useCreateNewSession** | `hooks/useCreateNewSession.ts` | 封装三步操作：navigate → `userInitiatedCreate` → `createSession` |

### 1.3 关键机制速查

| 机制 | 位置 | 作用 |
|---|---|---|
| **`session.id` 写回** | `createSession` | 库的 `updateSession` 返回原始入参对象，库随后用 `session.id` 设 `currentSessionId`。必须将生成的 ID 写回入参，否则 `currentSessionId` 为 `undefined`，导致每次发消息都重新创建 session 并清空消息 |
| **`userInitiatedCreate`** | `createSession` | 区分「用户点击新建」和「库 SSE 结束后自动调 `createSession`」。用户点击时执行 `onSessionCreated` 回调；库自动调用时走幂等/guard 分支，不触发多余导航 |
| **`leadingUnresolved`** | `applyChatsToSessionList` | 后端轮询重建 `sessionList` 时，保留首位未解析的本地 session，防止被丢弃 |
| **Drawer 过滤** | `ChatSessionDrawer` | `sortedSessions` 用 `isUnresolvedLocalSession` 过滤掉未发消息的本地 session，消除抽屉闪现 |
| **`resolvePromise`** | `triggerResolve` / `_doGetSession` | `triggerResolve` 将异步链存为 promise；`_doGetSession` 遇到未 resolve 的 localId 时先等它完成 |
| **`effectiveChatId`** | ChatPage | 三级 fallback：URL `chatId` → `lastActiveChatId`（内存） → `getLastChatId`（sessionStorage） |
| **`updateSession` 解构** | `updateSession` | 用解构剥离 `messages`，不直接修改入参，避免突变库内部 React state |
| **`findSession` 双字段** | SessionApi | 同时匹配 `id` 和 `realId`，外部传 UUID 时也能命中 |

### 1.4 守卫一览

| 守卫 | 位置 | 作用 |
|---|---|---|
| `isSessionSwitching` | SessionApi | Drawer 切换期间屏蔽 `onSessionSelected` 和 ChatSessionInitializer |
| `lastSelectedIds` | SessionApi.getSession | 防止 displayId/realId 交替触发 `onSessionSelected` |
| `lastNavigatedChatId` | SessionApi | ChatSessionInitializer 跳过已处理的 URL 变化 |
| `lastAppliedChatIdRef` | ChatSessionInitializer | 跳过 polling 引起的重复 `setCurrentSessionId` |
| `staleAutoSelectedIdRef` | ChatPage | 拦截智能体切换后旧库的延迟 `onSessionSelected` 回调 |
| `lastSessionIdRef` | ChatPage | 同一 ID 不重复导航 |
| `lastActiveChatId` | SessionApi | 当前活跃 session 标识，仅用户主动操作写入 |
| `userInitiatedCreate` | SessionApi | 消费后重置，区分用户 vs 库自动 `createSession` |
| `resolvePromise` | SessionApi | `_doGetSession` 等待 resolve 完成后再从后端拉取 |

---

## 二、场景链路（当前正确流程）

### 2.1 新建对话

```
用户点击「新建」（useCreateNewSession hook）
  ├─ navigate("/chat", { replace: true })
  ├─ sessionApi.userInitiatedCreate = true
  └─ 库 createSession()
       └─ adapter.createSession(session)
            ├─ isUserInitiated = true → 消费并重置
            ├─ localId = "ts-random"
            ├─ sessionList.unshift(createEmptySession(localId))
            ├─ session.id = localId           ← 写回入参
            ├─ onSessionCreated(localId)
            │    ├─ lastActiveChatId = localId
            │    ├─ sessionStorage 存入
            │    └─ navigate("/chat")
            └─ return [...sessionList]

  库拿到 session.id = localId → setCurrentSessionId(localId) ✓
  Drawer: isUnresolvedLocalSession 过滤 → 不显示 ✓
```

### 2.2 首次发消息（resolve）

```
handleSubmit()
  ├─ updateSession({ id: localId, name })    → index > -1，更新字段
  ├─ syncSessionMessages({ id: localId })    → index > -1，更新字段
  └─ customFetch → POST /console/chat
       ├─ getSessionIdentity() → 从 sessionList 读（不依赖 window globals）
       └─ POST 成功
            └─ triggerResolve(localId)        ← 唯一入口，fire-and-forget
                 └─ getSessionList()
                      └─ resolveAndNotify(localId)
                           ├─ resolveRealId → s.sessionId 匹配 → realId = UUID ✓
                           │    早退保护：realId 已设则不覆盖
                           ├─ sessionStorage key 迁移：localId → UUID
                           └─ onSessionIdResolved(_tempId, realId)
                                ├─ lastActiveChatId = UUID
                                └─ navigate("/chat/UUID") ✓

  SSE 期间库调 updateSession({ id: localId })
    → index > -1 → 仅更新字段，不触发 resolve ✓
```

### 2.3 SSE 结束后库自动 `createSession`

```
SSE 结束 → 库 ensureSession()
  └─ getCurrentSessionId() === localId（之前写回的）
       └─ 不为空 → 跳过 createSession ✓

若因竞态 currentSessionId 被清空：
  └─ 库调 createSession()
       └─ adapter.createSession(session)
            ├─ isUserInitiated = false
            ├─ 幂等分支：已有未解析 localId session
            │    → session.id = existing.id ← 写回
            │    → 不触发 onSessionCreated
            └─ 或 guard 分支：lastActiveChatId 已 resolved
                 → session.id = active.id ← 写回
                 → 不触发导航
```

### 2.4 刷新页面恢复

```
刷新后（JS 状态全部重置）：
  ├─ effectiveChatId = sessionStorage 读取 → UUID 或 localId
  └─ 库 useMount → getSessionList()
       └─ applyChatsToSessionList({ id: UUID, session_id: localId })
            ├─ preferredChatId 匹配 → sessionId 回退
            │    → s.realId = UUID, s.id = localId
            └─ 排首位 → setCurrentSessionId(localId)
                 └─ _doGetSession(localId)
                      └─ findSession → realId = UUID
                           → api.getChat(UUID) → 历史消息加载 ✓
```

### 2.5 切换页面再返回

```
离开 /chat → ChatPage UNMOUNT → 回调清空，sessionList 保留

返回 /chat：
  └─ effectiveChatId 三级 fallback
       ├─ URL chatId（有 ID 时）
       ├─ lastActiveChatId（单例内存，跨导航保留）← 通常命中
       └─ sessionStorage（刷新后兜底）
  → preferredChatId 匹配 → 正确恢复 ✓
```

### 2.6 切换智能体

```
用户切换智能体
  ├─ staleAutoSelectedIdRef = 当前 session ID   ← 标记旧 ID
  ├─ lastActiveChatId = restored 或 null
  ├─ navigate("/chat/restored")
  └─ refreshKey++ → 旧库卸载，新库挂载
       ├─ 新库 → onSessionSelected(correct) → navigate ✓
       └─ 旧库 in-flight → onSessionSelected(wrong)
            → staleAutoSelectedIdRef 匹配 → 拦截 ✓
```

### 2.7 发消息时的身份读取

```
customFetch → POST
  └─ getSessionIdentity()
       └─ findSession(lastActiveChatId)   ← 双字段匹配
            → { sessionId, userId, channel }  ← 从 session 对象读

外部传 UUID 时：
  findSession(UUID) → x.realId === UUID → 命中 ✓
```

---

## 三、问题修复索引

> 以下为历史问题的精简记录。每个问题只列根因和修复要点。

### A. 核心链路修复

| # | 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|---|
| A1 | 新建对话后发第 2 条消息，前一条记录被清空 | 库的 `createSession` 用 `session.id`（undefined）设 `currentSessionId`，导致每次发消息重新调 `createSession` + `setMessages([])` | `createSession` 所有分支写回 `session.id` | `sessionApi` |
| A2 | `updateSession` 清空库内部消息数组 | `session.messages = []` 直接突变了库传入的内部引用 | 改用解构 `const { messages, ...metadata } = session` | `sessionApi` |
| A3 | `resolveRealId` 重入覆盖 UUID | 多次 `updateSession` else 分支共享 Promise，各自挂 `.then(resolveAndNotify)` | 移除 else 分支 resolve；唯一入口 `triggerResolve` | `sessionApi` |
| A4 | `onSessionIdResolved` 参数解构错误 | 第 1 个参数是 tempId 被当成 realId → URL 暴露本地时间戳 | 修正为 `(_tempId, realId)` | `index.tsx` |
| A5 | `_doGetSession` 竞态：resolve 未完成就返回空 session | `triggerResolve` 异步进行中，`_doGetSession` 提前返回 | 新增 `resolvePromise`，`_doGetSession` 先 await | `sessionApi` |

### B. 新建对话相关

| # | 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|---|
| B1 | `createSession` 未加入 sessionList | 返回旧列表，库拿到 undefined → 污染 window globals | `sessionList.unshift(extended)` | `sessionApi` |
| B2 | 抽屉新 session 闪现后消失 | `applyChatsToSessionList` 用后端数据替换列表，丢弃本地 session | `leadingUnresolved` 保留 + Drawer `isUnresolvedLocalSession` 过滤 | `sessionApi` / `Drawer` |
| B3 | SSE 结束后 URL 回退到 `/chat` | 库自动调 `createSession` → `onSessionCreated` → `navigate("/chat")` | `userInitiatedCreate` 标志区分用户 vs 库自动调用 | `sessionApi` / `useCreateNewSession` |
| B4 | 在已有对话中新建，URL 未清理 | `onSessionCreated` 中 navigate 被 ChatSessionInitializer 用旧 URL 覆盖 | hook 先 `navigate("/chat")` 再调 `createSession` | `useCreateNewSession` |
| B5 | 新建对话创建 3 个重复 session | 无幂等守卫，库多次调 `createSession` 各创建一个 | 幂等分支：已有未解析 localId 时复用 | `sessionApi` |

### C. 页面恢复与导航

| # | 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|---|
| C1 | 切换页面返回后选错 session | `preferredChatId` 仅依赖 URL，返回时为空 | `effectiveChatId` 三级 fallback | `index.tsx` |
| C2 | 刷新后 `preferredChatId` 匹配不到 | 后端 `id` 是 UUID，localId 匹配失败 | 增加 `sessionId` 回退匹配 | `sessionApi` |
| C3 | 刷新后 `_doGetSession` 永久阻塞 | `waitForRealId` 无人触发 | 删除 `waitForRealId`，有 `realId` 直接 fetch | `sessionApi` |
| C4 | Chat Not Found（navigate away 再返回） | `resolveRealId` 重入覆盖 UUID 为 localId → `getChat(localId)` 404 | `resolveRealId` 早退保护：`realId` 已设则不覆盖 | `sessionApi` |

### D. 智能体切换与身份

| # | 问题 | 根因 | 修复 | 文件 |
|---|---|---|---|---|
| D1 | 切换智能体后跳到旧 session | 旧库 in-flight 回调绕过守卫 | `staleAutoSelectedIdRef` 标记旧 ID | `index.tsx` |
| D2 | `user_id` 错误导致后端创建新 chat | `customFetch` 直接读 window globals，被旧库覆盖 | `getSessionIdentity()` 从 sessionList 读 | `sessionApi` / `index.tsx` |
| D3 | `findSession` 传 UUID 时找不到 session | 只匹配 `id` 字段（始终是 localId） | 双字段匹配：`id` 或 `realId` | `sessionApi` |

### E. 其他修正

| # | 问题 | 修复 | 文件 |
|---|---|---|---|
| E1 | `ChatSessionInitializer` 匹配逻辑反转 | 三级匹配：`id` → `realId` → `sessionId` | `ChatSessionInitializer` |
| E2 | `removeSession` 传 UUID 无法移除 | 用 `findSession` 找规范 ID 后 filter | `sessionApi` |
| E3 | `_doGetSession` 404 catch 死代码 | `error.status` 不存在，改用 `error.message.includes` | `sessionApi` |
| E4 | `preloadSession` 重复设 `isSessionSwitching` | 删除冗余赋值 | `sessionApi` |
| E5 | reconnect handler 硬编码 `user_id` | 改用 `getSessionIdentity()` | `index.tsx` |

---

## 四、改动文件速览

| 文件 | 主要改动 |
|---|---|
| `sessionApi/index.ts` | `createSession` 写回 `session.id` + 幂等 + `userInitiatedCreate`；`updateSession` 解构防突变；`triggerResolve` + `resolvePromise`；`findSession` 双字段；`lastActiveChatId`；`getSessionIdentity`；`isUnresolvedLocalSession`；`leadingUnresolved` 保留；`resolveRealId` 早退；删除 `waitForRealId` |
| `index.tsx` | `effectiveChatId` 三级 fallback；4 个回调修正（`onSessionCreated` / `onSessionSelected` / `onSessionIdResolved` / `customFetch`）；`staleAutoSelectedIdRef` 增强 |
| `hooks/useCreateNewSession.ts` | **新增**：封装 navigate → `userInitiatedCreate` → `createSession` |
| `ChatSessionDrawer/` | `sortedSessions` 过滤 `isUnresolvedLocalSession`；使用 `useCreateNewSession` |
| `ChatActionGroup/` | 使用 `useCreateNewSession` |
| `ChatSessionInitializer/` | 三级匹配修正 |
