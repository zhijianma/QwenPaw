import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { chatApi } from "../../api/modules/chat";
import { streamRuntimeProjection } from "../../api/runtimeProjectionStream";
import type { ConversationRuntimeProjection } from "../../api/types";
import {
  resetRuntimeProjectionStoreForTests,
  useConversationRuntimeProjection,
} from "./runtimeProjectionStore";

vi.mock("../../api/modules/chat", () => ({
  chatApi: { getRuntime: vi.fn() },
}));

vi.mock("../../api/runtimeProjectionStream", () => ({
  streamRuntimeProjection: vi.fn(),
}));

const projection: ConversationRuntimeProjection = {
  agent_id: "default",
  conversation_id: "chat-1",
  queue: {
    agent_id: "default",
    conversation_id: "chat-1",
    revision: 4,
    active_submission_id: null,
    submissions: [],
    updated_at: "2026-09-28T00:00:00Z",
  },
  interactions: [],
  cursor: "v1-4-test",
  observed_at: "2026-09-28T00:00:00Z",
};

function Probe({ name }: { name: string }) {
  const { projection: current } = useConversationRuntimeProjection({
    active: true,
    agentId: "default",
    chatId: "chat-1",
  });
  return <output data-testid={name}>{current?.cursor ?? "empty"}</output>;
}

describe("runtimeProjectionStore", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(chatApi.getRuntime).mockResolvedValue(projection);
    vi.mocked(streamRuntimeProjection).mockImplementation(async (options) => {
      options.onSnapshot(projection);
      await new Promise<void>((resolve) => {
        options.signal?.addEventListener("abort", () => resolve(), {
          once: true,
        });
      });
      return projection.cursor;
    });
  });

  afterEach(() => resetRuntimeProjectionStoreForTests());

  it("shares one Runtime stream across queue and interaction consumers", async () => {
    render(
      <>
        <Probe name="queue" />
        <Probe name="interactions" />
      </>,
    );

    expect(await screen.findByTestId("queue")).toHaveTextContent(
      projection.cursor,
    );
    expect(screen.getByTestId("interactions")).toHaveTextContent(
      projection.cursor,
    );
    await waitFor(() =>
      expect(streamRuntimeProjection).toHaveBeenCalledTimes(1),
    );
  });
});
