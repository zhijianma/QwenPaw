// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useMessageQueueStore } from "../../../stores/messageQueueStore";
import ChatSenderTabsPanel from "./ChatSenderTabsPanel";

const backgroundState = vi.hoisted(() => ({
  tasks: [] as Record<string, unknown>[],
  removeTasks: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

vi.mock("../../../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

vi.mock("../../../stores/backgroundTasksStore", () => ({
  useBackgroundTasksStore: (
    selector: (state: typeof backgroundState) => unknown,
  ) => selector(backgroundState),
  selectTasksForSession: () => [],
}));

vi.mock("../../../hooks/useBackgroundTaskWatcher", () => ({
  cancelBackgroundTask: vi.fn(),
  stopBackgroundTaskWatcher: vi.fn(),
}));

const handlers = {
  onRemove: vi.fn(),
  onEdit: vi.fn(),
  onReorder: vi.fn(),
  onInterruptAndSend: vi.fn(),
  onClear: vi.fn(),
  onPauseResume: vi.fn(),
  onRetry: vi.fn(),
  onSkip: vi.fn(),
};

beforeEach(() => {
  useMessageQueueStore.setState({
    queues: {
      "chat-1": [
        {
          id: "legacy-1",
          text: "stale local queue turn",
          agentId: "default",
          status: "pending",
          retryCount: 0,
          createdAt: 1,
        },
      ],
    },
    runStates: { "chat-1": "paused" },
    currentSendingId: null,
    lastMigratedTo: null,
  });
});

describe("ChatSenderTabsPanel queue authority", () => {
  it("hides a stale legacy queue for a server-owned Chat", () => {
    const { container } = render(
      <ChatSenderTabsPanel
        bgSessionId="chat-1"
        queueSessionId="chat-1"
        legacyQueueEnabled={false}
        {...handlers}
      />,
    );

    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("stale local queue turn")).toBeNull();
  });

  it("keeps the legacy queue available for an external backend", () => {
    render(
      <ChatSenderTabsPanel
        bgSessionId="chat-1"
        queueSessionId="chat-1"
        legacyQueueEnabled
        {...handlers}
      />,
    );

    expect(screen.getByText("stale local queue turn")).toBeInTheDocument();
  });
});
