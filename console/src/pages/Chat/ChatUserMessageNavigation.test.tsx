// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IAgentScopeRuntimeWebUIMessage } from "@agentscope-ai/chat";

const state = vi.hoisted(() => ({
  messages: [] as IAgentScopeRuntimeWebUIMessage[],
  load: vi.fn(),
}));
vi.mock("use-context-selector", () => ({
  useContextSelector: (
    _context: unknown,
    select: (value: unknown) => unknown,
  ) => select({ messages: state.messages }),
}));
vi.mock(
  "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereMessagesContext",
  () => ({
    ChatAnywhereMessagesContext: {},
  }),
);
vi.mock("./sessionApi", () => ({
  default: { loadUserMessageIndex: state.load },
}));
vi.mock("./HistoryMessageWindow", () => ({
  HistoryNavigationDirectory: () => null,
}));
vi.mock("antd", () => ({
  Tooltip: ({ children }: { children: React.ReactNode }) => children,
  Popover: ({ children }: { children: React.ReactNode }) => children,
}));
import { ChatUserMessageNavigation } from "./ChatUserMessageNavigation";
import { historyMessageWindow } from "./historyMessageWindowStore";

afterEach(() => {
  cleanup();
  historyMessageWindow.end();
  state.load.mockReset();
  state.messages = [];
});

describe("unified navigation across history and live rendering", () => {
  it("renders a new chat before a session identity exists", () => {
    render(
      <ChatUserMessageNavigation onNavigate={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(state.load).not.toHaveBeenCalled();
  });

  it("loads following context without changing the navigation target", async () => {
    const entries = Array.from({ length: 5 }, (_, i) => ({
      messageId: `user-${i}`,
      preview: "input",
      createdAt: "",
      anchor: `cursor-${i}`,
    }));
    state.load.mockResolvedValue(entries);
    const onNavigate = vi.fn();
    render(
      <div className="qwenpaw-chat-anywhere-layout-right">
        <ChatUserMessageNavigation
          sessionId="chat"
          onNavigate={onNavigate}
          onCancel={vi.fn()}
        />
      </div>,
    );
    const previous = await screen.findByRole("button", {
      name: "Previous user message",
    });
    fireEvent.click(previous);
    expect(onNavigate).toHaveBeenCalledWith({
      ...entries[3],
      anchor: "cursor-4",
    });
  });

  it("retains the full count, adds live input, and does not refetch on assistant deltas", async () => {
    const entries = Array.from({ length: 17 }, (_, i) => ({
      messageId: `user-${i}`,
      preview: "input",
      createdAt: "",
      anchor: `cursor-${i}`,
    }));
    state.load.mockResolvedValue(entries);
    const props = { sessionId: "chat", onNavigate: vi.fn(), onCancel: vi.fn() };
    const view = () => (
      <div className="qwenpaw-chat-anywhere-layout-right">
        <ChatUserMessageNavigation {...props} />
      </div>
    );
    const { rerender } = render(view());
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "User message directory" })
          .textContent,
      ).toBe("17"),
    );
    state.messages = [{ id: "live-user", role: "user", cards: [] }];
    rerender(view());
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "User message directory" })
          .textContent,
      ).toBe("18"),
    );
    const calls = state.load.mock.calls.length;
    state.messages = [
      ...state.messages,
      { id: "assistant", role: "assistant", cards: [] },
    ];
    rerender(view());
    expect(state.load).toHaveBeenCalledTimes(calls);
    expect(screen.getAllByRole("navigation")).toHaveLength(1);
    act(() => historyMessageWindow.end("chat"));
    expect(
      screen.getByRole("button", { name: "User message directory" })
        .textContent,
    ).toBe("18");
  });
});
