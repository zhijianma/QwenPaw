import { describe, expect, it, vi } from "vitest";

import {
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
});
