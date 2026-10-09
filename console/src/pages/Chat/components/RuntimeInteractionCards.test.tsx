import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { chatApi } from "../../../api/modules/chat";
import { streamRuntimeProjection } from "../../../api/runtimeProjectionStream";
import type { ChatInteraction } from "../../../api/types";
import RuntimeInteractionCards from "./RuntimeInteractionCards";

vi.mock("../../../api/modules/chat", () => ({
  chatApi: {
    getRuntime: vi.fn(),
    respondToInteraction: vi.fn(),
  },
}));

vi.mock("../../../api/runtimeProjectionStream", () => ({
  streamRuntimeProjection: vi.fn(),
}));

const interaction: ChatInteraction = {
  interaction_id: "interaction-1",
  kind: "user_input",
  mode: "blocking",
  agent_id: "default",
  chat_id: "chat-1",
  invocation_id: "00000000-0000-0000-0000-000000000401",
  correlation_id: "00000000-0000-0000-0000-000000000402",
  title: "Choose output",
  prompt: "Which format should be generated?",
  options: [
    {
      option_id: "md",
      label: "Markdown",
      description: "",
      value: {},
    },
  ],
  response_schema: {},
  metadata: {},
  status: "open",
  revision: 1,
  created_at: "2026-09-28T00:00:00Z",
};

const runtimeProjection = (interactions: ChatInteraction[]) => ({
  agent_id: "default",
  chat_id: "chat-1",
  queue: {
    agent_id: "default",
    chat_id: "chat-1",
    revision: 0,
    active_submission_id: null,
    submissions: [],
    updated_at: "2026-09-28T00:00:00Z",
  },
  interactions,
  cursor: "v1-0-test",
  observed_at: "2026-09-28T00:00:00Z",
});

describe("RuntimeInteractionCards", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(streamRuntimeProjection).mockImplementation(async (options) => {
      options.onSnapshot(runtimeProjection([interaction]));
      await new Promise<void>((resolve) => {
        options.signal?.addEventListener("abort", () => resolve(), {
          once: true,
        });
      });
      return "v1-0-test";
    });
    vi.mocked(chatApi.getRuntime).mockResolvedValue(runtimeProjection([]));
    vi.mocked(chatApi.respondToInteraction).mockResolvedValue({
      interaction_id: interaction.interaction_id,
      status: "resolved",
      revision: 2,
      response: {
        selected_option_ids: ["md"],
        text: "",
        values: {},
      },
      detail: "",
      resolved_at: "2026-09-28T00:00:01Z",
    });
  });

  it("renders a server projection and submits its revision", async () => {
    const user = userEvent.setup();
    render(
      <RuntimeInteractionCards
        active
        agentId="default"
        chatId="chat-1"
        hasLegacyApprovals={false}
        hiddenInteractionIds={new Set()}
      />,
    );

    expect(
      await screen.findByText("Which format should be generated?"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Markdown" }));

    await waitFor(() => {
      expect(chatApi.respondToInteraction).toHaveBeenCalledWith(
        "chat-1",
        "interaction-1",
        expect.objectContaining({
          expected_revision: 1,
          selected_option_ids: ["md"],
        }),
      );
    });
  });

  it("hides an approval already rendered by the legacy projection", async () => {
    render(
      <RuntimeInteractionCards
        active
        agentId="default"
        chatId="chat-1"
        hasLegacyApprovals
        hiddenInteractionIds={new Set(["interaction-1"])}
      />,
    );

    await waitFor(() => {
      expect(streamRuntimeProjection).toHaveBeenCalledWith(
        expect.objectContaining({
          chatId: "chat-1",
          agentId: "default",
        }),
      );
    });
    expect(
      screen.queryByText("Which format should be generated?"),
    ).not.toBeInTheDocument();
  });

  it("keeps a non-blocking suggestion actionable after delivery", async () => {
    const user = userEvent.setup();
    const suggestion: ChatInteraction = {
      ...interaction,
      interaction_id: "suggestion-1",
      kind: "suggestion",
      mode: "non_blocking",
      title: "Suggested next step",
      prompt: "Add a regression test.",
      options: [],
    };
    vi.mocked(streamRuntimeProjection).mockImplementation(async (options) => {
      options.onSnapshot(runtimeProjection([suggestion]));
      await new Promise<void>((resolve) => {
        options.signal?.addEventListener("abort", () => resolve(), {
          once: true,
        });
      });
      return "v1-0-suggestion";
    });

    render(
      <RuntimeInteractionCards
        active
        agentId="default"
        chatId="chat-1"
        hasLegacyApprovals={false}
        hiddenInteractionIds={new Set()}
      />,
    );

    expect(await screen.findByText("Add a regression test.")).toBeVisible();
    await user.click(screen.getByRole("button"));

    await waitFor(() => {
      expect(chatApi.respondToInteraction).toHaveBeenCalledWith(
        "chat-1",
        "suggestion-1",
        expect.objectContaining({
          expected_revision: 1,
          values: { dismissed: true },
        }),
      );
    });
  });
});
