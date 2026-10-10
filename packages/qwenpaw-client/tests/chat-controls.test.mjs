import assert from "node:assert/strict";
import test from "node:test";

import { createChatControlClient } from "../dist/index.js";

function fakeTransport() {
  const calls = [];
  return {
    calls,
    transport: {
      async request(path, init) {
        calls.push([path, init]);
        return [];
      },
    },
  };
}

test("reads authoritative control history with request scope", async () => {
  const { calls, transport } = fakeTransport();
  const controls = createChatControlClient(transport);
  const abort = new AbortController();

  await controls.history("chat/one", {
    agentId: "agent-a",
    signal: abort.signal,
    limit: 25,
  });

  assert.equal(
    calls[0][0],
    "/chats/chat%2Fone/control/history?limit=25",
  );
  assert.equal(calls[0][1].headers["X-Agent-Id"], "agent-a");
  assert.equal(calls[0][1].signal, abort.signal);
});

test("rejects invalid control history limits before transport", () => {
  const { calls, transport } = fakeTransport();
  const controls = createChatControlClient(transport);

  assert.throws(
    () => controls.history("chat", { limit: 0 }),
    /between 1 and 1000/,
  );
  assert.deepEqual(calls, []);
});
