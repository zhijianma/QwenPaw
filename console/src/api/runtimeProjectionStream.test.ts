import { afterEach, describe, expect, it, vi } from "vitest";

import { streamRuntimeProjection } from "./runtimeProjectionStream";

vi.mock("./config", () => ({
  getApiUrl: (path: string) => `/api${path}`,
}));

vi.mock("./authHeaders", () => ({
  buildAuthHeaders: () => ({ Authorization: "Bearer test" }),
}));

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    },
  );
}

describe("streamRuntimeProjection", () => {
  afterEach(() => vi.restoreAllMocks());

  it("parses chunked snapshots and returns the latest cursor", async () => {
    const snapshot = {
      agent_id: "agent-1",
      chat_id: "chat-1",
      queue: {
        agent_id: "agent-1",
        chat_id: "chat-1",
        revision: 2,
        active_submission_id: null,
        submissions: [],
        updated_at: "2026-09-28T00:00:00Z",
      },
      interactions: [],
      cursor: "v1-2-next",
      observed_at: "2026-09-28T00:00:00Z",
    };
    const payload = JSON.stringify(snapshot);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      streamResponse([
        "id: v1-2-next\r\nevent: snap",
        `shot\r\ndata: ${payload}\r\n\r\n: keepalive\r\n\r\n`,
      ]),
    );
    const received: unknown[] = [];

    const cursor = await streamRuntimeProjection({
      chatId: "chat/one",
      agentId: "agent-1",
      afterCursor: "v1-1-old",
      onSnapshot: (projection) => received.push(projection),
    });

    expect(cursor).toBe("v1-2-next");
    expect(received).toEqual([snapshot]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/chats/chat%2Fone/runtime/stream",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer test",
          "X-Agent-Id": "agent-1",
          "Last-Event-ID": "v1-1-old",
        }),
      }),
    );
  });

  it("surfaces an HTTP failure without inventing a snapshot", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("not available", { status: 503 }),
    );

    await expect(
      streamRuntimeProjection({
        chatId: "chat-1",
        onSnapshot: vi.fn(),
      }),
    ).rejects.toThrow("Runtime stream failed: 503 - not available");
  });
});
