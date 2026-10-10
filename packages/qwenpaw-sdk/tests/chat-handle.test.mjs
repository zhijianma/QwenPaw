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
    },
    chatControls: {
      submit: async (...args) => {
        calls.push(["submit", ...args]);
        return { kind: "enqueue" };
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
