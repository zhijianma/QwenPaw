/** Per-model (has provider_id, model) or per-date (no provider_id, model) stats. */
export interface TokenUsageStats {
  provider_id?: string;
  model?: string;
  prompt_tokens: number;
  completion_tokens: number;
  call_count: number;
}

/** Per-session token usage stats returned by /token-usage/sessions/:id */
export interface TokenUsageSessionStats {
  agent_id: string;
  prompt_tokens: number;
  completion_tokens: number;
  call_count: number;
  last_updated?: string;
}

/** Per-agent token usage stats returned by /token-usage/agents/:id */
export interface TokenUsageAgentStats {
  agent_id: string;
  prompt_tokens: number;
  completion_tokens: number;
  call_count: number;
}

export interface TokenUsageSummary {
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_calls: number;
  by_model: Record<string, TokenUsageStats>;
  by_date: Record<string, TokenUsageStats>;
  by_session: Record<string, TokenUsageSessionStats>;
  by_agent: Record<string, TokenUsageAgentStats>;
}
