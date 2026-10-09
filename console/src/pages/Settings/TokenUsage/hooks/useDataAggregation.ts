import { useMemo } from "react";
import type { TokenUsageRecord } from "../../../../api/types/tokenUsage";

interface AggregatedData {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  total_cache_eligible_input_tokens: number;
  cache_observed_calls: number;
  total_calls: number;
  by_model: Record<
    string,
    {
      model: string;
      provider_id: string;
      prompt_tokens: number;
      completion_tokens: number;
      cache_read_tokens: number;
      cache_write_tokens: number;
      cache_eligible_input_tokens: number;
      cache_observed_calls: number;
      call_count: number;
    }
  >;
  by_date: Record<
    string,
    {
      prompt_tokens: number;
      completion_tokens: number;
      cache_read_tokens: number;
      cache_write_tokens: number;
      cache_eligible_input_tokens: number;
      cache_observed_calls: number;
      call_count: number;
    }
  >;
  by_agent: Record<
    string,
    {
      agent_id: string;
      prompt_tokens: number;
      completion_tokens: number;
      cache_read_tokens: number;
      cache_write_tokens: number;
      cache_eligible_input_tokens: number;
      cache_observed_calls: number;
      call_count: number;
    }
  >;
  by_chat: Record<
    string,
    {
      conversation_id: string;
      agent_id: string;
      prompt_tokens: number;
      completion_tokens: number;
      cache_read_tokens: number;
      cache_write_tokens: number;
      cache_eligible_input_tokens: number;
      cache_observed_calls: number;
      call_count: number;
    }
  >;
  by_turn: Record<
    string,
    {
      turn_id: string;
      conversation_id: string;
      agent_id: string;
      prompt_tokens: number;
      completion_tokens: number;
      cache_read_tokens: number;
      cache_write_tokens: number;
      cache_eligible_input_tokens: number;
      cache_observed_calls: number;
      call_count: number;
    }
  >;
  by_date_model: Record<
    string,
    Record<
      string,
      {
        model: string;
        provider_id: string;
        prompt_tokens: number;
        completion_tokens: number;
        cache_read_tokens: number;
        cache_write_tokens: number;
        cache_eligible_input_tokens: number;
        cache_observed_calls: number;
        call_count: number;
      }
    >
  >;
}

export function useDataAggregation(records: TokenUsageRecord[]) {
  return useMemo<AggregatedData | null>(() => {
    if (records.length === 0) return null;

    const byModel: AggregatedData["by_model"] = {};
    const byDate: AggregatedData["by_date"] = {};
    const byAgent: AggregatedData["by_agent"] = {};
    const byChat: AggregatedData["by_chat"] = {};
    const byTurn: AggregatedData["by_turn"] = {};
    const byDateModel: AggregatedData["by_date_model"] = {};

    let totalPrompt = 0;
    let totalCompletion = 0;
    let totalCacheRead = 0;
    let totalCacheWrite = 0;
    let totalCacheEligible = 0;
    let cacheObservedCalls = 0;
    let totalCalls = 0;

    records.forEach((r) => {
      const pt = r.prompt_tokens;
      const ct = r.completion_tokens;
      const calls = r.call_count;
      const providerId = r.provider_id;
      const agentId = r.agent_id;
      const agentKey = agentId ? agentId : "__unattributed__";
      const chatId = r.conversation_id || "";
      const chatKey = chatId
        ? `${agentKey}\u001f${chatId}`
        : `${agentKey}\u001f__unattributed__`;
      const turnId = r.turn_id || "";
      const turnKey = turnId
        ? `${chatKey}\u001f${turnId}`
        : `${chatKey}\u001f__unattributed__`;
      totalPrompt += pt;
      totalCompletion += ct;
      totalCacheRead += r.cache_read_tokens;
      totalCacheWrite += r.cache_write_tokens;
      totalCacheEligible += r.cache_eligible_input_tokens;
      cacheObservedCalls += r.cache_observed_calls;
      totalCalls += calls;

      const modelKey = `${providerId}:${r.model}`;
      if (!byModel[modelKey]) {
        byModel[modelKey] = {
          model: r.model,
          provider_id: providerId,
          prompt_tokens: 0,
          completion_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 0,
          cache_observed_calls: 0,
          call_count: 0,
        };
      }
      byModel[modelKey].prompt_tokens += pt;
      byModel[modelKey].completion_tokens += ct;
      byModel[modelKey].cache_read_tokens += r.cache_read_tokens;
      byModel[modelKey].cache_write_tokens += r.cache_write_tokens;
      byModel[modelKey].cache_eligible_input_tokens +=
        r.cache_eligible_input_tokens;
      byModel[modelKey].cache_observed_calls += r.cache_observed_calls;
      byModel[modelKey].call_count += calls;

      if (!byDate[r.date]) {
        byDate[r.date] = {
          prompt_tokens: 0,
          completion_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 0,
          cache_observed_calls: 0,
          call_count: 0,
        };
      }
      byDate[r.date].prompt_tokens += pt;
      byDate[r.date].completion_tokens += ct;
      byDate[r.date].cache_read_tokens += r.cache_read_tokens;
      byDate[r.date].cache_write_tokens += r.cache_write_tokens;
      byDate[r.date].cache_eligible_input_tokens +=
        r.cache_eligible_input_tokens;
      byDate[r.date].cache_observed_calls += r.cache_observed_calls;
      byDate[r.date].call_count += calls;

      if (!byAgent[agentKey]) {
        byAgent[agentKey] = {
          agent_id: agentId || "",
          prompt_tokens: 0,
          completion_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 0,
          cache_observed_calls: 0,
          call_count: 0,
        };
      }
      byAgent[agentKey].prompt_tokens += pt;
      byAgent[agentKey].completion_tokens += ct;
      byAgent[agentKey].cache_read_tokens += r.cache_read_tokens;
      byAgent[agentKey].cache_write_tokens += r.cache_write_tokens;
      byAgent[agentKey].cache_eligible_input_tokens +=
        r.cache_eligible_input_tokens;
      byAgent[agentKey].cache_observed_calls += r.cache_observed_calls;
      byAgent[agentKey].call_count += calls;

      if (!byChat[chatKey]) {
        byChat[chatKey] = {
          conversation_id: chatId,
          agent_id: agentId || "",
          prompt_tokens: 0,
          completion_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 0,
          cache_observed_calls: 0,
          call_count: 0,
        };
      }
      byChat[chatKey].prompt_tokens += pt;
      byChat[chatKey].completion_tokens += ct;
      byChat[chatKey].cache_read_tokens += r.cache_read_tokens;
      byChat[chatKey].cache_write_tokens += r.cache_write_tokens;
      byChat[chatKey].cache_eligible_input_tokens +=
        r.cache_eligible_input_tokens;
      byChat[chatKey].cache_observed_calls += r.cache_observed_calls;
      byChat[chatKey].call_count += calls;

      if (!byTurn[turnKey]) {
        byTurn[turnKey] = {
          turn_id: turnId,
          conversation_id: chatId,
          agent_id: agentId || "",
          prompt_tokens: 0,
          completion_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 0,
          cache_observed_calls: 0,
          call_count: 0,
        };
      }
      byTurn[turnKey].prompt_tokens += pt;
      byTurn[turnKey].completion_tokens += ct;
      byTurn[turnKey].cache_read_tokens += r.cache_read_tokens;
      byTurn[turnKey].cache_write_tokens += r.cache_write_tokens;
      byTurn[turnKey].cache_eligible_input_tokens +=
        r.cache_eligible_input_tokens;
      byTurn[turnKey].cache_observed_calls += r.cache_observed_calls;
      byTurn[turnKey].call_count += calls;

      if (!byDateModel[r.date]) {
        byDateModel[r.date] = {};
      }
      if (!byDateModel[r.date][modelKey]) {
        byDateModel[r.date][modelKey] = {
          model: r.model,
          provider_id: providerId,
          prompt_tokens: 0,
          completion_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          cache_eligible_input_tokens: 0,
          cache_observed_calls: 0,
          call_count: 0,
        };
      }
      byDateModel[r.date][modelKey].prompt_tokens += pt;
      byDateModel[r.date][modelKey].completion_tokens += ct;
      byDateModel[r.date][modelKey].cache_read_tokens += r.cache_read_tokens;
      byDateModel[r.date][modelKey].cache_write_tokens += r.cache_write_tokens;
      byDateModel[r.date][modelKey].cache_eligible_input_tokens +=
        r.cache_eligible_input_tokens;
      byDateModel[r.date][modelKey].cache_observed_calls +=
        r.cache_observed_calls;
      byDateModel[r.date][modelKey].call_count += calls;
    });

    return {
      total_prompt_tokens: totalPrompt,
      total_completion_tokens: totalCompletion,
      total_cache_read_tokens: totalCacheRead,
      total_cache_write_tokens: totalCacheWrite,
      total_cache_eligible_input_tokens: totalCacheEligible,
      cache_observed_calls: cacheObservedCalls,
      total_calls: totalCalls,
      by_model: byModel,
      by_date: byDate,
      by_agent: byAgent,
      by_chat: byChat,
      by_turn: byTurn,
      by_date_model: byDateModel,
    };
  }, [records]);
}
