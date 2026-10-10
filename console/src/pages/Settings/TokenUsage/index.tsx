import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DatePicker, Select, Tooltip } from "antd";
import { Card } from "@agentscope-ai/design";
import { Line } from "@ant-design/plots";
import { useTranslation } from "react-i18next";
import dayjs, { type Dayjs } from "dayjs";
import { useTheme } from "../../../contexts/ThemeContext";
import api from "../../../api";
import type { TokenUsageSummary } from "../../../api/types/tokenUsage";
import type { LlmToolDaily } from "../../../api/modules/agentStats";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { PageHeader } from "@/components/PageHeader";
import {
  LoadingState,
  SummaryCards,
  ModelTrendChart,
  TokenTypeChart,
  DataTables,
  EmptyState,
} from "./components";
import { useAgentStore } from "../../../stores/agentStore";
import { getAgentDisplayName } from "../../../utils/agentDisplayName";
import { lineChartChrome } from "./hooks/lineChartChrome";
import { useModelTrendConfig } from "./hooks/useModelTrendConfig";
import { useTokenTypeConfig } from "./hooks/useTokenTypeConfig";
import { buildByDateRows } from "./tokenUsageRows";
import styles from "./index.module.less";

function TokenUsagePage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { isDark } = useTheme();
  const agents = useAgentStore((state) => state.agents);
  const agentsById = useMemo(
    () => new Map(agents.map((agent) => [agent.id, agent])),
    [agents],
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [usageSummary, setUsageSummary] = useState<TokenUsageSummary | null>(
    null,
  );
  const [llmToolDays, setLlmToolDays] = useState<LlmToolDaily[] | null>(null);
  const [trendLoading, setTrendLoading] = useState(true);
  const [trendError, setTrendError] = useState(false);
  const [startDate, setStartDate] = useState<Dayjs>(
    dayjs().subtract(30, "day"),
  );
  const [endDate, setEndDate] = useState<Dayjs>(dayjs());
  const [agentFilter, setAgentFilter] = useState<string>();
  const [chatFilter, setChatFilter] = useState<string>();
  const [turnFilter, setTurnFilter] = useState<string>();
  const [providerFilter, setProviderFilter] = useState<string>();
  const [modelFilter, setModelFilter] = useState<string>();
  const detailsFetchIdRef = useRef(0);
  const trendFetchIdRef = useRef(0);
  const trendAbortRef = useRef<AbortController | null>(null);

  const dateRange = useMemo(
    () => ({
      start_date: startDate.format("YYYY-MM-DD"),
      end_date: endDate.format("YYYY-MM-DD"),
    }),
    [startDate, endDate],
  );

  const usageQuery = useMemo(
    () => ({
      ...dateRange,
      agent_id: agentFilter,
      chat_id: chatFilter,
      turn_id: turnFilter,
      provider: providerFilter,
      model: modelFilter,
    }),
    [
      agentFilter,
      chatFilter,
      dateRange,
      modelFilter,
      providerFilter,
      turnFilter,
    ],
  );

  const fetchTrend = useCallback(
    async (fetchId: number) => {
      trendAbortRef.current?.abort();
      const controller = new AbortController();
      trendAbortRef.current = controller;
      setTrendLoading(true);
      setTrendError(false);
      try {
        const data = await api.getGlobalLlmToolTrend(dateRange, {
          signal: controller.signal,
        });
        if (fetchId !== trendFetchIdRef.current) return;
        setLlmToolDays(data);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        console.error("Failed to load llm/tool trend:", err);
        if (fetchId !== trendFetchIdRef.current) return;
        setLlmToolDays(null);
        setTrendError(true);
      } finally {
        if (fetchId === trendFetchIdRef.current) {
          setTrendLoading(false);
        }
      }
    },
    [dateRange],
  );

  const fetchData = useCallback(async () => {
    const detailsId = ++detailsFetchIdRef.current;
    setLoading(true);
    setError(false);
    try {
      const summary = await api.getTokenUsage(usageQuery);
      if (detailsId !== detailsFetchIdRef.current) return;
      setUsageSummary(summary);
    } catch (err) {
      console.error("Failed to load token usage:", err);
      if (detailsId !== detailsFetchIdRef.current) return;
      message.error(t("tokenUsage.loadFailed"));
      setUsageSummary(null);
      setError(true);
    } finally {
      if (detailsId === detailsFetchIdRef.current) {
        setLoading(false);
      }
    }
  }, [message, t, usageQuery]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  useEffect(() => {
    void fetchTrend(++trendFetchIdRef.current);
  }, [fetchTrend]);

  const handleDateChange = (dates: [Dayjs | null, Dayjs | null] | null) => {
    if (!dates || !dates[0] || !dates[1]) {
      return;
    }
    setStartDate(dates[0]);
    setEndDate(dates[1]);
  };

  const aggregatedData =
    usageSummary && usageSummary.total_calls > 0 ? usageSummary : null;

  const filterOptions = useMemo(() => {
    const scopeRows = usageSummary?.scopes;
    const agentIds = new Set<string>();
    const chatIds = new Set<string>();
    const turnIds = new Set<string>();
    for (const row of scopeRows?.agents ?? []) {
      if (row.agent_id) agentIds.add(row.agent_id);
    }
    for (const row of scopeRows?.chats ?? []) {
      if (row.chat_id) chatIds.add(row.chat_id);
    }
    for (const row of scopeRows?.turns ?? []) {
      if (row.turn_id) turnIds.add(row.turn_id);
    }
    const providers = new Set<string>();
    const models = new Set<string>();
    for (const row of Object.values(usageSummary?.by_model ?? {})) {
      if (row.provider_id) providers.add(row.provider_id);
      if (row.model) models.add(row.model);
    }
    return {
      agents: [...agentIds].sort().map((agentId) => ({
        value: agentId,
        label: agentsById.has(agentId)
          ? getAgentDisplayName(agentsById.get(agentId)!, t)
          : agentId,
      })),
      chats: [...chatIds].sort().map((chatId) => ({
        value: chatId,
        label: chatId,
      })),
      turns: [...turnIds].sort().map((turnId) => ({
        value: turnId,
        label: turnId,
      })),
      providers: [...providers].sort().map((provider) => ({
        value: provider,
        label: provider,
      })),
      models: [...models].sort().map((model) => ({
        value: model,
        label: model,
      })),
    };
  }, [agentsById, t, usageSummary]);

  const modelTrendConfig = useModelTrendConfig({
    byDateModel: aggregatedData?.by_date_model ?? null,
    startDate,
    endDate,
    isDark,
  });

  const tokenTypeConfig = useTokenTypeConfig({
    byDate: aggregatedData?.by_date ?? null,
    startDate,
    endDate,
    isDark,
  });

  const llmToolConfig = useMemo(() => {
    const days = llmToolDays ?? [];
    const llmLabel = t("tokenUsage.recordedTurnsAllAgents");
    const toolLabel = t("tokenUsage.toolCalls");
    return {
      data: days.flatMap((row) => [
        { date: row.date, type: llmLabel, value: row.agent_llm_calls },
        { date: row.date, type: toolLabel, value: row.tool_calls },
      ]),
      ...lineChartChrome({
        isDark,
        tickCount: Math.min(10, Math.max(3, days.length)),
        startDate,
        endDate,
        seriesField: "type",
        colors: ["#722ed1", "#13c2c2"],
      }),
    };
  }, [llmToolDays, startDate, endDate, isDark, t]);

  const byModelData = useMemo(() => {
    if (!aggregatedData?.by_model) return [];
    return Object.entries(aggregatedData.by_model).map(([key, stats]) => ({
      key,
      model: stats.provider_id
        ? `${stats.provider_id} / ${stats.model}`
        : stats.model,
      prompt_tokens: stats.prompt_tokens,
      completion_tokens: stats.completion_tokens,
      cache_read_tokens: stats.cache_read_tokens,
      cache_eligible_input_tokens: stats.cache_eligible_input_tokens,
      context_usage_ratio: stats.context_usage_ratio,
      max_context_usage_ratio: stats.max_context_usage_ratio,
      near_compaction_calls: stats.near_compaction_calls,
      cost_micros: stats.cost_micros ?? 0,
      cost_unknown_calls: stats.cost_unknown_calls ?? 0,
      usage_unobserved_calls: stats.usage_unobserved_calls ?? 0,
      call_count: stats.call_count,
    }));
  }, [aggregatedData?.by_model]);

  const byDateData = useMemo(
    () => buildByDateRows(aggregatedData?.by_date),
    [aggregatedData?.by_date],
  );

  const byAgentData = useMemo(() => {
    if (!aggregatedData) return [];
    const rows =
      aggregatedData.scopes?.agents ?? Object.values(aggregatedData.by_agent);
    return rows
      .map((stats) => {
        const agentId = stats.agent_id;
        const profile = agentId ? agentsById.get(agentId) : undefined;
        const agent = !agentId
          ? t("tokenUsage.unattributed")
          : profile
          ? getAgentDisplayName(profile, t)
          : agentId;
        return {
          key: JSON.stringify([agentId]),
          agent,
          prompt_tokens: stats.prompt_tokens,
          completion_tokens: stats.completion_tokens,
          cache_read_tokens: stats.cache_read_tokens,
          cache_eligible_input_tokens: stats.cache_eligible_input_tokens,
          context_usage_ratio: stats.context_usage_ratio,
          max_context_usage_ratio: stats.max_context_usage_ratio,
          near_compaction_calls: stats.near_compaction_calls,
          cost_micros: stats.cost_micros ?? 0,
          cost_unknown_calls: stats.cost_unknown_calls ?? 0,
          usage_unobserved_calls: stats.usage_unobserved_calls ?? 0,
          call_count: stats.call_count,
        };
      })
      .sort(
        (a, b) =>
          b.prompt_tokens +
          b.completion_tokens -
          (a.prompt_tokens + a.completion_tokens),
      );
  }, [aggregatedData, agentsById, t]);

  const byChatData = useMemo(() => {
    if (!aggregatedData) return [];
    const rows =
      aggregatedData.scopes?.chats ?? Object.values(aggregatedData.by_chat);
    return rows
      .map((stats) => {
        const chatId = stats.chat_id ?? null;
        const profile = stats.agent_id
          ? agentsById.get(stats.agent_id)
          : undefined;
        return {
          key: JSON.stringify([stats.agent_id, chatId]),
          agent: !stats.agent_id
            ? t("tokenUsage.unattributed")
            : profile
            ? getAgentDisplayName(profile, t)
            : stats.agent_id,
          chat: chatId || t("tokenUsage.unattributed"),
          prompt_tokens: stats.prompt_tokens,
          completion_tokens: stats.completion_tokens,
          cache_read_tokens: stats.cache_read_tokens,
          cache_eligible_input_tokens: stats.cache_eligible_input_tokens,
          context_usage_ratio: stats.context_usage_ratio,
          max_context_usage_ratio: stats.max_context_usage_ratio,
          near_compaction_calls: stats.near_compaction_calls,
          cost_micros: stats.cost_micros ?? 0,
          cost_unknown_calls: stats.cost_unknown_calls ?? 0,
          usage_unobserved_calls: stats.usage_unobserved_calls ?? 0,
          call_count: stats.call_count,
        };
      })
      .sort(
        (a, b) =>
          b.prompt_tokens +
          b.completion_tokens -
          (a.prompt_tokens + a.completion_tokens),
      );
  }, [aggregatedData, agentsById, t]);

  const byTurnData = useMemo(() => {
    if (!aggregatedData) return [];
    const rows =
      aggregatedData.scopes?.turns ?? Object.values(aggregatedData.by_turn);
    return rows
      .map((stats) => {
        const chatId = stats.chat_id ?? null;
        const profile = stats.agent_id
          ? agentsById.get(stats.agent_id)
          : undefined;
        return {
          key: JSON.stringify([stats.agent_id, chatId, stats.turn_id]),
          agent: !stats.agent_id
            ? t("tokenUsage.unattributed")
            : profile
            ? getAgentDisplayName(profile, t)
            : stats.agent_id,
          chat: chatId || t("tokenUsage.unattributed"),
          turn: stats.turn_id || t("tokenUsage.unattributed"),
          prompt_tokens: stats.prompt_tokens,
          completion_tokens: stats.completion_tokens,
          cache_read_tokens: stats.cache_read_tokens,
          cache_eligible_input_tokens: stats.cache_eligible_input_tokens,
          context_usage_ratio: stats.context_usage_ratio,
          max_context_usage_ratio: stats.max_context_usage_ratio,
          near_compaction_calls: stats.near_compaction_calls,
          cost_micros: stats.cost_micros ?? 0,
          cost_unknown_calls: stats.cost_unknown_calls ?? 0,
          usage_unobserved_calls: stats.usage_unobserved_calls ?? 0,
          call_count: stats.call_count,
        };
      })
      .sort(
        (a, b) =>
          b.prompt_tokens +
          b.completion_tokens -
          (a.prompt_tokens + a.completion_tokens),
      );
  }, [aggregatedData, agentsById, t]);

  const tablesEmpty = byModelData.length === 0 && byDateData.length === 0;

  const pageHeader = (
    <PageHeader parent={t("nav.settings")} current={t("tokenUsage.title")} />
  );

  if (loading) {
    return (
      <div className={styles.container}>
        {pageHeader}
        <LoadingState message={t("common.loading", "Loading...")} />
      </div>
    );
  }

  return (
    <div className={styles.container}>
      {pageHeader}

      <div className={styles.content}>
        <div className={styles.toolbar}>
          <DatePicker.RangePicker
            value={[startDate, endDate]}
            onChange={handleDateChange}
            disabledDate={(current: Dayjs, info?: { from?: Dayjs }) => {
              if (!current || current.isAfter(dayjs(), "day")) return true;
              if (info?.from) {
                return Math.abs(current.diff(info.from, "day")) >= 365;
              }
              return false;
            }}
          />
          <Select
            aria-label={t("tokenUsage.agent")}
            allowClear
            placeholder={t("tokenUsage.agent")}
            value={agentFilter}
            options={filterOptions.agents}
            onChange={(value) => {
              setAgentFilter(value);
              setChatFilter(undefined);
              setTurnFilter(undefined);
            }}
          />
          <Select
            aria-label={t("tokenUsage.chat")}
            allowClear
            placeholder={t("tokenUsage.chat")}
            value={chatFilter}
            options={filterOptions.chats}
            onChange={(value) => {
              setChatFilter(value);
              setTurnFilter(undefined);
            }}
          />
          <Select
            aria-label={t("tokenUsage.turn")}
            allowClear
            placeholder={t("tokenUsage.turn")}
            value={turnFilter}
            options={filterOptions.turns}
            onChange={setTurnFilter}
          />
          <Select
            aria-label={t("tokenUsage.provider", "Provider")}
            allowClear
            placeholder={t("tokenUsage.provider", "Provider")}
            value={providerFilter}
            options={filterOptions.providers}
            onChange={(value) => {
              setProviderFilter(value);
              setModelFilter(undefined);
            }}
          />
          <Select
            aria-label={t("tokenUsage.model")}
            allowClear
            placeholder={t("tokenUsage.model")}
            value={modelFilter}
            options={filterOptions.models}
            onChange={setModelFilter}
          />
        </div>

        {error ? (
          <LoadingState
            message={t("tokenUsage.loadFailed")}
            error
            onRetry={fetchData}
          />
        ) : (
          <>
            {aggregatedData && (
              <SummaryCards
                totalCalls={aggregatedData.total_calls}
                usageUnobservedCalls={
                  aggregatedData.usage_unobserved_calls ?? 0
                }
                totalPromptTokens={aggregatedData.total_prompt_tokens}
                totalCompletionTokens={aggregatedData.total_completion_tokens}
                totalCacheReadTokens={aggregatedData.total_cache_read_tokens}
                totalCacheEligibleInputTokens={
                  aggregatedData.total_cache_eligible_input_tokens
                }
                contextUsageRatio={aggregatedData.context_usage_ratio}
                maxContextUsageRatio={aggregatedData.max_context_usage_ratio}
                nearCompactionCalls={aggregatedData.near_compaction_calls}
                totalCostMicros={aggregatedData.total_cost_micros ?? 0}
                costUnknownCalls={aggregatedData.cost_unknown_calls ?? 0}
              />
            )}

            <div className={styles.trendRow}>
              <ModelTrendChart chartConfig={modelTrendConfig} />
              <TokenTypeChart chartConfig={tokenTypeConfig} />
            </div>
          </>
        )}

        <Card
          className={styles.chartCard}
          title={
            <Tooltip title={t("tokenUsage.llmAndToolTrendTooltip")}>
              <span className={styles.chartTitle}>
                {t("tokenUsage.llmAndToolTrend")}
              </span>
            </Tooltip>
          }
        >
          {trendLoading ? (
            <LoadingState message={t("common.loading", "Loading...")} />
          ) : trendError ? (
            <LoadingState
              message={t("tokenUsage.llmAndToolTrendLoadFailed")}
              error
              onRetry={() => {
                void fetchTrend(++trendFetchIdRef.current);
              }}
            />
          ) : (llmToolDays ?? []).every(
              (d) => d.agent_llm_calls === 0 && d.tool_calls === 0,
            ) ? (
            <EmptyState message={t("tokenUsage.noData")} />
          ) : (
            <Line {...llmToolConfig} />
          )}
        </Card>

        {!error &&
          (tablesEmpty ? (
            <EmptyState message={t("tokenUsage.noData")} />
          ) : (
            <DataTables
              byModelData={byModelData}
              byDateData={byDateData}
              byAgentData={byAgentData}
              byChatData={byChatData}
              byTurnData={byTurnData}
            />
          ))}
      </div>
    </div>
  );
}

export default TokenUsagePage;
