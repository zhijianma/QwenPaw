/**
 * TokenUsagePage — settings page composing the token usage dashboard.
 * Covers the loading/error/empty states, details fetch failures with
 * retry, summary card totals, model/date table row building and resource
 * filters. Agent ownership belongs to Agent Statistics; turn ownership belongs
 * to the Chat page.
 *
 * The presentational children are stubbed with prop capture; the charts
 * and date picker are minimal drivers.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import type { TokenUsageSummary } from "../../../api/types/tokenUsage";
import userEvent from "@testing-library/user-event";
import React from "react";

const apiMocks = vi.hoisted(() => ({
  getTokenUsage: vi.fn(),
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
  const Select = ({
    "aria-label": ariaLabel,
    value,
    options = [],
    onChange,
  }: any) =>
    React.createElement(
      "select",
      {
        "aria-label": ariaLabel,
        value: value ?? "",
        onChange: (event: React.ChangeEvent<HTMLSelectElement>) =>
          onChange(event.target.value || undefined),
      },
      React.createElement("option", { value: "" }),
      ...options.map((option: { value: string; label: string }) =>
        React.createElement(
          "option",
          { key: option.value, value: option.value },
          option.label,
        ),
      ),
    );
  return { DatePicker, Select, Tooltip };
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
    context_input_tokens: 100,
    context_window_tokens: 200,
    context_observed_calls: 1,
    near_compaction_calls: 0,
    cost_micros: 125,
    cost_unknown_calls: 1,
    context_usage_ratio: 50,
    max_context_usage_ratio: 50,
    call_count: 3,
    agent_id: "agent-a",
    chat_id: "chat-a",
    turn_id: "turn-a",
    ...overrides,
  };
}

function makeSummary(
  records: ReturnType<typeof makeRecord>[],
): TokenUsageSummary {
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
      context_input_tokens:
        acc.context_input_tokens + record.context_input_tokens,
      context_window_tokens:
        acc.context_window_tokens + record.context_window_tokens,
      context_observed_calls:
        acc.context_observed_calls + record.context_observed_calls,
      near_compaction_calls:
        acc.near_compaction_calls + record.near_compaction_calls,
      cost_micros: acc.cost_micros + record.cost_micros,
      cost_unknown_calls: acc.cost_unknown_calls + record.cost_unknown_calls,
      call_count: acc.call_count + record.call_count,
    }),
    {
      prompt_tokens: 0,
      completion_tokens: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      cache_eligible_input_tokens: 0,
      cache_observed_calls: 0,
      context_input_tokens: 0,
      context_window_tokens: 0,
      context_observed_calls: 0,
      near_compaction_calls: 0,
      cost_micros: 0,
      cost_unknown_calls: 0,
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
    context_input_tokens: record.context_input_tokens,
    context_window_tokens: record.context_window_tokens,
    context_observed_calls: record.context_observed_calls,
    near_compaction_calls: record.near_compaction_calls,
    cost_micros: record.cost_micros,
    cost_unknown_calls: record.cost_unknown_calls,
    context_usage_ratio: record.context_usage_ratio,
    max_context_usage_ratio: record.max_context_usage_ratio,
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
    total_context_input_tokens: totals.context_input_tokens,
    total_context_window_tokens: totals.context_window_tokens,
    context_observed_calls: totals.context_observed_calls,
    near_compaction_calls: totals.near_compaction_calls,
    total_cost_micros: totals.cost_micros,
    cost_unknown_calls: totals.cost_unknown_calls,
    context_usage_ratio:
      totals.context_window_tokens > 0
        ? (totals.context_input_tokens / totals.context_window_tokens) * 100
        : null,
    max_context_usage_ratio: records.length > 0 ? 50 : null,
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
    scopes: {
      agents: records.map((record) => ({
        ...stats(record),
        agent_id: record.agent_id,
      })),
      chats: records.map((record) => ({
        ...stats(record),
        agent_id: record.agent_id,
        chat_id: record.chat_id,
      })),
      turns: records.map((record) => ({
        ...stats(record),
        agent_id: record.agent_id,
        chat_id: record.chat_id,
        turn_id: record.turn_id,
      })),
    },
    by_agent: Object.fromEntries(
      records.map((record, index) => [
        `${record.agent_id ?? "none"}:${index}`,
        { ...stats(record), agent_id: record.agent_id },
      ]),
    ),
    by_chat: Object.fromEntries(
      records.map((record, index) => [
        `${record.agent_id ?? "none"}:${record.chat_id}:${index}`,
        {
          ...stats(record),
          agent_id: record.agent_id,
          chat_id: record.chat_id,
        },
      ]),
    ),
    by_turn: Object.fromEntries(
      records.map((record, index) => [
        `${record.agent_id ?? "none"}:${record.chat_id}:${
          record.turn_id
        }:${index}`,
        {
          ...stats(record),
          agent_id: record.agent_id,
          chat_id: record.chat_id,
          turn_id: record.turn_id,
        },
      ]),
    ),
  };
}

function setupDefaultMocks() {
  apiMocks.getTokenUsage.mockResolvedValue(makeSummary([makeRecord()]));
}

describe("TokenUsagePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    capturedProps.summary = null;
    capturedProps.tables = null;
    capturedProps.modelTrend = null;
    capturedProps.tokenType = null;
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
      usageUnobservedCalls: 0,
      totalPromptTokens: 100,
      totalCompletionTokens: 50,
      totalCacheReadTokens: 10,
      totalCacheEligibleInputTokens: 20,
      contextUsageRatio: 50,
      maxContextUsageRatio: 50,
      nearCompactionCalls: 0,
      totalCostMicros: 125,
      costUnknownCalls: 1,
    });
    expect(screen.getByTestId("data-tables")).toBeInTheDocument();
    expect(screen.getByTestId("model-trend-chart")).toBeInTheDocument();
    expect(screen.getByTestId("token-type-chart")).toBeInTheDocument();
  });

  it("shows calls whose provider usage is unavailable", async () => {
    const summary = makeSummary([makeRecord()]);
    summary.usage_observed_calls = 2;
    summary.usage_unobserved_calls = 1;
    apiMocks.getTokenUsage.mockResolvedValue(summary);

    render(<TokenUsagePage />);

    await waitFor(() =>
      expect(capturedProps.summary).toMatchObject({
        totalCalls: 3,
        usageUnobservedCalls: 1,
      }),
    );
  });

  it("builds resource table rows without ownership records", async () => {
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
    // Model labels use explicit route fields instead of opaque map keys.
    expect(
      capturedProps.tables.byModelData.map((r: any) => r.model).sort(),
    ).toEqual(["anthropic / claude", "openai / gpt-4o"]);
    expect(capturedProps.tables).not.toHaveProperty("byAgentData");
    expect(capturedProps.tables).not.toHaveProperty("byChatData");
    expect(capturedProps.tables).not.toHaveProperty("byTurnData");
  });

  it("renders colliding model keys from explicit route fields", async () => {
    const summary = makeSummary([makeRecord()]);
    const stats = Object.values(summary.by_model)[0];
    const dayjs = (await import("dayjs")).default;
    summary.by_date_model = {
      [dayjs().format("YYYY-MM-DD")]: {
        '["a","b:c"]': {
          ...stats,
          provider_id: "a",
          model: "b:c",
        },
        '["a:b","c"]': {
          ...stats,
          provider_id: "a:b",
          model: "c",
        },
      },
    };
    apiMocks.getTokenUsage.mockResolvedValue(summary);

    render(<TokenUsagePage />);
    await waitFor(() => expect(capturedProps.modelTrend).toBeTruthy());

    const activeSeries = capturedProps.modelTrend.chartConfig.data
      .filter((row: { value: number }) => row.value > 0)
      .map((row: { model: string }) => row.model)
      .sort();
    expect(activeSeries).toEqual(["a / b:c", "a:b / c"]);
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

  it("refetches token usage when the date range changes", async () => {
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("summary-cards")).toBeInTheDocument(),
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

  it("queries provider and model scopes without ownership filters", async () => {
    const user = userEvent.setup();
    render(<TokenUsagePage />);
    await waitFor(() => expect(capturedProps.tables).toBeTruthy());
    expect(screen.queryByLabelText("tokenUsage.agent")).toBeNull();
    expect(screen.queryByLabelText("tokenUsage.chat")).toBeNull();
    expect(screen.queryByLabelText("tokenUsage.turn")).toBeNull();

    await user.selectOptions(screen.getByLabelText("Provider"), "openai");
    await waitFor(() =>
      expect(apiMocks.getTokenUsage).toHaveBeenLastCalledWith(
        expect.objectContaining({
          provider: "openai",
        }),
      ),
    );
    const [query] = apiMocks.getTokenUsage.mock.calls.at(-1) ?? [];
    expect(query).not.toHaveProperty("agent_id");
    expect(query).not.toHaveProperty("chat_id");
    expect(query).not.toHaveProperty("turn_id");
  });

  it("ignores null date selections", async () => {
    render(<TokenUsagePage />);
    await waitFor(() =>
      expect(screen.getByTestId("summary-cards")).toBeInTheDocument(),
    );
    const callsBefore = apiMocks.getTokenUsage.mock.calls.length;
    datePickerMock.onChange(null);
    datePickerMock.onChange([null, null]);
    await new Promise((r) => setTimeout(r, 30));
    expect(apiMocks.getTokenUsage.mock.calls.length).toBe(callsBefore);
  });
});
