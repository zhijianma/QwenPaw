import { describe, expect, it, vi } from "vitest";

import {
  createChatControlClient,
  createQwenPawClient,
  QwenPawHttpError,
} from "../../../packages/qwenpaw-client/src";

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("public QwenPaw client", () => {
  it("composes Host clients over one authenticated Fetch transport", async () => {
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(
        jsonResponse({
          schema: "qwenpaw.host-handshake.v1",
          product: "qwenpaw",
          version: "2.2.2b1",
          protocol_version: 1,
          features: ["task.runtime", "future.feature"],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          schema: "qwenpaw.kernel-model.v1",
          task_id: "task-1",
        }),
      );
    const client = createQwenPawClient({
      baseUrl: "http://localhost:8004/api/",
      token: "test-token",
      agentId: "reviewer",
      fetch,
    });

    await expect(client.runtime.handshake()).resolves.toMatchObject({
      protocol_version: 1,
      features: ["task.runtime", "future.feature"],
    });
    await client.tasks.create(
      { objective: "Review the README" },
      { idempotencyKey: "create-once" },
    );

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8004/api/version",
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    );
    const taskInit = fetch.mock.calls[1][1];
    const headers = new Headers(taskInit?.headers);
    expect(headers.get("Authorization")).toBe("Bearer test-token");
    expect(headers.get("X-Agent-Id")).toBe("reviewer");
    expect(headers.get("Idempotency-Key")).toBe("create-once");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("preserves bounded Host error details", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response("denied", {
        status: 403,
        headers: { "Content-Type": "text/plain" },
      }),
    );
    const client = createQwenPawClient({
      baseUrl: "http://localhost:8004/api",
      fetch,
    });

    await expect(client.runtime.handshake()).rejects.toMatchObject({
      name: "QwenPawHttpError",
      status: 403,
      body: "denied",
    } satisfies Partial<QwenPawHttpError>);
  });

  it("returns non-JSON artifact content without coercion", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response("# Result", {
        headers: { "Content-Type": "text/markdown" },
      }),
    );
    const client = createQwenPawClient({
      baseUrl: "http://localhost:8004/api",
      fetch,
    });

    await expect(
      client.tasks.artifactContent("task/1", "artifact/1"),
    ).resolves.toBe("# Result");
    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:8004/api/tasks/task%2F1/artifacts/artifact%2F1/content",
      expect.any(Object),
    );
  });

  it("exposes durable Chat controls without owning queue state", async () => {
    const queue = {
      schema: "qwenpaw.kernel-model.v1",
      agent_id: "reviewer",
      chat_id: "chat/1",
      revision: 3,
      active_submission_id: null,
      submissions: [],
      updated_at: "2026-10-10T00:00:00Z",
    } as const;
    const receipt = {
      schema: "qwenpaw.kernel-model.v1",
      receipt_id: "receipt-1",
      command_id: "command-1",
      submission_id: null,
      kind: "steer",
      status: "accepted",
      agent_id: "reviewer",
      chat_id: "chat/1",
      revision: 4,
      detail: "queued for the next safe point",
      applied_at_safe_point: null,
      recorded_at: "2026-10-10T00:00:01Z",
    } as const;
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(jsonResponse(queue))
      .mockResolvedValueOnce(jsonResponse(receipt));
    const client = createQwenPawClient({
      baseUrl: "http://localhost:8004/api",
      agentId: "default-agent",
      fetch,
    });

    await expect(
      client.chatControls.queue("chat/1", { agentId: "reviewer" }),
    ).resolves.toEqual(queue);
    await expect(
      client.chatControls.steer(
        "chat/1",
        {
          idempotency_key: "steer-once",
          expected_revision: 3,
          instruction: "verify the translation before writing",
        },
        { agentId: "reviewer" },
      ),
    ).resolves.toEqual(receipt);

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8004/api/chats/chat%2F1/queue",
      expect.objectContaining({ method: "GET" }),
    );
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8004/api/chats/chat%2F1/control/steer",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          idempotency_key: "steer-once",
          expected_revision: 3,
          instruction: "verify the translation before writing",
        }),
      }),
    );
    for (const [, init] of fetch.mock.calls) {
      expect(new Headers(init?.headers).get("X-Agent-Id")).toBe("reviewer");
    }
  });

  it("maps every Chat control to the stable Host route", async () => {
    const request = vi.fn().mockResolvedValue({});
    const controls = createChatControlClient({ request });
    const command = {
      idempotency_key: "command-once",
      expected_revision: 7,
    };

    await controls.submit("chat/1", {
      idempotency_key: "submit-once",
      content_parts: [{ type: "text", text: "continue" }],
    });
    await controls.interrupt("chat/1", command);
    await controls.stopAndClear("chat/1", command);
    await controls.cancelQueued("chat/1", "submission/1", command);
    await controls.reorder("chat/1", {
      ...command,
      ordered_submission_ids: ["submission/2", "submission/1"],
    });

    expect(request.mock.calls.map(([path]) => path)).toEqual([
      "/chats/chat%2F1/submissions",
      "/chats/chat%2F1/control/interrupt",
      "/chats/chat%2F1/control/stop-and-clear",
      "/chats/chat%2F1/queue/submission%2F1/cancel",
      "/chats/chat%2F1/queue/reorder",
    ]);
    expect(request.mock.calls.every(([, init]) => init.method === "POST")).toBe(
      true,
    );
  });
});
