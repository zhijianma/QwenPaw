import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import ComposedProvider from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/ChatAnywhere/ComposedProvider";
import {
  AgentScopeRuntimeMessageType,
  AgentScopeRuntimeRunStatus,
} from "@agentscope-ai/chat";
import { HostRequestCard, HostResponseCard } from "./HostBubbles";
import { ChatRegenerateContext } from "./ChatRegenerateContext";
import { ChatScalar, ChatList } from "../../plugins/registry/slotKeys";
import {
  setAssistantMessageDisplayPreference,
  setShowThinkingPreference,
} from "../../utils/chatDisplayPreference";

const extensions = vi.hoisted(() => ({
  scalar: {} as Record<string, unknown>,
  lists: {} as Record<string, unknown[]>,
}));
vi.mock("../../plugins/registry/useChatExtensions", () => ({
  useChatScalarSnapshot: () => extensions.scalar,
  useChatListSnapshot: () =>
    new Proxy(extensions.lists, {
      get: (target, key: string) => target[key] ?? [],
    }),
}));
vi.mock("../../components/RenderableCodeBlock", () => ({
  renderableCodeComponents: {},
}));
vi.mock("../../features/files-workspace/ResponseArtifactList", () => ({
  default: () => null,
}));
vi.mock("../../components/Chat/MediaDownload", () => ({
  DownloadableAudios: () => null,
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
// Keep the real vendor Request/Card and SDK contexts. Only visual leaves are
// stubbed: the shared test design alias omits vendor ConfigProvider internals.
vi.mock("@agentscope-ai/icons", async (importOriginal) => {
  const icons = await importOriginal<Record<string, unknown>>();
  const placeholder = () => <span />;
  return new Proxy(icons, {
    has: (target, key) =>
      Reflect.has(target, key) || String(key).startsWith("Spark"),
    get: (target, key) =>
      Reflect.get(target, key) ??
      (String(key).startsWith("Spark") ? placeholder : undefined),
  });
});
vi.mock("@agentscope-ai/chat/lib/Bubble", () => ({
  default: Object.assign(
    ({ cards }: { cards?: Array<{ data: { content?: string } }> }) => (
      <div>
        {cards?.map((card, index) => (
          <span key={index}>{card.data.content}</span>
        ))}
      </div>
    ),
    {
      Spin: () => <span>loading</span>,
      Interrupted: () => <span>interrupted</span>,
    },
  ),
}));
vi.mock(
  "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Request/Actions",
  () => ({ default: () => <span>request-actions</span> }),
);
vi.mock(
  "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Reasoning",
  () => ({ default: () => <span>reasoning content</span> }),
);
vi.mock(
  "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Tool",
  () => ({ default: () => <span>tool content</span> }),
);
vi.mock(
  "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/AgentScopeRuntime/Response/Actions",
  () => ({
    default: ({ messageId }: { messageId: string }) => (
      <span data-testid="response-actions">{messageId}</span>
    ),
  }),
);
vi.mock("./LazyAccordion", () => ({
  default: ({
    renderChildren,
    defaultOpen,
  }: {
    renderChildren: () => ReactNode;
    defaultOpen: boolean;
  }) => (
    <section data-testid="steps" data-open={String(defaultOpen)}>
      {renderChildren()}
    </section>
  ),
}));

function provider(children: ReactNode) {
  return (
    <ComposedProvider
      options={{
        api: {},
        session: {
          currentSessionId: undefined,
          api: {
            getSessionList: async () => [],
            getSession: async (id) => ({ id, name: id, messages: [] }),
            createSession: async (draft) => {
              const session = {
                id: "fixture-session",
                name: draft.name || "",
                messages: [],
              };
              return { sessions: [session], session };
            },
            updateSession: async () => [],
            removeSession: async () => [],
          },
        },
      }}
      cards={{}}
    >
      {children}
    </ComposedProvider>
  );
}

beforeEach(() => {
  extensions.scalar = {};
  extensions.lists = {};
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("merged host bubbles behavior", () => {
  it("reacts to thinking/display preferences while retaining response identity and regenerate", async () => {
    const regenerate = vi.fn();
    setAssistantMessageDisplayPreference("expanded");
    const output = [
      {
        id: "reason",
        type: AgentScopeRuntimeMessageType.REASONING,
        role: "assistant",
        status: AgentScopeRuntimeRunStatus.Completed,
        content: [],
      },
      {
        id: "tool",
        type: AgentScopeRuntimeMessageType.FUNCTION_CALL,
        role: "assistant",
        status: AgentScopeRuntimeRunStatus.Completed,
        content: [],
      },
    ];
    render(
      provider(
        <ChatRegenerateContext.Provider value={regenerate}>
          <HostResponseCard
            id="sdk-message-id"
            data={{
              id: "runtime-response-id",
              status: AgentScopeRuntimeRunStatus.Completed,
              output,
            }}
            isLast
          />
        </ChatRegenerateContext.Provider>,
      ),
    );
    expect(screen.getByText("reasoning content")).toBeInTheDocument();
    expect(screen.getByText("tool content")).toBeInTheDocument();
    expect(screen.queryByTestId("steps")).not.toBeInTheDocument();
    await act(async () => {
      setShowThinkingPreference(false);
    });
    expect(screen.queryByText("reasoning content")).not.toBeInTheDocument();
    await act(async () => {
      setAssistantMessageDisplayPreference("process-collapsed");
    });
    expect(screen.getByTestId("steps")).toHaveAttribute("data-open", "false");
    expect(screen.getByTestId("response-actions")).toHaveTextContent(
      "sdk-message-id",
    );
    fireEvent.click(screen.getByRole("button", { name: "chat.regenerate" }));
    expect(regenerate).toHaveBeenCalledWith("sdk-message-id");
  });

  it("renders usage owned by the completed response turn", () => {
    render(
      provider(
        <HostResponseCard
          id="usage-message"
          data={{
            id: "usage-response",
            status: AgentScopeRuntimeRunStatus.Completed,
            output: [],
            usage: {
              prompt_tokens: 34_179,
              completion_tokens: 382,
              total_tokens: 34_561,
              provider_id: "dashscope",
              model_name: "qwen3.8-max",
              measurement: "provider_reported",
            },
          }}
        />,
      ),
    );

    const summary = screen.getByTestId("turn-usage-summary");
    expect(summary).toHaveTextContent("chat.turnUsagePopover.turn");
    expect(summary).toHaveTextContent("34.6K");
    expect(summary).toHaveTextContent("dashscope/qwen3.8-max");
  });

  it("does not render an empty usage summary for legacy responses", () => {
    render(
      provider(
        <HostResponseCard
          id="legacy-message"
          data={{
            id: "legacy-response",
            status: AgentScopeRuntimeRunStatus.Completed,
            output: [],
          }}
        />,
      ),
    );

    expect(screen.queryByTestId("turn-usage-summary")).toBeNull();
  });

  it("renders unavailable usage without presenting it as zero tokens", () => {
    render(
      provider(
        <HostResponseCard
          id="unobserved-usage-message"
          data={{
            id: "unobserved-usage-response",
            status: AgentScopeRuntimeRunStatus.Completed,
            output: [],
            usage: {
              provider_id: "openai",
              model_name: "gpt-4o",
              total_tokens: 0,
              measurement: "unavailable",
              usage_unobserved_calls: 1,
            },
          }}
        />,
      ),
    );

    const summary = screen.getByTestId("turn-usage-summary");
    expect(summary).toHaveTextContent("chat.turnUsagePopover.unavailable");
    expect(summary).toHaveTextContent("openai/gpt-4o");
    expect(summary).not.toHaveTextContent("0 chat.turnUsagePopover.tok");
  });

  it("keeps a provider-reported zero distinct from unavailable usage", () => {
    render(
      provider(
        <HostResponseCard
          id="zero-usage-message"
          data={{
            id: "zero-usage-response",
            status: AgentScopeRuntimeRunStatus.Completed,
            output: [],
            usage: {
              provider_id: "local",
              model_name: "zero-model",
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: 0,
              measurement: "provider_reported",
            },
          }}
        />,
      ),
    );

    const summary = screen.getByTestId("turn-usage-summary");
    expect(summary).toHaveTextContent("0 chat.turnUsagePopover.tok");
    expect(summary).not.toHaveTextContent("chat.turnUsagePopover.unavailable");
  });

  it("renders every model route for a fallback turn", () => {
    render(
      provider(
        <HostResponseCard
          id="fallback-usage-message"
          data={{
            id: "fallback-usage-response",
            status: AgentScopeRuntimeRunStatus.Completed,
            output: [],
            usage: {
              prompt_tokens: 120,
              completion_tokens: 15,
              total_tokens: 135,
              provider_id: "anthropic",
              model_name: "claude-sonnet",
              measurement: "provider_reported",
              model_routes: [
                {
                  provider_id: "openai",
                  model_name: "gpt-4",
                  prompt_tokens: 100,
                  completion_tokens: 10,
                  total_tokens: 110,
                  call_count: 1,
                },
                {
                  provider_id: "anthropic",
                  model_name: "claude-sonnet",
                  prompt_tokens: 20,
                  completion_tokens: 5,
                  total_tokens: 25,
                  call_count: 1,
                },
              ],
            },
          }}
        />,
      ),
    );

    const summary = screen.getByTestId("turn-usage-summary");
    expect(summary).toHaveTextContent("openai/gpt-4 · 110");
    expect(summary).toHaveTextContent("anthropic/claude-sonnet · 25");
  });

  it("retains the original request card's content and ordered prepend/append fallback", () => {
    extensions.lists[ChatList.requestPrepend] = [
      {
        pluginId: "test",
        item: {
          id: "late",
          order: 20,
          render: () => <span>prepend-late</span>,
        },
      },
      {
        pluginId: "test",
        item: {
          id: "early",
          order: 10,
          render: () => <span>prepend-early</span>,
        },
      },
    ];
    extensions.lists[ChatList.requestAppend] = [
      {
        pluginId: "test",
        item: { id: "append", render: () => <span>append-content</span> },
      },
    ];
    extensions.scalar[ChatScalar.requestRender] = {
      pluginId: "test",
      value: ({ fallback }: { fallback: () => ReactNode }) => (
        <div data-testid="plugin-request">{fallback()}</div>
      ),
    };
    const { container } = render(
      provider(
        <HostRequestCard
          data={{
            input: [
              {
                role: "user",
                content: [{ type: "text", text: "original request content" }],
              },
            ],
          }}
        />,
      ),
    );
    expect(screen.getByTestId("plugin-request")).toBeInTheDocument();
    expect(container.textContent).toMatch(
      /prepend-early[\s\S]*prepend-late[\s\S]*original request content[\s\S]*append-content[\s\S]*request-actions/,
    );
  });
});
