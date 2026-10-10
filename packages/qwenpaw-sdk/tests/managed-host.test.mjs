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

test("starts, negotiates and closes one owned Host", async () => {
  const host = await QwenPawHost.create(
    options("ready", { requiredFeatures: ["chat.control.v1"] }),
  );

  assert.equal(host.handshake.schema, "qwenpaw.host-handshake.v1");
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
