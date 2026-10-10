import assert from "node:assert/strict";
import test from "node:test";

import { buildChatSubmission } from "../dist/index.js";

test("builds a direct user submission from plain text", () => {
  assert.deepEqual(
    buildChatSubmission("Review the README", {
      idempotencyKey: "message-1",
      priority: 10,
    }),
    {
      idempotency_key: "message-1",
      content_parts: [{ type: "text", text: "Review the README" }],
      priority: 10,
    },
  );
});

test("builds a typed lower-trust external submission", () => {
  assert.deepEqual(
    buildChatSubmission(
      {
        kind: "external",
        text: "Issue body",
        source: { type: "github.issue", id: "42" },
      },
      { idempotencyKey: "message-2" },
    ),
    {
      idempotency_key: "message-2",
      content_parts: [{ type: "text", text: "Issue body" }],
      input_trust: "external",
      external_source: {
        source_type: "github.issue",
        source_id: "42",
      },
    },
  );
});

test("rejects empty identities and external origins", () => {
  assert.throws(
    () => buildChatSubmission("hello", { idempotencyKey: " " }),
    /idempotencyKey must not be empty/,
  );
  assert.throws(
    () =>
      buildChatSubmission(
        { kind: "external", text: "payload", source: { type: " " } },
        { idempotencyKey: "message-3" },
      ),
    /input.source.type must not be empty/,
  );
});
