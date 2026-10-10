import assert from "node:assert/strict";
import test from "node:test";

import { QwenPawChat } from "../dist/index.js";

function fakeClient() {
  const calls = [];
  const client = {
    chats: {
      history: async (...args) => {
        calls.push(["history", ...args]);
        return { messages: [], status: "idle" };
      },
      runtime: async (...args) => {
        calls.push(["runtime", ...args]);
        return { chat_id: args[0], cursor: "cursor-1" };
      },
      actions: async (...args) => calls.push(["actions", ...args]),
      artifacts: async (...args) => calls.push(["artifacts", ...args]),
      modelCalls: async (...args) => calls.push(["modelCalls", ...args]),
      waitConditions: async (...args) =>
        calls.push(["waitConditions", ...args]),
      artifactContent: async (...args) =>
        calls.push(["artifactContent", ...args]),
      observations: async (...args) => calls.push(["observations", ...args]),
      trajectory: async (...args) => calls.push(["trajectory", ...args]),
      fork: async (...args) => {
        calls.push(["fork", ...args]);
        return { id: "child-chat" };
      },
      async *followSubmission(...args) {
        calls.push(["follow", ...args]);
        yield { state: "running" };
        return { state: "inactive" };
      },
    },
    chatControls: {
      submit: async (...args) => {
        calls.push(["submit", ...args]);
        return { kind: "enqueue", submission_id: "submission-1" };
      },
      queue: async (...args) => {
        calls.push(["queue", ...args]);
        return { chat_id: args[0], revision: 1 };
      },
      steer: async (...args) => calls.push(["steer", ...args]),
      interrupt: async (...args) => calls.push(["interrupt", ...args]),
      stopAndClear: async (...args) => calls.push(["clear", ...args]),
      cancelQueued: async (...args) => calls.push(["cancel", ...args]),
      reorder: async (...args) => calls.push(["reorder", ...args]),
    },
    interactions: {
      list: async (...args) => calls.push(["interactions", ...args]),
      respond: async (...args) => calls.push(["respond", ...args]),
    },
  };
  return { calls, client };
}

test("binds Chat operations to one ChatSpec identity", async () => {
  const { calls, client } = fakeClient();
  const chat = new QwenPawChat(client, "chat/one");

  await chat.history();
  await chat.queue();
  await chat.submit({
    idempotency_key: "submit-once",
    content_parts: [{ type: "text", text: "translate README" }],
  });

  assert.equal(chat.id, "chat/one");
  assert.deepEqual(
    calls.map((call) => call.slice(0, 2)),
    [
      ["history", "chat/one"],
      ["queue", "chat/one"],
      ["submit", "chat/one"],
    ],
  );
});

test("returns a handle bound to the Host-created fork identity", async () => {
  const { calls, client } = fakeClient();
  const parent = new QwenPawChat(client, "parent/chat");
  const child = await parent.fork({
    source_message_id: "message-7",
    idempotency_key: "fork-once",
  });

  assert.equal(child.id, "child-chat");
  assert.equal(child.spec.id, "child-chat");
  assert.deepEqual(calls[0].slice(0, 2), ["fork", "parent/chat"]);
});

test("returns a turn handle that waits on the shared runtime client", async () => {
  const { calls, client } = fakeClient();
  const chat = new QwenPawChat(client, "chat/one");
  const turn = await chat.send({
    idempotency_key: "submit-once",
    content_parts: [{ type: "text", text: "translate README" }],
  });

  assert.equal(turn.submissionId, "submission-1");
  assert.equal((await turn.wait()).state, "inactive");
  assert.deepEqual(
    calls.map((call) => call.slice(0, 2)),
    [
      ["submit", "chat/one"],
      ["follow", "chat/one"],
    ],
  );
});

test("accepts ergonomic direct and lower-trust external inputs", async () => {
  const { calls, client } = fakeClient();
  const chat = new QwenPawChat(client, "chat/one");

  await chat.send("Review the README", {
    idempotencyKey: "direct-message",
  });
  await chat.send(
    {
      kind: "external",
      text: "Issue body",
      source: { type: "github.issue", id: "42" },
    },
    { idempotencyKey: "external-message" },
  );

  assert.deepEqual(calls[0][2], {
    idempotency_key: "direct-message",
    content_parts: [{ type: "text", text: "Review the README" }],
  });
  assert.deepEqual(calls[1][2], {
    idempotency_key: "external-message",
    content_parts: [{ type: "text", text: "Issue body" }],
    input_trust: "external",
    external_source: {
      source_type: "github.issue",
      source_id: "42",
    },
  });
});

test("delegates result and evidence reads to the shared Chat client", async () => {
  const { calls, client } = fakeClient();
  const chat = new QwenPawChat(client, "chat/one");

  await chat.actions({ limit: 5 });
  await chat.artifacts({ limit: 6 });
  await chat.modelCalls({ limit: 7 });
  await chat.waitConditions({ limit: 8, includeTerminal: true });
  await chat.artifactContent("artifact-1", { disposition: "attachment" });
  await chat.observations({ limit: 9 });
  await chat.trajectory("correlation-1", { limit: 10 });

  assert.deepEqual(
    calls.map((call) => call.slice(0, 2)),
    [
      ["actions", "chat/one"],
      ["artifacts", "chat/one"],
      ["modelCalls", "chat/one"],
      ["waitConditions", "chat/one"],
      ["artifactContent", "chat/one"],
      ["observations", "chat/one"],
      ["trajectory", "chat/one"],
    ],
  );
});
