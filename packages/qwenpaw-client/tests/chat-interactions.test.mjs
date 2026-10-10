import assert from "node:assert/strict";
import test from "node:test";

import { createInteractionClient } from "../dist/index.js";

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

test("reads open and historical interactions with request scope", async () => {
  const { calls, transport } = fakeTransport();
  const interactions = createInteractionClient(transport);
  const abort = new AbortController();

  await interactions.list("chat/one", abort.signal);
  await interactions.history("chat/one", {
    agentId: "agent-a",
    signal: abort.signal,
    limit: 25,
  });

  assert.equal(calls[0][0], "/chats/chat%2Fone/interactions");
  assert.equal(calls[0][1].signal, abort.signal);
  assert.equal(
    calls[1][0],
    "/chats/chat%2Fone/interactions/history?limit=25",
  );
  assert.equal(calls[1][1].headers.get("X-Agent-Id"), "agent-a");
  assert.equal(calls[1][1].signal, abort.signal);
});

test("responds through the same scoped interaction transport", async () => {
  const { calls, transport } = fakeTransport();
  const interactions = createInteractionClient(transport);
  const abort = new AbortController();
  const body = {
    idempotency_key: "answer-once",
    expected_revision: 1,
    text: "continue",
  };

  await interactions.respond("chat/one", "interaction/one", body, {
    agentId: "agent-a",
    signal: abort.signal,
  });

  assert.equal(
    calls[0][0],
    "/chats/chat%2Fone/interactions/interaction%2Fone/response",
  );
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].body, JSON.stringify(body));
  assert.equal(calls[0][1].headers.get("X-Agent-Id"), "agent-a");
  assert.equal(calls[0][1].signal, abort.signal);
});

test("rejects invalid history limits before transport", async () => {
  const { calls, transport } = fakeTransport();
  const interactions = createInteractionClient(transport);

  assert.throws(
    () => interactions.history("chat", { limit: 0 }),
    /between 1 and 1000/,
  );
  assert.deepEqual(calls, []);
});
