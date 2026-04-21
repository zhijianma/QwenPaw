import { request } from "../request";
import type {
  TokenUsageAgentStats,
  TokenUsageSessionStats,
  TokenUsageSummary,
} from "../types/tokenUsage";

export interface GetTokenUsageParams {
  start_date: string;
  end_date: string;
}

function buildQuery(params: GetTokenUsageParams): string {
  const search = new URLSearchParams({
    start_date: params.start_date,
    end_date: params.end_date,
  });
  return `?${search.toString()}`;
}

export const tokenUsageApi = {
  getTokenUsage: (params: GetTokenUsageParams) =>
    request<TokenUsageSummary>(`/token-usage${buildQuery(params)}`),

  getSessionTokenUsage: (sessionId: string) =>
    request<TokenUsageSessionStats>(`/token-usage/sessions/${encodeURIComponent(sessionId)}`),

  getAllAgentsTokenUsage: () =>
    request<Record<string, TokenUsageAgentStats>>("/token-usage/agents"),

  getAgentTokenUsage: (agentId: string) =>
    request<TokenUsageAgentStats>(`/token-usage/agents/${encodeURIComponent(agentId)}`),
};
