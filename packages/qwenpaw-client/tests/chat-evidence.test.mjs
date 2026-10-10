import assert from "node:assert/strict";
import test from "node:test";

import { createChatClient, QwenPawHttpError } from "../dist/index.js";

function fakeTransport(contentResponse = new Response("artifact")) {
  const calls = [];
  return {
    calls,
    transport: {
      async request(path, init) {
        calls.push(["request", path, init]);
        return [];
      },
      async openStream(path, init) {
        calls.push(["openStream", path, init]);
        return contentResponse;
      },
    },
  };
}

test("reads Chat-owned results through stable evidence routes", async () => {
  const { calls, transport } = fakeTransport();
  const chats = createChatClient(transport);

  await chats.actions("chat/one", { limit: 5, agentId: "agent-a" });
  await chats.artifacts("chat/one", { limit: 6 });
  await chats.modelCalls("chat/one", { limit: 7 });
  await chats.waitConditions("chat/one", {
    limit: 8,
    includeTerminal: true,
  });
  await chats.observations("chat/one", { limit: 9, cursor: "next/1" });
  await chats.trajectory("chat/one", "correlation/1", { limit: 10 });
  const content = await chats.artifactContent("chat/one", "artifact/1", {
    disposition: "attachment",
  });

  assert.equal(await content.text(), "artifact");
  assert.equal(
    calls[0][1],
    "/chats/chat%2Fone/actions?limit=5",
  );
  assert.equal(calls[0][2].headers["X-Agent-Id"], "agent-a");
  assert.equal(calls[1][1], "/chats/chat%2Fone/artifacts?limit=6");
  assert.equal(
    calls[2][1],
    "/chats/chat%2Fone/model-calls?limit=7",
  );
  assert.equal(
    calls[3][1],
    "/chats/chat%2Fone/wait-conditions?limit=8&include_terminal=true",
  );
  assert.equal(
    calls[4][1],
    "/chats/chat%2Fone/observations/page?limit=9&cursor=next%2F1",
  );
  assert.equal(
    calls[5][1],
    "/chats/chat%2Fone/trajectories/correlation%2F1?limit=10",
  );
  assert.equal(
    calls[6][1],
    "/chats/chat%2Fone/artifacts/artifact%2F1/content" +
      "?disposition=attachment",
  );
});

test("rejects invalid evidence bounds before transport", async () => {
  const { calls, transport } = fakeTransport();
  const chats = createChatClient(transport);

  assert.throws(() => chats.actions("chat", { limit: 0 }), /between 1 and 1000/);
  assert.throws(
    () => chats.trajectory("chat", " "),
    /correlationId must not be empty/,
  );
  await assert.rejects(
    () => chats.artifactContent("chat", " "),
    /artifactId must not be empty/,
  );
  assert.deepEqual(calls, []);
});

test("preserves Host artifact download errors", async () => {
  const { transport } = fakeTransport(
    new Response("not owned", { status: 404 }),
  );
  const chats = createChatClient(transport);

  await assert.rejects(
    () => chats.artifactContent("chat", "artifact"),
    (error) =>
      error instanceof QwenPawHttpError &&
      error.status === 404 &&
      error.body === "not owned",
  );
});
