import { beforeEach, describe, expect, it, vi } from "vitest";

import { hostFetch } from "../hostSdk/fetch";
import {
  createInteractionsNamespace,
  PawInteractionError,
} from "./interactions";

vi.mock("../hostSdk/fetch", () => ({ hostFetch: vi.fn() }));

const mockedHostFetch = vi.mocked(hostFetch);

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("PawApp interactions", () => {
  beforeEach(() => mockedHostFetch.mockReset());

  it("lists open interactions by the current ChatSpec id", async () => {
    mockedHostFetch.mockResolvedValueOnce(jsonResponse([]));

    const interactions = createInteractionsNamespace(() => "chat/one");
    await expect(interactions.list()).resolves.toEqual([]);

    expect(mockedHostFetch).toHaveBeenCalledWith(
      "/chats/chat%2Fone/interactions",
      { signal: undefined },
    );
  });

  it("uses one optimistic command for approval, input, or suggestion", async () => {
    mockedHostFetch.mockResolvedValueOnce(
      jsonResponse({
        interaction_id: "interaction/1",
        status: "resolved",
        revision: 2,
        detail: "resolved",
        resolved_at: "2026-10-10T00:00:00Z",
      }),
    );
    const interactions = createInteractionsNamespace(() => "chat-1");
    const request = {
      interactionId: "interaction/1",
      expectedRevision: 1,
      selectedOptionIds: ["approve_exact"],
      values: { reviewed: true },
      idempotencyKey: "resolve-once",
    };

    const first = interactions.respond(request);
    expect(interactions.respond(request)).toBe(first);
    await expect(first).resolves.toMatchObject({ status: "resolved" });
    expect(mockedHostFetch).toHaveBeenCalledTimes(1);
    expect(mockedHostFetch).toHaveBeenCalledWith(
      "/chats/chat-1/interactions/interaction%2F1/response",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          idempotency_key: "resolve-once",
          expected_revision: 1,
          selected_option_ids: ["approve_exact"],
          text: "",
          values: { reviewed: true },
        }),
      }),
    );
  });

  it("rejects conflicting responses for the same revision locally", async () => {
    mockedHostFetch.mockResolvedValueOnce(jsonResponse({ status: "resolved" }));
    const interactions = createInteractionsNamespace(() => "chat-1");
    const first = interactions.respond({
      interactionId: "interaction-1",
      expectedRevision: 1,
      selectedOptionIds: ["approve_exact"],
    });

    await expect(
      interactions.respond({
        interactionId: "interaction-1",
        expectedRevision: 1,
        selectedOptionIds: ["deny"],
      }),
    ).rejects.toMatchObject({ code: "INTERACTION_COMMAND_CONFLICT" });
    await first;
    expect(mockedHostFetch).toHaveBeenCalledTimes(1);
  });

  it("retries a transport failure with the same idempotency key", async () => {
    mockedHostFetch
      .mockRejectedValueOnce(new TypeError("network unavailable"))
      .mockResolvedValueOnce(jsonResponse({ status: "resolved" }));
    const interactions = createInteractionsNamespace(() => "chat-1");
    const request = {
      interactionId: "interaction-1",
      expectedRevision: 1,
      text: "Use SQLite",
    };

    await expect(interactions.respond(request)).rejects.toThrow(
      "network unavailable",
    );
    await expect(interactions.respond(request)).resolves.toMatchObject({
      status: "resolved",
    });

    const firstBody = JSON.parse(
      String(mockedHostFetch.mock.calls[0][1]?.body),
    );
    const secondBody = JSON.parse(
      String(mockedHostFetch.mock.calls[1][1]?.body),
    );
    expect(secondBody.idempotency_key).toBe(firstBody.idempotency_key);
  });

  it("requires a ChatSpec id before sending a request", async () => {
    const interactions = createInteractionsNamespace(() => null);

    await expect(interactions.list()).rejects.toBeInstanceOf(
      PawInteractionError,
    );
    expect(mockedHostFetch).not.toHaveBeenCalled();
  });
});
