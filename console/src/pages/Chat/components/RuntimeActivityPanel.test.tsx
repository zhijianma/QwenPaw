import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { streamRuntimeProjection } from "../../../api/runtimeProjectionStream";
import type {
  ConversationExecutionChain,
  ConversationRuntimeProjection,
  RuntimeObservation,
} from "../../../api/types";
import { resetRuntimeProjectionStoreForTests } from "../runtimeProjectionStore";
import RuntimeActivityPanel from "./RuntimeActivityPanel";

vi.mock("../../../api/modules/chat", () => ({
  chatApi: { getRuntime: vi.fn() },
}));

vi.mock("../../../api/runtimeProjectionStream", () => ({
  streamRuntimeProjection: vi.fn(),
}));

function chain(
  state: ConversationExecutionChain["state"] = "running",
): ConversationExecutionChain {
  return {
    chat_id: "chat-1",
    correlation_id: "correlation-1",
    state,
    submission_ids: ["submission-1"],
    invocation_ids: ["invocation-1"],
    head_submission_id: "submission-1",
    head_invocation_id: "invocation-1",
    latest_submission_status: "running",
    open_interaction_ids: [],
    outcome: null,
    accepted_at: "2026-10-08T00:00:00Z",
    latest_submission_at: "2026-10-08T00:00:00Z",
  };
}

function observation(updates: Partial<RuntimeObservation>): RuntimeObservation {
  return {
    observation_id: crypto.randomUUID(),
    category: "action",
    stage: "execution",
    status: "running",
    source: {
      source_type: "qwenpaw.action.request",
      source_id: "00000000-0000-0000-0000-000000000123",
    },
    chat_id: "chat-1",
    invocation_id: "invocation-1",
    correlation_id: "correlation-1",
    registry_generation: 4,
    title: "Action running",
    facts: {},
    occurred_at: "2026-10-08T00:00:01Z",
    ...updates,
  };
}

function projection(
  items: RuntimeObservation[],
  executionChains: ConversationExecutionChain[] = [chain()],
): ConversationRuntimeProjection {
  return {
    agent_id: "default",
    chat_id: "chat-1",
    queue: {
      agent_id: "default",
      chat_id: "chat-1",
      revision: 1,
      active_submission_id: "submission-1",
      submissions: [],
      updated_at: "2026-10-08T00:00:01Z",
    },
    interactions: [],
    execution_chains: executionChains,
    execution_window_truncated: false,
    activity: { items, next_cursor: null },
    cursor: "v3-activity",
    observed_at: "2026-10-08T00:00:01Z",
  };
}

function stream(snapshot: ConversationRuntimeProjection) {
  vi.mocked(streamRuntimeProjection).mockImplementation(async (options) => {
    options.onSnapshot(snapshot);
    await new Promise<void>((resolve) => {
      options.signal?.addEventListener("abort", () => resolve(), {
        once: true,
      });
    });
    return snapshot.cursor;
  });
}

describe("RuntimeActivityPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    resetRuntimeProjectionStoreForTests();
  });

  it("keeps an ordinary one-answer model exchange visually quiet", async () => {
    stream(
      projection([
        observation({
          observation_id: "observation-submission-accepted",
          category: "control",
          status: "recorded",
          source: {
            source_type: "qwenpaw.control.submission",
            source_id: "submission-1",
          },
          title: "Submission accepted",
        }),
        observation({
          category: "model",
          status: "succeeded",
          source: {
            source_type: "qwenpaw.model.result",
            source_id: "model-result-1",
          },
          title: "Model request completed",
        }),
        observation({
          observation_id: "observation-submission-completed",
          category: "control",
          status: "succeeded",
          source: {
            source_type: "qwenpaw.control.submission",
            source_id: "submission-1",
          },
          title: "Submission completed",
        }),
      ]),
    );

    render(<RuntimeActivityPanel active agentId="default" chatId="chat-1" />);

    await waitFor(() => expect(streamRuntimeProjection).toHaveBeenCalled());
    expect(
      screen.queryByText("Model request completed"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("chat.activity.title"),
    ).not.toBeInTheDocument();
  });

  it("shows only semantic activity for the latest execution chain", async () => {
    stream(
      projection([
        observation({
          observation_id: "observation-action",
          title: "Workspace file updated",
          status: "succeeded",
        }),
        observation({
          observation_id: "observation-model",
          category: "model",
          source: {
            source_type: "qwenpaw.model.route-decision",
            source_id: "route-1",
          },
          title: "Model route selected",
        }),
        observation({
          observation_id: "observation-foreign",
          correlation_id: "correlation-2",
          title: "Foreign action",
        }),
      ]),
    );

    render(<RuntimeActivityPanel active agentId="default" chatId="chat-1" />);

    expect(await screen.findByText("Workspace file updated")).toBeVisible();
    expect(screen.queryByText("Model route selected")).not.toBeInTheDocument();
    expect(screen.queryByText("Foreign action")).not.toBeInTheDocument();
    expect(screen.getByText(/qwenpaw\.action\.request/)).toBeVisible();
  });

  it("shows model recovery and supports presentation-only collapse", async () => {
    const user = userEvent.setup();
    stream(
      projection([
        observation({
          observation_id: "observation-wait",
          category: "model",
          status: "pending",
          source: {
            source_type: "qwenpaw.model.resource-wait",
            source_id: "resource-wait-1",
          },
          title: "Model resource recovery",
        }),
      ]),
    );

    render(<RuntimeActivityPanel active agentId="default" chatId="chat-1" />);

    expect(await screen.findByText("Model resource recovery")).toBeVisible();
    const header = screen.getByRole("button", {
      name: /chat\.activity\.title/,
    });
    expect(header).toHaveAttribute("aria-expanded", "true");

    await user.click(header);

    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByText("Model resource recovery"),
    ).not.toBeInTheDocument();
  });
});
