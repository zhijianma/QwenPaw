/** Single token usage record with optional ChatSpec and turn ownership. */
export interface TokenUsageRecord {
  date: string; // YYYY-MM-DD
  provider_id: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cache_eligible_input_tokens: number;
  cache_observed_calls: number;
  context_input_tokens: number;
  context_window_tokens: number;
  context_observed_calls: number;
  near_compaction_calls: number;
  context_usage_ratio: number | null;
  max_context_usage_ratio: number | null;
  call_count: number;
  agent_id?: string | null;
  conversation_id?: string | null;
  turn_id?: string | null;
}

/** Per-model (has provider_id, model) or per-date (no provider_id, model) stats. */
export interface TokenUsageStats {
  provider_id?: string;
  model?: string;
  prompt_tokens: number;
  completion_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cache_eligible_input_tokens: number;
  cache_observed_calls: number;
  context_input_tokens: number;
  context_window_tokens: number;
  context_observed_calls: number;
  near_compaction_calls: number;
  context_usage_ratio: number | null;
  max_context_usage_ratio: number | null;
  call_count: number;
}

export interface TokenUsageByAgent extends TokenUsageStats {
  agent_id: string | null;
}

export interface TokenUsageByChat extends TokenUsageByAgent {
  conversation_id: string | null;
}

export interface TokenUsageByTurn extends TokenUsageByChat {
  turn_id: string | null;
}

export interface TokenUsageSummary {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  total_cache_eligible_input_tokens: number;
  cache_observed_calls: number;
  cache_hit_rate: number | null;
  total_context_input_tokens: number;
  total_context_window_tokens: number;
  context_observed_calls: number;
  near_compaction_calls: number;
  context_usage_ratio: number | null;
  max_context_usage_ratio: number | null;
  total_calls: number;
  by_model: Record<string, TokenUsageStats>;
  by_date: Record<string, TokenUsageStats>;
  by_date_model: Record<string, Record<string, TokenUsageStats>>;
  by_agent: Record<string, TokenUsageByAgent>;
  by_chat: Record<string, TokenUsageByChat>;
  by_turn: Record<string, TokenUsageByTurn>;
}
