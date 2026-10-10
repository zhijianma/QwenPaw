# `@qwenpaw/client`

Dependency-free TypeScript client for a running QwenPaw Host. It uses the
same generated Kernel contracts and transport-neutral clients as the QwenPaw
Console and PawApp SDK; it does not embed or reimplement the Agent runtime.

```ts
import {
  buildChatSubmission,
  createQwenPawClient,
} from "@qwenpaw/client";

const paw = createQwenPawClient({
  baseUrl: "http://localhost:8004/api",
  token: process.env.QWENPAW_TOKEN,
});

const host = await paw.runtime.handshake();
const task = await paw.tasks.create(
  {
    objective: "Review README installation instructions",
    acceptance_criteria: ["Produce an Italian translation artifact"],
  },
  { idempotencyKey: crypto.randomUUID() },
);
await paw.tasks.start(task.task_id);

for await (const event of paw.tasks.follow(task.task_id)) {
  console.log(event.sequence, event.event_type);
}

const queue = await paw.chatControls.queue("chat-spec-id");
await paw.chatControls.steer("chat-spec-id", {
  idempotency_key: crypto.randomUUID(),
  expected_revision: queue.revision,
  instruction: "Verify the translation before writing the artifact",
});

for await (const snapshot of paw.chats.followRuntime("chat-spec-id")) {
  console.log(snapshot.cursor, snapshot.queue.revision);
}

const receipt = await paw.chatControls.submit("chat-spec-id", {
  idempotency_key: crypto.randomUUID(),
  content_parts: [{ type: "text", text: "Review the README" }],
});
if (receipt.submission_id) {
  for await (const execution of paw.chats.followSubmission(
    "chat-spec-id",
    receipt.submission_id,
  )) {
    console.log(execution.state);
  }
}
```

Use `buildChatSubmission()` when an integration wants the SDK-style text
input without adopting the managed-process package. Plain strings represent
direct user instructions. Content copied from an issue, webhook, email or
another agent must use the explicit external shape so the Host can preserve
its lower trust level:

```ts
const body = buildChatSubmission(
  {
    kind: "external",
    text: issue.body,
    source: { type: "github.issue", id: String(issue.number) },
  },
  { idempotencyKey: crypto.randomUUID() },
);
await paw.chatControls.submit("chat-spec-id", body);
```

Result inspection uses the same Chat identity and Host-owned evidence stores:

```ts
const artifacts = await paw.chats.artifacts("chat-spec-id");
const actions = await paw.chats.actions("chat-spec-id");
const trajectory = await paw.chats.trajectory(
  "chat-spec-id",
  correlationId,
);
const content = await paw.chats.artifactContent(
  "chat-spec-id",
  artifacts[0].artifact.artifact_id,
);
```

Pass a custom `QwenPawTransport` instead of Fetch options to integrate another
authentication, IPC or test transport. Completion, approval, recovery and
artifact semantics remain owned by the Host; consumers should reconcile an
ended stream with `paw.tasks.projection(taskId)`.

`chatControls` exposes server-authoritative submission, queue, steer,
interrupt, stop-and-clear, cancellation and reorder commands. The client does
not maintain a second browser queue. Every mutation uses an idempotency key and
the queue revision returned by the Host; steer instructions become effective
only at a Runtime safe point.

`chats` exposes ChatSpec creation, history, fork and authoritative runtime
snapshots. `followRuntime()` reconnects from `afterCursor` and returns the last
observed cursor when the transport ends; EOF is not a successful turn. Read the
latest runtime projection to determine queue, interaction and execution state.
`followSubmission()` reconciles stream EOF and transient disconnects against
that projection before reconnecting. An `inactive` execution means the
submission stopped successfully but has no verified business Outcome; only
`achieved`, `partial`, `not_achieved` or `abandoned` carry that meaning.

## 中文说明

该包用于让外部 TypeScript/JavaScript 程序连接正在运行的 QwenPaw Host。
Console、PawApp 和第三方程序复用同一套生成契约与客户端实现，SDK 不会另起
一套任务状态、审批队列或 Agent Loop。`baseUrl` 应指向 Host 的 `/api` 根路径。
