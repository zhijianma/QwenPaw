import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { chatApi } from "../../../api/modules/chat";
import { streamRuntimeProjection } from "../../../api/runtimeProjectionStream";
import type {
  ConversationRuntimeProjection,
  TurnSubmission,
} from "../../../api/types";
import ServerRuntimeQueue from "./ServerRuntimeQueue";

vi.mock("../../../api/modules/chat", () => ({
  chatApi: {
    cancelQueued: vi.fn(),
    getRuntime: vi.fn(),
    reorderQueue: vi.fn(),
  },
}));

vi.mock("../../../api/runtimeProjectionStream", () => ({
  streamRuntimeProjection: vi.fn(),
}));

function submission(id: string, position: number): TurnSubmission {
  return {
    agent_id: "default",
    conversation_id: "chat-1",
    priority: 20,
    content: `Message ${id}`,
    artifact_refs: [],
    request_context: {},
    input_envelope: null,
    idempotency_key: `key-${id}`,
    correlation_id: `correlation-${id}`,
    submission_id: id,
    sequence: position,
    queue_position: position,
    invocation_id: null,
    status: "queued",
    revision: 3,
    created_at: "2026-09-28T00:00:00Z",
    updated_at: "2026-09-28T00:00:00Z",
  };
}

function projection(
  submissions: TurnSubmission[],
  revision = 3,
): ConversationRuntimeProjection {
  return {
    agent_id: "default",
    conversation_id: "chat-1",
    queue: {
      agent_id: "default",
      conversation_id: "chat-1",
      revision,
      active_submission_id: null,
      submissions,
      updated_at: "2026-09-28T00:00:00Z",
    },
    interactions: [],
    cursor: `v1-${revision}`,
    observed_at: "2026-09-28T00:00:00Z",
  };
}

describe("ServerRuntimeQueue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("crypto", { randomUUID: () => "command-key" });
    vi.mocked(streamRuntimeProjection).mockImplementation(async (options) => {
      options.onSnapshot(
        projection([
          submission("submission-2", 2),
          submission("submission-1", 1),
        ]),
      );
      await new Promise<void>((resolve) => {
        options.signal?.addEventListener("abort", () => resolve(), {
          once: true,
        });
      });
      return "v1-3";
    });
    vi.mocked(chatApi.cancelQueued).mockResolvedValue({
      receipt_id: "receipt-1",
      command_id: "command-1",
      submission_id: "submission-1",
      kind: "cancel_queued",
      status: "applied",
      agent_id: "default",
      conversation_id: "chat-1",
      revision: 4,
      detail: "",
      recorded_at: "2026-09-28T00:00:01Z",
    });
    vi.mocked(chatApi.reorderQueue).mockResolvedValue({
      receipt_id: "receipt-2",
      command_id: "command-2",
      submission_id: null,
      kind: "reorder",
      status: "applied",
      agent_id: "default",
      conversation_id: "chat-1",
      revision: 4,
      detail: "",
      recorded_at: "2026-09-28T00:00:01Z",
    });
    vi.mocked(chatApi.getRuntime).mockResolvedValue(
      projection([submission("submission-2", 1)], 4),
    );
  });

  it("renders the server order and cancels using its revision", async () => {
    const user = userEvent.setup();
    render(<ServerRuntimeQueue active agentId="default" chatId="chat-1" />);

    const messages = await screen.findAllByText(/Message submission-/);
    expect(messages.map((node) => node.textContent)).toEqual([
      "Message submission-1",
      "Message submission-2",
    ]);
    await user.click(screen.getAllByRole("button", { name: /delete/i })[0]);

    await waitFor(() => {
      expect(chatApi.cancelQueued).toHaveBeenCalledWith(
        "chat-1",
        "submission-1",
        {
          idempotency_key: "command-key",
          expected_revision: 3,
        },
        "default",
      );
    });
  });

  it("refreshes history only after the runtime becomes empty", async () => {
    const onSettled = vi.fn();
    vi.mocked(streamRuntimeProjection).mockImplementation(async (options) => {
      const active = submission("submission-1", 1);
      active.status = "running";
      active.invocation_id = "invocation-1";
      options.onSnapshot(projection([active], 4));
      options.onSnapshot(projection([], 5));
      await new Promise<void>((resolve) => {
        options.signal?.addEventListener("abort", () => resolve(), {
          once: true,
        });
      });
      return "v1-5";
    });

    render(
      <ServerRuntimeQueue
        active
        agentId="default"
        chatId="chat-1"
        onSettled={onSettled}
      />,
    );

    await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1));
  });

  it("reorders the complete server queue with its observed revision", async () => {
    render(<ServerRuntimeQueue active agentId="default" chatId="chat-1" />);

    const first = await screen.findByText("Message submission-1");
    const second = screen.getByText("Message submission-2");
    const firstRow = first.closest('[draggable="true"]');
    const secondRow = second.closest('[draggable="true"]');
    expect(firstRow).not.toBeNull();
    expect(secondRow).not.toBeNull();
    fireEvent.dragStart(firstRow as Element);
    fireEvent.dragOver(secondRow as Element);
    fireEvent.drop(secondRow as Element);

    await waitFor(() => {
      expect(chatApi.reorderQueue).toHaveBeenCalledWith(
        "chat-1",
        {
          idempotency_key: "command-key",
          expected_revision: 3,
          ordered_submission_ids: ["submission-2", "submission-1"],
        },
        "default",
      );
    });
  });
});
