import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { cacheHitRate } from "../../../../utils/cacheUsage";
import { useDataAggregation } from "./useDataAggregation";

describe("useDataAggregation cache usage", () => {
  it("aggregates cache tokens before calculating the hit rate", () => {
    const { result } = renderHook(() =>
      useDataAggregation([
        {
          date: "2026-08-27",
          provider_id: "deepseek",
          model: "deepseek-chat",
          prompt_tokens: 100,
          completion_tokens: 10,
          cache_read_tokens: 90,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 100,
          cache_observed_calls: 1,
          call_count: 1,
        },
        {
          date: "2026-08-27",
          provider_id: "deepseek",
          model: "deepseek-chat",
          prompt_tokens: 900,
          completion_tokens: 20,
          cache_read_tokens: 450,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 900,
          cache_observed_calls: 1,
          call_count: 1,
        },
      ]),
    );

    expect(result.current?.total_cache_read_tokens).toBe(540);
    expect(result.current?.total_cache_eligible_input_tokens).toBe(1000);
    expect(
      cacheHitRate(
        result.current?.total_cache_read_tokens || 0,
        result.current?.total_cache_eligible_input_tokens || 0,
      ),
    ).toBe(54);
  });

  it("keeps agent, chat, and turn ownership as separate scopes", () => {
    const base = {
      date: "2026-10-09",
      provider_id: "dashscope",
      model: "qwen-max",
      completion_tokens: 5,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      cache_eligible_input_tokens: 0,
      cache_observed_calls: 0,
      call_count: 1,
    };
    const { result } = renderHook(() =>
      useDataAggregation([
        {
          ...base,
          prompt_tokens: 10,
          agent_id: "agent-a",
          conversation_id: "chat-shared",
          turn_id: "turn-shared",
        },
        {
          ...base,
          prompt_tokens: 20,
          agent_id: "agent-b",
          conversation_id: "chat-shared",
          turn_id: "turn-shared",
        },
        {
          ...base,
          prompt_tokens: 30,
          agent_id: null,
          conversation_id: null,
          turn_id: null,
        },
        {
          ...base,
          prompt_tokens: 40,
          agent_id: "agent-b",
          conversation_id: null,
          turn_id: null,
        },
      ]),
    );

    expect(Object.values(result.current?.by_chat ?? {})).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ agent_id: "agent-a", prompt_tokens: 10 }),
        expect.objectContaining({ agent_id: "agent-b", prompt_tokens: 20 }),
        expect.objectContaining({
          conversation_id: "",
          prompt_tokens: 30,
        }),
        expect.objectContaining({
          agent_id: "agent-b",
          conversation_id: "",
          prompt_tokens: 40,
        }),
      ]),
    );
    expect(Object.values(result.current?.by_chat ?? {})).toHaveLength(4);
    expect(Object.values(result.current?.by_turn ?? {})).toHaveLength(4);
    expect(Object.values(result.current?.by_turn ?? {})).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          agent_id: "agent-a",
          conversation_id: "chat-shared",
          turn_id: "turn-shared",
        }),
        expect.objectContaining({
          agent_id: "agent-b",
          conversation_id: "chat-shared",
          turn_id: "turn-shared",
        }),
      ]),
    );
  });
});
