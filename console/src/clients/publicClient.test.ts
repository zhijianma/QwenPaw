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

  it("follows Chat runtime snapshots without treating EOF as completion", async () => {
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(": keepalive\r\n\r\n"));
        controller.enqueue(
          encoder.encode(
            'id: cursor-2\r\nevent: snapshot\r\ndata: {"agent_id":' +
              '"default","chat_id":"chat/1","queue":{},' +
              '"interactions":[],"cursor":"cursor-2",' +
              '"observed_at":"2026-10-10T00:00:00Z"}',
          ),
        );
        controller.close();
      },
    });
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(
      new Response(stream, {
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const client = createQwenPawClient({
      baseUrl: "http://localhost:8004/api",
      agentId: "reviewer",
      fetch,
    });

    const snapshots = [];
    const events = client.chats.followRuntime("chat/1", {
      afterCursor: "cursor-1",
    });
    let result = await events.next();
    while (!result.done) {
      snapshots.push(result.value);
      result = await events.next();
    }

    expect(snapshots).toHaveLength(1);
    expect(snapshots[0]).toMatchObject({
      chat_id: "chat/1",
      cursor: "cursor-2",
    });
    expect(result.value).toBe("cursor-2");
    const headers = new Headers(fetch.mock.calls[0][1]?.headers);
    expect(headers.get("Last-Event-ID")).toBe("cursor-1");
    expect(headers.get("X-Agent-Id")).toBe("reviewer");
  });

  it("forks from a stable source message through the shared Chat client", async () => {
    const request = vi.fn().mockResolvedValue({ id: "child-chat" });
    const openStream = vi.fn();
    const client = createQwenPawClient({ request, openStream });

    await client.chats.fork("parent/chat", {
      source_message_id: "message-7",
      idempotency_key: "fork-once",
      name: "Italian README",
    });

    expect(request).toHaveBeenCalledWith(
      "/chats/parent%2Fchat/fork",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          source_message_id: "message-7",
          idempotency_key: "fork-once",
          name: "Italian README",
        }),
      }),
    );
  });

  it("tracks a submission without treating inactive as achieved", async () => {
    const chain = (state: string) => ({
      chat_id: "chat-1",
      correlation_id: "correlation-1",
      state,
      submission_ids: ["submission-1"],
      invocation_ids: ["invocation-1"],
      head_submission_id: "submission-1",
      head_invocation_id: "invocation-1",
      latest_submission_status: state === "inactive" ? "succeeded" : "running",
      open_interaction_ids: state === "waiting_user" ? ["interaction-1"] : [],
      accepted_at: "2026-10-10T00:00:00Z",
      latest_submission_at: "2026-10-10T00:00:01Z",
    });
    const projection = (state: string, cursor: string) => ({
      agent_id: "default",
      chat_id: "chat-1",
      queue: {},
      interactions: [],
      execution_chains: [chain(state)],
      cursor,
      observed_at: "2026-10-10T00:00:01Z",
    });
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const encoder = new TextEncoder();
        for (const [state, cursor] of [
          ["waiting_user", "cursor-2"],
          ["inactive", "cursor-3"],
        ]) {
          controller.enqueue(
            encoder.encode(
              `id: ${cursor}\nevent: snapshot\ndata: ` +
                `${JSON.stringify(projection(state, cursor))}\n\n`,
            ),
          );
        }
        controller.close();
      },
    });
    const request = vi
      .fn()
      .mockResolvedValue(projection("running", "cursor-1"));
    const client = createQwenPawClient({
      request,
      openStream: vi.fn().mockResolvedValue(new Response(stream)),
    });
    const updates = [];
    const execution = client.chats.followSubmission("chat-1", "submission-1");
    let result = await execution.next();
    while (!result.done) {
      updates.push(result.value.state);
      result = await execution.next();
    }

    expect(updates).toEqual(["running", "waiting_user", "inactive"]);
    expect(result.value.state).toBe("inactive");
    expect(result.value.outcome).toBeUndefined();
  });

  it("reconciles stream EOF with an authoritative outcome", async () => {
    const running = {
      chat_id: "chat-1",
      correlation_id: "correlation-1",
      state: "running",
      submission_ids: ["submission-1"],
    };
    const achieved = {
      ...running,
      state: "achieved",
      latest_submission_status: "succeeded",
      outcome: { status: "achieved" },
    };
    const request = vi
      .fn()
      .mockResolvedValueOnce({
        chat_id: "chat-1",
        execution_chains: [running],
        cursor: "cursor-1",
      })
      .mockResolvedValueOnce({
        chat_id: "chat-1",
        execution_chains: [achieved],
        cursor: "cursor-2",
      });
    const client = createQwenPawClient({
      request,
      openStream: vi.fn().mockResolvedValue(new Response("")),
    });
    const execution = client.chats.followSubmission("chat-1", "submission-1", {
      reconnectDelayMs: 0,
    });

    expect((await execution.next()).value.state).toBe("running");
    const terminal = await execution.next();
    expect(terminal.value.state).toBe("achieved");
    expect((await execution.next()).value.state).toBe("achieved");
  });

  it("fails closed when a submission is outside a truncated window", async () => {
    const client = createQwenPawClient({
      request: vi.fn().mockResolvedValue({
        chat_id: "chat-1",
        execution_chains: [],
        execution_window_truncated: true,
        cursor: "cursor-oldest",
      }),
      openStream: vi.fn(),
    });
    const execution = client.chats.followSubmission("chat-1", "submission-old");

    await expect(execution.next()).rejects.toMatchObject({
      code: "SUBMISSION_OUTSIDE_EXECUTION_WINDOW",
      submissionId: "submission-old",
    });
  });

  it("propagates cancellation while following a submission", async () => {
    const controller = new AbortController();
    const request = vi.fn().mockResolvedValue({
      chat_id: "chat-1",
      execution_chains: [
        {
          chat_id: "chat-1",
          correlation_id: "correlation-1",
          state: "running",
          submission_ids: ["submission-1"],
        },
      ],
      cursor: "cursor-1",
    });
    const openStream = vi.fn(
      (_path: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    const client = createQwenPawClient({ request, openStream });
    const execution = client.chats.followSubmission("chat-1", "submission-1", {
      signal: controller.signal,
    });

    expect((await execution.next()).value.state).toBe("running");
    const pending = execution.next();
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });
});
