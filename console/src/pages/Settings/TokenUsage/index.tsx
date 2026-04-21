import { useEffect, useMemo, useState } from "react";
import { Button, Card, Table } from "@agentscope-ai/design";
import type { ColumnsType } from "antd/es/table";
import { DatePicker, Tabs } from "antd";
import { useTranslation } from "react-i18next";
import dayjs, { Dayjs } from "dayjs";
import api from "../../../api";
import type {
  TokenUsageSummary,
  TokenUsageStats,
  TokenUsageAgentStats,
  TokenUsageSessionStats,
} from "../../../api/types/tokenUsage";
import { formatCompact } from "../../../utils/formatNumber";
import { LoadingState, EmptyState } from "./components";
import { PageHeader } from "@/components/PageHeader";
import { useAppMessage } from "../../../hooks/useAppMessage";
import styles from "./index.module.less";

type ByModelRow = TokenUsageStats & { key: string };
type ByDateRow = TokenUsageStats & { key: string; date: string };
type ByAgentRow = TokenUsageAgentStats & { key: string };
type BySessionRow = TokenUsageSessionStats & { key: string; sessionId: string };

// ─── Overview Tab ────────────────────────────────────────────────────────────
function OverviewTab({
  data,
  loading,
  startDate,
  endDate,
  onDateChange,
  onRefresh,
}: {
  data: TokenUsageSummary | null;
  loading: boolean;
  startDate: Dayjs;
  endDate: Dayjs;
  onDateChange: (dates: [Dayjs | null, Dayjs | null] | null) => void;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();

  const byModelDataSource: ByModelRow[] = useMemo(() => {
    if (!data?.by_model) return [];
    return Object.entries(data.by_model).map(([key, stats]) => ({
      ...stats,
      key,
    }));
  }, [data?.by_model]);

  const byDateDataSource: ByDateRow[] = useMemo(() => {
    if (!data?.by_date) return [];
    return Object.entries(data.by_date)
      .map(([dt, stats]) => ({ ...stats, key: dt, date: dt }))
      .sort((a, b) => b.date.localeCompare(a.date));
  }, [data?.by_date]);

  const byModelColumns: ColumnsType<ByModelRow> = useMemo(
    () => [
      {
        title: t("tokenUsage.provider"),
        dataIndex: "provider_id",
        key: "provider_id",
        render: (v: string) => v ?? "",
      },
      {
        title: t("tokenUsage.model"),
        dataIndex: "model",
        key: "model",
        render: (v: string, r) => v ?? r.key,
      },
      {
        title: t("tokenUsage.promptTokens"),
        dataIndex: "prompt_tokens",
        key: "prompt_tokens",
        render: (n: number) => formatCompact(n),
      },
      {
        title: t("tokenUsage.completionTokens"),
        dataIndex: "completion_tokens",
        key: "completion_tokens",
        render: (n: number) => formatCompact(n),
      },
      {
        title: t("tokenUsage.totalCalls"),
        dataIndex: "call_count",
        key: "call_count",
        render: (n: number) => formatCompact(n),
      },
    ],
    [t],
  );

  const byDateColumns: ColumnsType<ByDateRow> = useMemo(
    () => [
      { title: t("tokenUsage.date"), dataIndex: "date", key: "date" },
      {
        title: t("tokenUsage.promptTokens"),
        dataIndex: "prompt_tokens",
        key: "prompt_tokens",
        render: (n: number) => formatCompact(n),
      },
      {
        title: t("tokenUsage.completionTokens"),
        dataIndex: "completion_tokens",
        key: "completion_tokens",
        render: (n: number) => formatCompact(n),
      },
      {
        title: t("tokenUsage.totalCalls"),
        dataIndex: "call_count",
        key: "call_count",
        render: (n: number) => formatCompact(n),
      },
    ],
    [t],
  );

  return (
    <>
      <div className={styles.filters}>
        <DatePicker.RangePicker
          value={[startDate, endDate]}
          onChange={onDateChange}
          className={styles.datePicker}
        />
        <Button type="primary" onClick={onRefresh} loading={loading}>
          {t("tokenUsage.refresh")}
        </Button>
      </div>

      {data && data.total_calls > 0 ? (
        <>
          <div className={styles.summaryCards}>
            <Card className={styles.card}>
              <div className={styles.cardValue}>
                {formatCompact(
                  data.total_prompt_tokens + data.total_completion_tokens,
                )}
              </div>
              <div className={styles.cardLabel}>
                {t("tokenUsage.totalTokens")}
              </div>
            </Card>
            <Card className={styles.card}>
              <div className={styles.cardValue}>
                {formatCompact(data.total_prompt_tokens)}
              </div>
              <div className={styles.cardLabel}>
                {t("tokenUsage.promptTokens")}
              </div>
            </Card>
            <Card className={styles.card}>
              <div className={styles.cardValue}>
                {formatCompact(data.total_completion_tokens)}
              </div>
              <div className={styles.cardLabel}>
                {t("tokenUsage.completionTokens")}
              </div>
            </Card>
            <Card className={styles.card}>
              <div className={styles.cardValue}>
                {formatCompact(data.total_calls)}
              </div>
              <div className={styles.cardLabel}>
                {t("tokenUsage.totalCalls")}
              </div>
            </Card>
          </div>

          {byModelDataSource.length > 0 && (
            <Card
              className={styles.tableCard}
              title={t("tokenUsage.byModel")}
              bodyStyle={{ padding: 0 }}
            >
              <Table<ByModelRow>
                columns={byModelColumns}
                dataSource={byModelDataSource}
                rowKey="key"
                pagination={false}
              />
            </Card>
          )}

          {byDateDataSource.length > 0 && (
            <Card
              className={styles.tableCard}
              title={t("tokenUsage.byDate")}
              bodyStyle={{ padding: 0 }}
            >
              <Table<ByDateRow>
                columns={byDateColumns}
                dataSource={byDateDataSource}
                rowKey="key"
                pagination={{ pageSize: 14, hideOnSinglePage: true }}
              />
            </Card>
          )}
        </>
      ) : (
        <EmptyState message={t("tokenUsage.noData")} />
      )}
    </>
  );
}

// ─── Agents Tab ───────────────────────────────────────────────────────────────
function AgentsTab({
  data,
  loading,
}: {
  data: TokenUsageSummary | null;
  loading: boolean;
}) {
  const { t } = useTranslation();

  const dataSource: ByAgentRow[] = useMemo(() => {
    if (!data?.by_agent) return [];
    return Object.entries(data.by_agent)
      .map(([agentId, stats]) => ({ ...stats, key: agentId }))
      .sort((a, b) => b.prompt_tokens + b.completion_tokens - (a.prompt_tokens + a.completion_tokens));
  }, [data?.by_agent]);

  const columns: ColumnsType<ByAgentRow> = useMemo(
    () => [
      {
        title: t("tokenUsage.agentId"),
        dataIndex: "agent_id",
        key: "agent_id",
        render: (v: string) => <span className={styles.monoCell}>{v}</span>,
      },
      {
        title: t("tokenUsage.promptTokens"),
        dataIndex: "prompt_tokens",
        key: "prompt_tokens",
        render: (n: number) => formatCompact(n),
        sorter: (a, b) => a.prompt_tokens - b.prompt_tokens,
      },
      {
        title: t("tokenUsage.completionTokens"),
        dataIndex: "completion_tokens",
        key: "completion_tokens",
        render: (n: number) => formatCompact(n),
        sorter: (a, b) => a.completion_tokens - b.completion_tokens,
      },
      {
        title: t("tokenUsage.totalCalls"),
        dataIndex: "call_count",
        key: "call_count",
        render: (n: number) => formatCompact(n),
        sorter: (a, b) => a.call_count - b.call_count,
      },
    ],
    [t],
  );

  if (!loading && (!data || dataSource.length === 0)) {
    return <EmptyState message={t("tokenUsage.noAgentData")} />;
  }

  return (
    <Card
      className={styles.tableCard}
      title={t("tokenUsage.byAgent")}
      bodyStyle={{ padding: 0 }}
    >
      <Table<ByAgentRow>
        columns={columns}
        dataSource={dataSource}
        rowKey="key"
        loading={loading}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
      />
    </Card>
  );
}

// ─── Sessions Tab ─────────────────────────────────────────────────────────────
function SessionsTab({
  data,
  loading,
}: {
  data: TokenUsageSummary | null;
  loading: boolean;
}) {
  const { t } = useTranslation();

  const dataSource: BySessionRow[] = useMemo(() => {
    if (!data?.by_session) return [];
    return Object.entries(data.by_session)
      .map(([sessionId, stats]) => ({ ...stats, key: sessionId, sessionId }))
      .sort((a, b) => {
        const ta = a.last_updated ?? "";
        const tb = b.last_updated ?? "";
        return tb.localeCompare(ta);
      });
  }, [data?.by_session]);

  const columns: ColumnsType<BySessionRow> = useMemo(
    () => [
      {
        title: t("tokenUsage.sessionId"),
        dataIndex: "sessionId",
        key: "sessionId",
        ellipsis: true,
        render: (v: string) => (
          <span className={styles.monoCell} title={v}>
            {v.length > 20 ? `${v.slice(0, 8)}…${v.slice(-8)}` : v}
          </span>
        ),
      },
      {
        title: t("tokenUsage.agentId"),
        dataIndex: "agent_id",
        key: "agent_id",
        render: (v: string) => v || "—",
      },
      {
        title: t("tokenUsage.promptTokens"),
        dataIndex: "prompt_tokens",
        key: "prompt_tokens",
        render: (n: number) => formatCompact(n),
        sorter: (a, b) => a.prompt_tokens - b.prompt_tokens,
      },
      {
        title: t("tokenUsage.completionTokens"),
        dataIndex: "completion_tokens",
        key: "completion_tokens",
        render: (n: number) => formatCompact(n),
        sorter: (a, b) => a.completion_tokens - b.completion_tokens,
      },
      {
        title: t("tokenUsage.totalCalls"),
        dataIndex: "call_count",
        key: "call_count",
        render: (n: number) => formatCompact(n),
        sorter: (a, b) => a.call_count - b.call_count,
      },
      {
        title: t("tokenUsage.lastUpdated"),
        dataIndex: "last_updated",
        key: "last_updated",
        render: (v: string) =>
          v ? new Date(v).toLocaleString() : "—",
        sorter: (a, b) =>
          (a.last_updated ?? "").localeCompare(b.last_updated ?? ""),
      },
    ],
    [t],
  );

  if (!loading && (!data || dataSource.length === 0)) {
    return <EmptyState message={t("tokenUsage.noSessionData")} />;
  }

  return (
    <Card
      className={styles.tableCard}
      title={t("tokenUsage.bySession")}
      bodyStyle={{ padding: 0 }}
    >
      <Table<BySessionRow>
        columns={columns}
        dataSource={dataSource}
        rowKey="key"
        loading={loading}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
      />
    </Card>
  );
}

// ─── Root Page ────────────────────────────────────────────────────────────────
function TokenUsagePage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<TokenUsageSummary | null>(null);
  const [startDate, setStartDate] = useState<Dayjs>(dayjs().subtract(30, "day"));
  const [endDate, setEndDate] = useState<Dayjs>(dayjs());
  const [activeTab, setActiveTab] = useState("overview");

  const fetchData = async () => {
    setLoading(true);
    setError(null);
    try {
      const summary = await api.getTokenUsage({
        start_date: startDate.format("YYYY-MM-DD"),
        end_date: endDate.format("YYYY-MM-DD"),
      });
      setData(summary);
    } catch (e) {
      console.error("Failed to load token usage:", e);
      const msg = t("tokenUsage.loadFailed");
      message.error(msg);
      setError(msg);
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, []);

  const handleDateChange = (dates: [Dayjs | null, Dayjs | null] | null) => {
    if (dates?.[0]) setStartDate(dates[0]);
    if (dates?.[1]) setEndDate(dates[1]);
  };

  const tabItems = [
    {
      key: "overview",
      label: t("tokenUsage.tabOverview"),
      children: (
        <OverviewTab
          data={data}
          loading={loading}
          startDate={startDate}
          endDate={endDate}
          onDateChange={handleDateChange}
          onRefresh={fetchData}
        />
      ),
    },
    {
      key: "agents",
      label: t("tokenUsage.tabAgents"),
      children: <AgentsTab data={data} loading={loading} />,
    },
    {
      key: "sessions",
      label: t("tokenUsage.tabSessions"),
      children: <SessionsTab data={data} loading={loading} />,
    },
  ];

  return (
    <div className={styles.tokenUsagePage}>
      <PageHeader parent={t("nav.settings")} current={t("tokenUsage.title")} />
      <div className={styles.content}>
        {loading && !data ? (
          <LoadingState
            message={error ?? t("common.loading")}
            error={!!error}
            onRetry={error ? fetchData : undefined}
          />
        ) : (
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            className={styles.tabs}
            items={tabItems}
          />
        )}
      </div>
    </div>
  );
}

export default TokenUsagePage;
