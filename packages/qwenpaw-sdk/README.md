# `@qwenpaw/sdk`

Managed local Host SDK for applications that want to run QwenPaw rather than
connect to an existing Lite, Workstation or Hub deployment.

The SDK owns one local QwenPaw process and returns the same
`@qwenpaw/client` used by remote integrations. It does not implement an Agent
Loop, queue, approval state, checkpoint store or alternate domain model.

```ts
import { QwenPawHost } from "@qwenpaw/sdk";

await using host = await QwenPawHost.create({
  runtime: {
    executable: "/absolute/path/to/python",
    args: ["-m", "qwenpaw"],
  },
  stateDir: "/absolute/path/to/qwenpaw-state",
  requiredFeatures: ["chat.control.v1"],
});

const chat = host.chats.open("chat-spec-id");
const queue = await chat.queue();
console.log(queue);

const turn = await chat.send({
  idempotency_key: crypto.randomUUID(),
  content_parts: [{ type: "text", text: "Review the README" }],
});
const settled = await turn.wait();
console.log(settled.state, settled.outcome);

const direct = await chat.send("Review the README", {
  idempotencyKey: crypto.randomUUID(),
});

const external = await chat.send(
  {
    kind: "external",
    text: githubIssue.body,
    source: { type: "github.issue", id: String(githubIssue.number) },
  },
  { idempotencyKey: crypto.randomUUID() },
);

for (const record of await chat.artifacts()) {
  console.log(record.artifact, record.evidence);
  const response = await chat.artifactContent(record.artifact.artifact_id);
  console.log(await response.text());
}

for (const action of await chat.actions()) {
  console.log(action.request, action.result);
}

const trajectory = await chat.trajectory(settled.correlation_id);
console.log(trajectory.items);

const child = await chat.fork({
  source_message_id: "message-id",
  idempotency_key: crypto.randomUUID(),
});
```

The runtime executable is mandatory. The SDK never selects an arbitrary
`qwenpaw` from `PATH`. It starts `qwenpaw app --managed` on a pre-bound
loopback port, validates the versioned launch record, waits for the existing
Host protocol handshake and checks required features before returning.

Call `close()` or use `await using` to stop the owned process. Startup failure,
incompatible protocol, missing features and timeout all terminate that process
before rejecting.

`host.exited` resolves for every post-readiness process exit. Its record
contains the owned PID, exit code or signal, and whether `close()` initiated
the shutdown or escalated to a forced kill. Applications can therefore
distinguish graceful disposal from a runtime crash or bounded forced shutdown
without polling or waiting for an unrelated API call to fail.

`host.chats.open()` returns a convenience handle bound to `ChatSpec.id`.
History, submission, queue, steer, interrupt, stop-and-clear, approvals, fork
and runtime streams delegate to the same `@qwenpaw/client` used remotely. The
handle stores no queue or completion state, and runtime stream EOF is never
reported as successful completion.

`chat.send()` returns a `QwenPawTurn` immediately after durable admission.
`turn.follow()` streams the Host-owned execution chain and `turn.wait()`
reconciles disconnects until execution settles. A settled `inactive` state is
not promoted to `achieved`; verified goal outcomes remain explicit Host facts.
