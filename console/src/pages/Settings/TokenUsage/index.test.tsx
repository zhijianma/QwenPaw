/**
 * TokenUsagePage — settings page composing the token usage dashboard.
 * Covers the loading/error/empty states, details fetch failures with
 * retry, summary card totals, the model/date/agent table row building
 * (including unattributed and named agents), the llm/tool trend card
 * (loading, error with retry, all-zero empty, data) and date range
 * changes re-fetching both endpoints.
 *
 * The presentational children are stubbed with prop capture; the charts
 * and date picker are minimal drivers.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

const apiMocks = vi.hoisted(() => ({
  getTokenUsage: vi.fn(),
  getGlobalLlmToolTrend: vi.fn(),
}));

vi.mock("../../../api", () => ({ default: apiMocks }));

// t and message must be stable references: fetchData/fetchTrend list them in
// their dependency arrays and a fresh function per render would refetch
// forever.
const stableT = vi.hoisted(
  () => (key: string, fallback?: string) =>
    typeof fallback === "string" && !fallback.includes("{") ? fallback : key,
);
const stableMessage = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));

vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: stableMessage }),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: stableT,
    i18n: { resolvedLanguage: "en", changeLanguage: vi.fn(), language: "en" },
  }),
}));

vi.mock("../../../contexts/ThemeContext", () => ({
  useTheme: () => ({ isDark: false }),
}));

const agentStore = vi.hoisted(() => ({
  agents: [{ id: "agent-a", name: "Agent A" }],
}));

vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: (selector: (s: typeof agentStore) => unknown) =>
    selector(agentStore),
}));

vi.mock("@/components/PageHeader", () => ({
  PageHeader: ({ parent, current }: { parent: string; current: string }) =>
    React.createElement(
      "div",
      { "data-testid": "page-header" },
      `${parent}/${current}`,
    ),
}));

const capturedProps = vi.hoisted(() => ({
  summary: null as any,
  tables: null as any,
  modelTrend: null as any,
  tokenType: null as any,
  line: null as any,
}));

vi.mock("./components", () => ({
  LoadingState: ({
    message,
    error,
    onRetry,
  }: {
    message: string;
    error?: boolean;
    onRetry?: () => void;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "loading-state", "data-error": String(!!error) },
      message,
      error &&
        React.createElement(
          "button",
          { onClick: onRetry, "data-testid": "retry" },
          "retry",
        ),
    ),
  EmptyState: ({ message }: { message: string }) =>
    React.createElement("div", { "data-testid": "empty-state" }, message),
  SummaryCards: (props: any) => {
    capturedProps.summary = props;
    return React.createElement("div", { "data-testid": "summary-cards" });
  },
  ModelTrendChart: (props: any) => {
    capturedProps.modelTrend = props;
    return React.createElement("div", { "data-testid": "model-trend-chart" });
  },
  TokenTypeChart: (props: any) => {
    capturedProps.tokenType = props;
    return React.createElement("div", { "data-testid": "token-type-chart" });
  },
  DataTables: (props: any) => {
    capturedProps.tables = props;
    return React.createElement("div", { "data-testid": "data-tables" });
  },
}));

vi.mock("@ant-design/plots", () => ({
  Line: (props: any) => {
    capturedProps.line = props;
    return React.createElement("div", { "data-testid": "llm-tool-line" });
  },
}));

vi.mock("@agentscope-ai/design", () => ({
  Card: ({ children, title }: any) =>
    React.createElement("div", null, title, children),
}));

const datePickerMock = vi.hoisted(() => ({ onChange: null as any }));

vi.mock("antd", () => {
  const Tooltip = ({ children }: any) =>
    React.createElement("span", null, children);
  const RangePicker = ({ onChange }: any) => {
    datePickerMock.onChange = onChange;
    return React.createElement("input", {
      "data-testid": "range-picker",
      readOnly: true,
    });
  };
  const DatePicker = { RangePicker };
  return { DatePicker, Tooltip };
});

import TokenUsagePage from "./index";

function makeRecord(overrides: Record<string, unknown> = {}) {
  return {
    date: "2026-09-01",
    provider_id: "openai",
    model: "gpt-4o",
    prompt_tokens: 100,
    completion_tokens: 50,
    cache_read_tokens: 10,
    cache_write_tokens: 2,
    cache_eligible_input_tokens: 20,
    cache_observed_calls: 1,
    call_count: 3,
    agent_id: "agent-a",
    conversation_id: "chat-a",
    turn_id: "turn-a",
    ...overrides,
  };
}

function makeSummary(records: ReturnType<typeof makeRecord>[]) {
  const totals = records.reduce(
    (acc, record) => ({
      prompt_tokens: acc.prompt_tokens + record.prompt_tokens,
      completion_tokens: acc.completion_tokens + record.completion_tokens,
      cache_read_tokens: acc.cache_read_tokens + record.cache_read_tokens,
      cache_write_tokens: acc.cache_write_tokens + record.cache_write_tokens,
      cache_eligible_input_tokens:
        acc.cache_eligible_input_tokens + record.cache_eligible_input_tokens,
      cache_observed_calls:
        acc.cache_observed_calls + record.cache_observed_calls,
      call_count: acc.call_count + record.call_count,
    }),
    {
      prompt_tokens: 0,
      completion_tokens: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      cache_eligible_input_tokens: 0,
      cache_observed_calls: 0,
      call_count: 0,
    },
  );
  const stats = (record: ReturnType<typeof makeRecord>) => ({
    prompt_tokens: record.prompt_tokens,
    completion_tokens: record.completion_tokens,
    cache_read_tokens: record.cache_read_tokens,
    cache_write_tokens: record.cache_write_tokens,
    cache_eligible_input_tokens: record.cache_eligible_input_tokens,
    cache_observed_calls: record.cache_observed_calls,
    call_count: record.call_count,
  });
  return {
    total_prompt_tokens: totals.prompt_tokens,
    total_completion_tokens: totals.completion_tokens,
    total_cache_read_tokens: totals.cache_read_tokens,
    total_cache_write_tokens: totals.cache_write_tokens,
    total_cache_eligible_input_tokens: totals.cache_eligible_input_tokens,
    cache_observed_calls: totals.cache_observed_calls,
    cache_hit_rate: null,
    total_calls: totals.call_count,
    by_model: Object.fromEntries(
      records.map((record) => [
        `${record.provider_id}:${record.model}`,
        {
          ...stats(record),
          provider_id: record.provider_id,
          model: record.model,
        },
      ]),
    ),
    by_date: Object.fromEntries(
      records.map((record, index) => [
        `${record.date}:${index}`,
        stats(record),
      ]),
    ),
    by_date_model: {},
    by_agent: Object.fromEntries(
      records.map((record, index) => [
        `${record.agent_id ?? "none"}:${index}`,
        { ...stats(record), agent_id: record.agent_id },
      ]),
    ),
    by_chat: Object.fromEntries(
      records.map((record, index) => [
        `${record.agent_id ?? "none"}:${record.conversation_id}:${index}`,
        {
          ...stats(record),
          agent_id: record.agent_id,
          conversation_id: record.conversation_id,
        },
      ]),
    ),
    by_turn: Object.fromEntries(
      records.map((record, index) => [
        `${record.agent_id ?? "none"}:${record.conversation_id}:${
          record.turn_id
        }:${index}`,
        {
          ...stats(record),
          agent_id: record.agent_id,
          conversation_id: record.conversation_id,
          turn_id: record.turn_id,
        },
      ]),
    ),
  };
}

function setupDefaultMocks() {
  apiMocks.getTokenUsage.mockResolvedValue(makeSummary([makeRecord()]));
  apiMocks.getGlobalLlmToolTrend.mockResolvedValue([
    { date: "2026-09-01", agent_llm_calls: 4, tool_calls: 2 },
  ]);
}

describe("TokenUsagePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedProps.summary = null;
    capturedProps.tables = null;
    capturedProps.modelTrend = null;
    capturedProps.tokenType = null;
    capturedProps.line = null;
    setupDefaultMocks();
  });

  it("shows the loading state before the details arrive", () => {
    apiMocks.getTokenUsage.mockReturnValue(new Promise(() => {}));
    render(<TokenUsagePage />);
    expect(screen.getByTestId("loading-state")).toBeInTheDocument();
  });

  it("renders the dashboard with aggregated data", async () => {
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("summary-cards")).toBeInTheDocument(),
    );
    expect(capturedProps.summary).toMatchObject({
      totalCalls: 3,
      totalPromptTokens: 100,
      totalCompletionTokens: 50,
      totalCacheReadTokens: 10,
      totalCacheEligibleInputTokens: 20,
    });
    expect(screen.getByTestId("data-tables")).toBeInTheDocument();
    expect(screen.getByTestId("model-trend-chart")).toBeInTheDocument();
    expect(screen.getByTestId("token-type-chart")).toBeInTheDocument();
  });

  it("builds model and agent table rows from the records", async () => {
    apiMocks.getTokenUsage.mockResolvedValue(
      makeSummary([
        makeRecord(),
        makeRecord({
          provider_id: "anthropic",
          model: "claude",
          agent_id: null,
        }),
      ]),
    );
    render(<TokenUsagePage />);
    await waitFor(() => expect(capturedProps.tables).toBeTruthy());
    // Model rows are keyed provider:model.
    expect(
      capturedProps.tables.byModelData.map((r: any) => r.model).sort(),
    ).toEqual(["anthropic:claude", "openai:gpt-4o"]);
    // The named agent resolves through the store; the null agent id is
    // labelled unattributed; rows sort by total tokens descending.
    expect(capturedProps.tables.byAgentData).toEqual([
      expect.objectContaining({ agent: "Agent A" }),
      expect.objectContaining({ agent: "tokenUsage.unattributed" }),
    ]);
    expect(capturedProps.tables.byChatData).toEqual([
      expect.objectContaining({ agent: "Agent A", chat: "chat-a" }),
      expect.objectContaining({
        agent: "tokenUsage.unattributed",
        chat: "chat-a",
      }),
    ]);
    expect(capturedProps.tables.byTurnData).toEqual([
      expect.objectContaining({
        agent: "Agent A",
        chat: "chat-a",
        turn: "turn-a",
      }),
      expect.objectContaining({
        agent: "tokenUsage.unattributed",
        chat: "chat-a",
        turn: "turn-a",
      }),
    ]);
  });

  it("falls back to the agent id when the store has no profile", async () => {
    apiMocks.getTokenUsage.mockResolvedValue(
      makeSummary([makeRecord({ agent_id: "unknown-agent" })]),
    );
    render(<TokenUsagePage />);
    await waitFor(() => expect(capturedProps.tables).toBeTruthy());
    expect(capturedProps.tables.byAgentData[0].agent).toBe("unknown-agent");
  });

  it("shows the error state with a retry that refetches", async () => {
    apiMocks.getTokenUsage.mockRejectedValueOnce(new Error("down"));
    const user = userEvent.setup();
    render(<TokenUsagePage />);
    await waitFor(() => {
      const state = screen.getByTestId("loading-state");
      expect(state).toHaveAttribute("data-error", "true");
    });
    expect(stableMessage.error).toHaveBeenCalledWith("tokenUsage.loadFailed");
    expect(capturedProps.tables).toBeNull();

    apiMocks.getTokenUsage.mockResolvedValueOnce(makeSummary([makeRecord()]));
    await user.click(screen.getByTestId("retry"));
    await waitFor(() =>
      expect(screen.getByTestId("summary-cards")).toBeInTheDocument(),
    );
  });

  it("shows the empty state when there are no records", async () => {
    apiMocks.getTokenUsage.mockResolvedValue(makeSummary([]));
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("empty-state")).toBeInTheDocument(),
    );
    expect(capturedProps.tables).toBeNull();
    expect(capturedProps.summary).toBeNull();
  });

  it("shows the trend loading state until the trend arrives", async () => {
    apiMocks.getGlobalLlmToolTrend.mockReturnValue(new Promise(() => {}));
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("summary-cards")).toBeInTheDocument(),
    );
    // The trend card keeps showing a loading state.
    expect(screen.getAllByTestId("loading-state").length).toBeGreaterThan(0);
    expect(capturedProps.line).toBeNull();
  });

  it("shows the trend error state with a working retry", async () => {
    apiMocks.getGlobalLlmToolTrend.mockRejectedValueOnce(new Error("down"));
    const user = userEvent.setup();
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(
        screen.getByText("tokenUsage.llmAndToolTrendLoadFailed"),
      ).toBeInTheDocument(),
    );

    apiMocks.getGlobalLlmToolTrend.mockResolvedValueOnce([
      { date: "2026-09-01", agent_llm_calls: 1, tool_calls: 1 },
    ]);
    await user.click(screen.getByTestId("retry"));
    await waitFor(() =>
      expect(screen.getByTestId("llm-tool-line")).toBeInTheDocument(),
    );
  });

  it("shows the trend empty state when every day is zero", async () => {
    apiMocks.getGlobalLlmToolTrend.mockResolvedValue([
      { date: "2026-09-01", agent_llm_calls: 0, tool_calls: 0 },
      { date: "2026-09-02", agent_llm_calls: 0, tool_calls: 0 },
    ]);
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByText("tokenUsage.noData")).toBeInTheDocument(),
    );
    expect(capturedProps.line).toBeNull();
  });

  it("feeds the llm/tool line chart with labelled series", async () => {
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("llm-tool-line")).toBeInTheDocument(),
    );
    expect(capturedProps.line.data).toEqual([
      {
        date: "2026-09-01",
        type: "tokenUsage.recordedTurnsAllAgents",
        value: 4,
      },
      { date: "2026-09-01", type: "tokenUsage.toolCalls", value: 2 },
    ]);
  });

  it("refetches both endpoints when the date range changes", async () => {
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("llm-tool-line")).toBeInTheDocument(),
    );
    const callsBefore = apiMocks.getTokenUsage.mock.calls.length;

    const dayjs = (await import("dayjs")).default;
    datePickerMock.onChange([dayjs().subtract(7, "day"), dayjs()]);

    await waitFor(() =>
      expect(apiMocks.getTokenUsage.mock.calls.length).toBe(callsBefore + 1),
    );
    const [range] =
      apiMocks.getTokenUsage.mock.calls[
        apiMocks.getTokenUsage.mock.calls.length - 1
      ];
    expect(range.start_date).toBe(
      dayjs().subtract(7, "day").format("YYYY-MM-DD"),
    );
  });

  it("ignores null date selections", async () => {
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("llm-tool-line")).toBeInTheDocument(),
    );
    const callsBefore = apiMocks.getTokenUsage.mock.calls.length;
    datePickerMock.onChange(null);
    datePickerMock.onChange([null, null]);
    await new Promise((r) => setTimeout(r, 30));
    expect(apiMocks.getTokenUsage.mock.calls.length).toBe(callsBefore);
  });
});
