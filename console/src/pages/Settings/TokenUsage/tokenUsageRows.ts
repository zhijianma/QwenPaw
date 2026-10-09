/**
 * Row builders for the Token Usage tables.
 *
 * Extracted from TokenUsage/index.tsx so the row contract can be unit-tested
 * without rendering the page. Behaviour is unchanged.
 *
 * Regressions guarded here:
 * - #3368: the by-date table must list the newest date first. Users had to
 *   scroll to the bottom to find the latest day, so rows are sorted by date
 *   descending.
 */

export interface DateTokenRow {
  key: string;
  date: string;
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
  call_count: number;
}

export interface DateTokenStats {
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
  call_count: number;
}

/**
 * Builds by-date table rows sorted by date **descending** (newest first).
 * Regression guard for #3368.
 */
export function buildByDateRows(
  byDate: Record<string, DateTokenStats> | null | undefined,
): DateTokenRow[] {
  if (!byDate) return [];
  return Object.entries(byDate)
    .map(([date, stats]) => ({
      key: date,
      date,
      prompt_tokens: stats.prompt_tokens,
      completion_tokens: stats.completion_tokens,
      cache_read_tokens: stats.cache_read_tokens,
      cache_write_tokens: stats.cache_write_tokens,
      cache_eligible_input_tokens: stats.cache_eligible_input_tokens,
      cache_observed_calls: stats.cache_observed_calls,
      context_input_tokens: stats.context_input_tokens,
      context_window_tokens: stats.context_window_tokens,
      context_observed_calls: stats.context_observed_calls,
      near_compaction_calls: stats.near_compaction_calls,
      cost_micros: stats.cost_micros ?? 0,
      cost_unknown_calls: stats.cost_unknown_calls ?? 0,
      context_usage_ratio: stats.context_usage_ratio,
      max_context_usage_ratio: stats.max_context_usage_ratio,
      call_count: stats.call_count,
    }))
    .sort((a, b) => b.date.localeCompare(a.date));
}
