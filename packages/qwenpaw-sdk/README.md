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

const queue = await host.client.chatControls.getQueue("chat-id");
console.log(queue);
```

The runtime executable is mandatory. The SDK never selects an arbitrary
`qwenpaw` from `PATH`. It starts `qwenpaw app --managed` on a pre-bound
loopback port, validates the versioned launch record, waits for the existing
Host protocol handshake and checks required features before returning.

Call `close()` or use `await using` to stop the owned process. Startup failure,
incompatible protocol, missing features and timeout all terminate that process
before rejecting.
