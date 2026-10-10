/* eslint-disable @typescript-eslint/no-explicit-any */
import React from "react";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DataTables } from "./DataTables";
import { SummaryCards } from "./SummaryCards";

const captured = vi.hoisted(() => ({ tables: [] as any[] }));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("@agentscope-ai/design", () => ({
  Card: ({ children }: { children: React.ReactNode }) =>
    React.createElement("div", null, children),
  Table: (props: any) => {
    captured.tables.push(props);
    return null;
  },
}));

const tokenRow = {
  key: "model-a",
  model: "provider-a:model-a",
  prompt_tokens: 100,
  completion_tokens: 20,
  cache_read_tokens: 0,
  cache_eligible_input_tokens: 100,
  context_usage_ratio: 50,
  max_context_usage_ratio: 50,
  near_compaction_calls: 0,
  cost_micros: 125,
  cost_unknown_calls: 2,
  call_count: 3,
};

describe("Token Usage cost evidence", () => {
  beforeEach(() => {
    captured.tables.length = 0;
  });

  it("renders known cost and unknown-call totals separately", () => {
    render(
      <SummaryCards
        totalCalls={3}
        totalPromptTokens={100}
        totalCompletionTokens={20}
        totalCacheReadTokens={0}
        totalCacheEligibleInputTokens={100}
        contextUsageRatio={50}
        maxContextUsageRatio={50}
        nearCompactionCalls={0}
        totalCostMicros={125}
        costUnknownCalls={2}
      />,
    );

    expect(screen.getByText("125 µ")).toBeInTheDocument();
    expect(screen.getByText("tokenUsage.reportedCost")).toBeInTheDocument();
    expect(screen.getByText("tokenUsage.costUnknown")).toBeInTheDocument();
  });

  it("adds cost evidence columns to resource usage tables", () => {
    render(
      <DataTables byModelData={[tokenRow]} byDateData={[]} />,
    );

    const columns = captured.tables[0].columns;
    const cost = columns.find((column: any) => column.key === "cost_micros");
    const unknown = columns.find(
      (column: any) => column.key === "cost_unknown_calls",
    );
    expect(cost.title).toBe("tokenUsage.reportedCost");
    expect(cost.render(tokenRow.cost_micros)).toBe("125 µ");
    expect(unknown.title).toBe("tokenUsage.costUnknown");
    expect(unknown.render(tokenRow.cost_unknown_calls)).toBe("2");
    const unavailable = columns.find(
      (column: any) => column.key === "usage_unobserved_calls",
    );
    expect(unavailable.title).toBe("tokenUsage.usageUnavailable");
    expect(unavailable.render(undefined)).toBe("0");
  });
});
