import assert from "node:assert/strict";
import test from "node:test";

import { createQwenPawClient } from "../dist/index.js";

test("cancels the runtime stream when the consumer stops after a snapshot", async () => {
  let cancelled = false;
  let request;
  const payload = {
    agent_id: "agent-1",
    chat_id: "chat-1",
    queue: { chat_id: "chat-1", revision: 1, submissions: [] },
    interactions: [],
    cursor: "cursor-2",
    observed_at: "2026-10-10T00:00:00Z",
  };
  const client = createQwenPawClient({
    request: async () => payload,
    openStream: async (path, init) => {
      request = { path, headers: new Headers(init?.headers) };
      const body = new ReadableStream({
        start(controller) {
          controller.enqueue(
            new TextEncoder().encode(
              `id: cursor-2\nevent: snapshot\ndata: ${JSON.stringify(
                payload,
              )}\n\n`,
            ),
          );
        },
        cancel() {
          cancelled = true;
        },
      });
      return new Response(body, {
        headers: { "Content-Type": "text/event-stream" },
      });
    },
  });

  const stream = client.chats.followRuntime("chat-1", {
    agentId: "agent-1",
    afterCursor: "cursor-1",
  });
  const first = await stream.next();
  assert.equal(first.done, false);
  assert.deepEqual(first.value, payload);

  await stream.return();

  assert.equal(cancelled, true);
  assert.equal(request.path, "/chats/chat-1/runtime/stream");
  assert.equal(request.headers.get("Last-Event-ID"), "cursor-1");
  assert.equal(request.headers.get("X-Agent-Id"), "agent-1");
});
