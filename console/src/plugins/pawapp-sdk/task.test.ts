import { beforeEach, describe, expect, it, vi } from "vitest";

import { hostFetch } from "../hostSdk/fetch";
import { createScopedPawTask, PawTaskTransportError } from "./task";
import type { PawTaskInteractionRequest } from "./types";

vi.mock("../hostSdk/fetch", () => ({
  hostFetch: vi.fn(),
}));

const mockedHostFetch = vi.mocked(hostFetch);

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(events: unknown[]): Response {
  const encoder = new TextEncoder();
  const body = events
    .map((event) => `data: ${JSON.stringify(event)}\n\n`)
    .join("");
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(body));
        controller.close();
      },
    }),
    {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    },
  );
}

describe("PawTask interactions", () => {
  beforeEach(() => {
    mockedHostFetch.mockReset();
  });

  it("preserves the confirmation envelope and submits a response", async () => {
    const interaction: PawTaskInteractionRequest = {
      interaction_id: "interaction-1",
      agent_id: "default",
      chat_id: "chat-child",
      invocation_id: "11111111-1111-4111-8111-111111111111",
      revision: 1,
      message: "Continue?",
      data: { branch: "feature" },
      options: [
        { option_id: "approve", label: "Approve" },
        { option_id: "deny", label: "Deny" },
      ],
    };
    mockedHostFetch
      .mockResolvedValueOnce(jsonResponse({ task_id: "task-1" }))
      .mockResolvedValueOnce(
        sseResponse([
          { type: "pawapp:confirm_request", ...interaction },
          { type: "done", data: { ok: true } },
        ]),
      )
      .mockResolvedValueOnce(jsonResponse({ status: "resolved" }));

    const task = createScopedPawTask(
      "review-app",
      "/run",
      {},
      { chatId: "chat-child" },
    );
    const received = vi.fn();
    task.on("pawapp:confirm_request", received);

    await expect(task.result).resolves.toEqual({ ok: true });
    expect(mockedHostFetch).toHaveBeenNthCalledWith(
      1,
      "/review-app/run",
      expect.objectContaining({
        headers: {
          "Content-Type": "application/json",
          "X-QwenPaw-Chat-Id": "chat-child",
        },
      }),
    );
    expect(received).toHaveBeenCalledWith({
      type: "pawapp:confirm_request",
      ...interaction,
    });

    await task.respond(interaction, {
      optionId: "approve",
      idempotencyKey: "approve-once",
      values: { reviewed: true },
    });

    expect(mockedHostFetch).toHaveBeenLastCalledWith(
      "/chats/chat-child/interactions/interaction-1/response",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          idempotency_key: "approve-once",
          expected_revision: 1,
          selected_option_ids: ["approve"],
          text: "",
          values: { reviewed: true },
        }),
      }),
    );
  });

  it("inherits the current ChatSpec identity by default", async () => {
    const originalQwenPaw = window.QwenPaw;
    window.QwenPaw = {
      host: {
        getCurrentChatId: () => "chat-current",
      },
    } as typeof window.QwenPaw;
    mockedHostFetch
      .mockResolvedValueOnce(jsonResponse({ task_id: "task-2" }))
      .mockResolvedValueOnce(
        sseResponse([{ type: "done", data: { ok: true } }]),
      );

    try {
      const task = createScopedPawTask("review-app", "/run", {});
      await expect(task.result).resolves.toEqual({ ok: true });
      expect(mockedHostFetch).toHaveBeenNthCalledWith(
        1,
        "/review-app/run",
        expect.objectContaining({
          headers: {
            "Content-Type": "application/json",
            "X-QwenPaw-Chat-Id": "chat-current",
          },
        }),
      );
    } finally {
      window.QwenPaw = originalQwenPaw;
    }
  });

  it("rejects transport EOF instead of reporting null success", async () => {
    mockedHostFetch
      .mockResolvedValueOnce(jsonResponse({ task_id: "task-interrupted" }))
      .mockResolvedValueOnce(sseResponse([]));

    const task = createScopedPawTask("review-app", "/run", {});
    const onError = vi.fn();
    task.on("error", onError);

    await expect(task.result).rejects.toMatchObject({
      name: "PawTaskTransportError",
      code: "PAW_TASK_STREAM_INTERRUPTED",
      message: expect.stringContaining("outcome is unknown"),
    });
    await task.result.catch((error: unknown) => {
      expect(error).toBeInstanceOf(PawTaskTransportError);
    });
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({
        code: "PAW_TASK_STREAM_INTERRUPTED",
      }),
    );
  });
});
