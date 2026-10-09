import { Card, Table } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import { formatCompact } from "../../../../utils/formatNumber";
import { cacheHitRate, formatPercent } from "../../../../utils/cacheUsage";
import styles from "../index.module.less";

interface TokenRow {
  prompt_tokens: number;
  completion_tokens: number;
  cache_read_tokens: number;
  cache_eligible_input_tokens: number;
  context_usage_ratio: number | null;
  max_context_usage_ratio: number | null;
  near_compaction_calls: number;
  call_count: number;
}

interface ByModelData extends TokenRow {
  key: string;
  model: string;
}

interface ByDateData extends TokenRow {
  key: string;
  date: string;
}

interface ByAgentData extends TokenRow {
  key: string;
  agent: string;
}

interface ByChatData extends TokenRow {
  key: string;
  agent: string;
  chat: string;
}

interface ByTurnData extends TokenRow {
  key: string;
  agent: string;
  chat: string;
  turn: string;
}

interface DataTablesProps {
  byModelData: ByModelData[];
  byDateData: ByDateData[];
  byAgentData: ByAgentData[];
  byChatData: ByChatData[];
  byTurnData: ByTurnData[];
}

function tokenStatColumns<T extends TokenRow>(titles: {
  prompt: string;
  completion: string;
  total: string;
  cacheRead: string;
  cacheHitRate: string;
  contextUsage: string;
  maxContextUsage: string;
  nearCompaction: string;
  calls: string;
}) {
  return [
    {
      title: titles.prompt,
      dataIndex: "prompt_tokens",
      key: "prompt_tokens",
      render: (v: number) => formatCompact(v),
      sorter: (a: T, b: T) => a.prompt_tokens - b.prompt_tokens,
    },
    {
      title: titles.cacheRead,
      dataIndex: "cache_read_tokens",
      key: "cache_read_tokens",
      render: (v: number) => formatCompact(v),
      sorter: (a: T, b: T) => a.cache_read_tokens - b.cache_read_tokens,
    },
    {
      title: titles.cacheHitRate,
      key: "cache_hit_rate",
      render: (_: unknown, record: T) =>
        formatPercent(
          cacheHitRate(
            record.cache_read_tokens,
            record.cache_eligible_input_tokens,
          ),
        ),
      sorter: (a: T, b: T) =>
        (cacheHitRate(a.cache_read_tokens, a.cache_eligible_input_tokens) ??
          -1) -
        (cacheHitRate(b.cache_read_tokens, b.cache_eligible_input_tokens) ??
          -1),
    },
    {
      title: titles.contextUsage,
      dataIndex: "context_usage_ratio",
      key: "context_usage_ratio",
      render: (v: number | null) => formatPercent(v),
      sorter: (a: T, b: T) =>
        (a.context_usage_ratio ?? -1) - (b.context_usage_ratio ?? -1),
    },
    {
      title: titles.maxContextUsage,
      dataIndex: "max_context_usage_ratio",
      key: "max_context_usage_ratio",
      render: (v: number | null) => formatPercent(v),
      sorter: (a: T, b: T) =>
        (a.max_context_usage_ratio ?? -1) - (b.max_context_usage_ratio ?? -1),
    },
    {
      title: titles.nearCompaction,
      dataIndex: "near_compaction_calls",
      key: "near_compaction_calls",
      render: (v: number) => formatCompact(v),
      sorter: (a: T, b: T) => a.near_compaction_calls - b.near_compaction_calls,
    },
    {
      title: titles.completion,
      dataIndex: "completion_tokens",
      key: "completion_tokens",
      render: (v: number) => formatCompact(v),
      sorter: (a: T, b: T) => a.completion_tokens - b.completion_tokens,
    },
    {
      title: titles.total,
      key: "total_tokens",
      render: (_: unknown, record: T) =>
        formatCompact(record.prompt_tokens + record.completion_tokens),
      sorter: (a: T, b: T) =>
        a.prompt_tokens +
        a.completion_tokens -
        (b.prompt_tokens + b.completion_tokens),
    },
    {
      title: titles.calls,
      dataIndex: "call_count",
      key: "call_count",
      render: (v: number) => formatCompact(v),
      sorter: (a: T, b: T) => a.call_count - b.call_count,
    },
  ];
}

export function DataTables({
  byModelData,
  byDateData,
  byAgentData,
  byChatData,
  byTurnData,
}: DataTablesProps) {
  const { t } = useTranslation();
  const tokenTitles = {
    prompt: t("tokenUsage.promptTokens"),
    completion: t("tokenUsage.completionTokens"),
    total: t("tokenUsage.totalTokens"),
    cacheRead: t("tokenUsage.cacheRead"),
    cacheHitRate: t("tokenUsage.cacheHitRate"),
    contextUsage: t("tokenUsage.contextUsage"),
    maxContextUsage: t("tokenUsage.maxContextUsage"),
    nearCompaction: t("tokenUsage.nearCompaction"),
    calls: t("tokenUsage.totalCalls"),
  };

  return (
    <>
      {byModelData.length > 0 && (
        <Card
          className={`${styles.tableCard} mobile-scroll-x`}
          title={t("tokenUsage.byModel")}
        >
          <Table
            columns={[
              {
                title: t("tokenUsage.model"),
                dataIndex: "model",
                key: "model",
              },
              ...tokenStatColumns<ByModelData>(tokenTitles),
            ]}
            dataSource={byModelData}
            pagination={{ pageSize: 10 }}
            size="small"
            scroll={{ x: "max-content" }}
          />
        </Card>
      )}

      {byDateData.length > 0 && (
        <Card
          className={`${styles.tableCard} mobile-scroll-x`}
          title={t("tokenUsage.byDate")}
        >
          <Table
            columns={[
              { title: t("tokenUsage.date"), dataIndex: "date", key: "date" },
              ...tokenStatColumns<ByDateData>(tokenTitles),
            ]}
            dataSource={byDateData}
            pagination={{ pageSize: 10 }}
            size="small"
            scroll={{ x: "max-content" }}
          />
        </Card>
      )}

      {byAgentData.length > 0 && (
        <Card
          className={`${styles.tableCard} mobile-scroll-x`}
          title={t("tokenUsage.byAgent")}
        >
          <Table
            columns={[
              {
                title: t("tokenUsage.agent"),
                dataIndex: "agent",
                key: "agent",
              },
              ...tokenStatColumns<ByAgentData>(tokenTitles),
            ]}
            dataSource={byAgentData}
            pagination={{ pageSize: 10 }}
            size="small"
            scroll={{ x: "max-content" }}
          />
        </Card>
      )}

      {byChatData.length > 0 && (
        <Card
          className={`${styles.tableCard} mobile-scroll-x`}
          title={t("tokenUsage.byChat")}
        >
          <Table
            columns={[
              {
                title: t("tokenUsage.chat"),
                dataIndex: "chat",
                key: "chat",
              },
              {
                title: t("tokenUsage.agent"),
                dataIndex: "agent",
                key: "agent",
              },
              ...tokenStatColumns<ByChatData>(tokenTitles),
            ]}
            dataSource={byChatData}
            pagination={{ pageSize: 10 }}
            size="small"
            scroll={{ x: "max-content" }}
          />
        </Card>
      )}

      {byTurnData.length > 0 && (
        <Card
          className={`${styles.tableCard} mobile-scroll-x`}
          title={t("tokenUsage.byTurn")}
        >
          <Table
            columns={[
              {
                title: t("tokenUsage.turn"),
                dataIndex: "turn",
                key: "turn",
              },
              {
                title: t("tokenUsage.chat"),
                dataIndex: "chat",
                key: "chat",
              },
              {
                title: t("tokenUsage.agent"),
                dataIndex: "agent",
                key: "agent",
              },
              ...tokenStatColumns<ByTurnData>(tokenTitles),
            ]}
            dataSource={byTurnData}
            pagination={{ pageSize: 10 }}
            size="small"
            scroll={{ x: "max-content" }}
          />
        </Card>
      )}
    </>
  );
}
