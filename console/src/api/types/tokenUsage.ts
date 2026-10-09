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
  cost_micros: number;
  cost_unknown_calls: number;
  context_usage_ratio: number | null;
  max_context_usage_ratio: number | null;
  usage_observed_calls?: number;
  usage_unobserved_calls?: number;
  call_count: number;
  agent_id?: string | null;
  chat_id?: string | null;
  /** @deprecated Use chat_id. */
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
  cost_micros: number;
  cost_unknown_calls: number;
  context_usage_ratio: number | null;
  max_context_usage_ratio: number | null;
  usage_observed_calls?: number;
  usage_unobserved_calls?: number;
  call_count: number;
}

export interface TokenUsageByModel extends TokenUsageStats {
  provider_id: string;
  model: string;
}

export interface TokenUsageByAgent extends TokenUsageStats {
  agent_id: string | null;
}

export interface TokenUsageByChat extends TokenUsageByAgent {
  chat_id?: string | null;
  /** @deprecated Rolling-upgrade alias; use chat_id. */
  conversation_id?: string | null;
}

export interface TokenUsageByTurn extends TokenUsageByChat {
  turn_id: string | null;
}

export interface TokenUsageScopeRows {
  agents: TokenUsageByAgent[];
  chats: TokenUsageByChat[];
  turns: TokenUsageByTurn[];
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
  total_cost_micros: number;
  cost_unknown_calls: number;
  context_usage_ratio: number | null;
  max_context_usage_ratio: number | null;
  total_calls: number;
  usage_observed_calls?: number;
  usage_unobserved_calls?: number;
  by_model: Record<string, TokenUsageByModel>;
  by_date: Record<string, TokenUsageStats>;
  by_date_model: Record<string, Record<string, TokenUsageByModel>>;
  /** Structured ownership rows. Optional while rolling upgrades are supported. */
  scopes?: TokenUsageScopeRows;
  /** @deprecated Prefer scopes.agents. */
  by_agent: Record<string, TokenUsageByAgent>;
  /** @deprecated Prefer scopes.chats. */
  by_chat: Record<string, TokenUsageByChat>;
  /** @deprecated Prefer scopes.turns. */
  by_turn: Record<string, TokenUsageByTurn>;
}
