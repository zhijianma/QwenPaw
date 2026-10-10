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
