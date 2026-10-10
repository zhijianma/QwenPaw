import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  createQwenPawClient,
  QwenPawHttpError,
} from "@qwenpaw/client";
import { ManagedHostError, QwenPawHost } from "../dist/index.js";

const fixture = fileURLToPath(
  new URL("./fixtures/fake-runtime.mjs", import.meta.url),
);

function options(mode, extra = {}) {
  return {
    runtime: { executable: process.execPath, args: [fixture] },
    env: { FAKE_QWENPAW_MODE: mode },
    startupTimeoutMs: 2_000,
    shutdownTimeoutMs: 1_000,
    ...extra,
  };
}

async function readRuntimeStream(stream) {
  const first = await stream.next();
  const done = await stream.next();
  assert.equal(first.done, false);
  assert.equal(done.done, true);
  return { projection: first.value, cursor: done.value };
}

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error?.code === "ESRCH") return false;
    throw error;
  }
}

async function waitForProcessExit(pid, timeoutMs = 1_000) {
  const deadline = Date.now() + timeoutMs;
  while (processExists(pid) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return !processExists(pid);
}

test("starts, negotiates and closes one owned Host", async () => {
  const host = await QwenPawHost.create(
    options("ready", {
      requiredFeatures: ["chat.control.v1", "chat.execution-manifests.v1"],
    }),
  );

  assert.equal(host.handshake.schema, "qwenpaw.host-handshake.v1");
  assert.ok(host.handshake.features.includes("chat.control.v1"));
  assert.ok(host.handshake.features.includes("chat.execution-manifests.v1"));
  assert.match(host.apiUrl, /^http:\/\/127\.0\.0\.1:\d+\/api$/);
  assert.ok(host.pid > 0);
  await host.close();
  await host.close();
  const exit = await host.exited;
  assert.equal(exit.pid, host.pid);
  assert.equal(exit.expected, true);
  assert.equal(exit.forced, false);
  assert.ok(exit.code !== null || exit.signal !== null);
});

test("remote and managed modes preserve Chat values, errors and streams", async () => {
  const host = await QwenPawHost.create(options("ready"));
  const remote = createQwenPawClient({ baseUrl: host.apiUrl });
  const managedChat = host.chats.open("chat-1");

  try {
    assert.deepEqual(
      await managedChat.history(),
      await remote.chats.history("chat-1"),
    );
    assert.deepEqual(
      await managedChat.runtime(),
      await remote.chats.runtime("chat-1"),
    );

    const managedStream = await readRuntimeStream(
      managedChat.followRuntime({ afterCursor: "cursor-1" }),
    );
    const remoteStream = await readRuntimeStream(
      remote.chats.followRuntime("chat-1", { afterCursor: "cursor-1" }),
    );
    assert.deepEqual(managedStream, remoteStream);
    assert.equal(managedStream.cursor, "cursor-2");

    const managedError = await host.chats
      .open("missing")
      .history()
      .catch((error) => error);
    const remoteError = await remote.chats
      .history("missing")
      .catch((error) => error);
    assert.ok(managedError instanceof QwenPawHttpError);
    assert.ok(remoteError instanceof QwenPawHttpError);
    assert.deepEqual(
      { status: managedError.status, body: managedError.body },
      { status: remoteError.status, body: remoteError.body },
    );
  } finally {
    await host.close();
  }
});

test("reports an unexpected runtime exit after readiness", async () => {
  const host = await QwenPawHost.create(options("crash-after-handshake"));

  assert.deepEqual(await host.exited, {
    pid: host.pid,
    expected: false,
    forced: false,
    code: 31,
    signal: null,
  });
  await host.close();
});

test("aborts turn observation without interrupting Host execution", async () => {
  const host = await QwenPawHost.create(options("stream-holds"));
  const controller = new AbortController();

  try {
    const turn = await host.chats.open("chat-1").send("keep running", {
      idempotencyKey: "submission-1",
    });
    const stream = turn.follow({ signal: controller.signal });
    const running = await stream.next();
    assert.equal(running.done, false);
    assert.equal(running.value.state, "running");

    const waiting = stream.next();
    await new Promise((resolve) => setTimeout(resolve, 50));
    controller.abort(new DOMException("Stop observing", "AbortError"));
    await assert.rejects(waiting, (error) => error?.name === "AbortError");

    const projection = await host.client.chats.runtime("chat-1");
    assert.equal(projection.execution_chains[0].state, "running");
  } finally {
    await host.close();
  }
});

test("reports a Host crash while a turn stream is active", async () => {
  const host = await QwenPawHost.create(options("crash-during-stream"));

  try {
    const turn = await host.chats.open("chat-1").send("crash while running", {
      idempotencyKey: "submission-1",
    });
    const stream = turn.follow();
    const running = await stream.next();
    assert.equal(running.value.state, "running");

    const waiting = stream.next().then(
      () => undefined,
      (error) => error,
    );
    const [exit, streamError] = await Promise.all([host.exited, waiting]);
    assert.deepEqual(exit, {
      pid: host.pid,
      expected: false,
      forced: false,
      code: 32,
      signal: null,
    });
    assert.ok(streamError instanceof Error);
  } finally {
    await host.close();
  }
});

test(
  "reports forced shutdown escalation",
  { skip: process.platform === "win32" },
  async () => {
    const host = await QwenPawHost.create(
      options("ignore-term", { shutdownTimeoutMs: 25 }),
    );

    await host.close();
    const exit = await host.exited;
    assert.equal(exit.expected, true);
    assert.equal(exit.forced, true);
    assert.equal(exit.signal, "SIGKILL");
  },
);

test(
  "force-closes the owned POSIX Host process group",
  { skip: process.platform === "win32" },
  async () => {
    let workerPid;
    const host = await QwenPawHost.create(
      options("child-ignores-term", {
        shutdownTimeoutMs: 100,
        onOutput({ line }) {
          if (line.startsWith("QWENPAW_TEST_CHILD ")) {
            workerPid = Number(line.slice("QWENPAW_TEST_CHILD ".length));
          }
        },
      }),
    );

    assert.ok(Number.isSafeInteger(workerPid));
    assert.equal(processExists(workerPid), true);
    await host.close();
    assert.deepEqual(await host.exited, {
      pid: host.pid,
      expected: true,
      forced: true,
      code: 0,
      signal: null,
    });
    assert.equal(await waitForProcessExit(workerPid), true);
  },
);

test("fails closed when the Host is missing a required feature", async () => {
  await assert.rejects(
    QwenPawHost.create(
      options("missing-feature", {
        requiredFeatures: ["chat.control.v1"],
      }),
    ),
    (error) =>
      error instanceof ManagedHostError &&
      error.code === "HOST_FEATURE_MISSING",
  );
});

test("rejects an incompatible Host handshake", async () => {
  await assert.rejects(
    QwenPawHost.create(options("invalid-handshake")),
    (error) => error?.code === "HOST_PROTOCOL_INCOMPATIBLE",
  );
});

test("rejects an invalid Host handshake without retrying", async () => {
  await assert.rejects(
    QwenPawHost.create(options("invalid-handshake-value")),
    (error) => error?.code === "HOST_HANDSHAKE_INVALID",
  );
});

test("rejects an invalid launch record", async () => {
  await assert.rejects(
    QwenPawHost.create(options("invalid-launch")),
    (error) =>
      error instanceof ManagedHostError && error.code === "HOST_LAUNCH_INVALID",
  );
});

test("rejects a launch record outside the exact API root", async () => {
  await assert.rejects(
    QwenPawHost.create(options("invalid-api-root")),
    (error) =>
      error instanceof ManagedHostError && error.code === "HOST_LAUNCH_INVALID",
  );
});

test("reports a runtime that exits before launch", async () => {
  await assert.rejects(
    QwenPawHost.create(options("exit")),
    (error) =>
      error instanceof ManagedHostError && error.code === "HOST_PROCESS_EXITED",
  );
});

test("reports an executable that cannot be spawned", async () => {
  await assert.rejects(
    QwenPawHost.create({
      ...options("ready"),
      runtime: { executable: `${fixture}.missing` },
    }),
    (error) =>
      error instanceof ManagedHostError && error.code === "HOST_PROCESS_ERROR",
  );
});

test("times out and terminates a runtime without a launch record", async () => {
  await assert.rejects(
    QwenPawHost.create(
      options("no-launch", {
        startupTimeoutMs: 50,
      }),
    ),
    (error) =>
      error instanceof ManagedHostError &&
      error.code === "HOST_STARTUP_TIMEOUT",
  );
});
