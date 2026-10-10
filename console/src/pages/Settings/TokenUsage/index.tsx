import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DatePicker, Select } from "antd";
import { useTranslation } from "react-i18next";
import dayjs, { type Dayjs } from "dayjs";
import { useTheme } from "../../../contexts/ThemeContext";
import api from "../../../api";
import type { TokenUsageSummary } from "../../../api/types/tokenUsage";
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
import { useModelTrendConfig } from "./hooks/useModelTrendConfig";
import { useTokenTypeConfig } from "./hooks/useTokenTypeConfig";
import { buildByDateRows } from "./tokenUsageRows";
import styles from "./index.module.less";

function TokenUsagePage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { isDark } = useTheme();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [usageSummary, setUsageSummary] = useState<TokenUsageSummary | null>(
    null,
  );
  const [startDate, setStartDate] = useState<Dayjs>(
    dayjs().subtract(30, "day"),
  );
  const [endDate, setEndDate] = useState<Dayjs>(dayjs());
  const [providerFilter, setProviderFilter] = useState<string>();
  const [modelFilter, setModelFilter] = useState<string>();
  const detailsFetchIdRef = useRef(0);

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
      provider: providerFilter,
      model: modelFilter,
    }),
    [dateRange, modelFilter, providerFilter],
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
    const providers = new Set<string>();
    const models = new Set<string>();
    for (const row of Object.values(usageSummary?.by_model ?? {})) {
      if (row.provider_id) providers.add(row.provider_id);
      if (row.model) models.add(row.model);
    }
    return {
      providers: [...providers].sort().map((provider) => ({
        value: provider,
        label: provider,
      })),
      models: [...models].sort().map((model) => ({
        value: model,
        label: model,
      })),
    };
  }, [usageSummary]);

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

        {!error &&
          (tablesEmpty ? (
            <EmptyState message={t("tokenUsage.noData")} />
          ) : (
            <DataTables
              byModelData={byModelData}
              byDateData={byDateData}
            />
          ))}
      </div>
    </div>
  );
}

export default TokenUsagePage;
