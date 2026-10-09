import { Card } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import { formatCompact } from "../../../../utils/formatNumber";
import { cacheHitRate, formatPercent } from "../../../../utils/cacheUsage";
import styles from "../index.module.less";

interface SummaryCardsProps {
  totalCalls: number;
  totalPromptTokens: number;
  totalCompletionTokens: number;
  totalCacheReadTokens: number;
  totalCacheEligibleInputTokens: number;
  contextUsageRatio: number | null;
  maxContextUsageRatio: number | null;
  nearCompactionCalls: number;
  totalCostMicros: number;
  costUnknownCalls: number;
}

export function SummaryCards({
  totalCalls,
  totalPromptTokens,
  totalCompletionTokens,
  totalCacheReadTokens,
  totalCacheEligibleInputTokens,
  contextUsageRatio,
  maxContextUsageRatio,
  nearCompactionCalls,
  totalCostMicros,
  costUnknownCalls,
}: SummaryCardsProps) {
  const { t } = useTranslation();
  const hitRate = cacheHitRate(
    totalCacheReadTokens,
    totalCacheEligibleInputTokens,
  );

  return (
    <div className={styles.summaryCards}>
      <Card className={styles.card}>
        <div className={styles.cardValue}>{formatCompact(totalCalls)}</div>
        <div className={styles.cardLabel}>{t("tokenUsage.totalCalls")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatPercent(contextUsageRatio)}
        </div>
        <div className={styles.cardLabel}>{t("tokenUsage.contextUsage")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatPercent(maxContextUsageRatio)}
        </div>
        <div className={styles.cardLabel}>
          {t("tokenUsage.maxContextUsage")}
        </div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatCompact(nearCompactionCalls)}
        </div>
        <div className={styles.cardLabel}>{t("tokenUsage.nearCompaction")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatCompact(totalCostMicros)} µ
        </div>
        <div className={styles.cardLabel}>{t("tokenUsage.reportedCost")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatCompact(costUnknownCalls)}
        </div>
        <div className={styles.cardLabel}>{t("tokenUsage.costUnknown")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatCompact(totalPromptTokens)}
        </div>
        <div className={styles.cardLabel}>{t("tokenUsage.promptTokens")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatCompact(totalCacheReadTokens)}
        </div>
        <div className={styles.cardLabel}>{t("tokenUsage.cacheRead")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>{formatPercent(hitRate)}</div>
        <div className={styles.cardLabel}>{t("tokenUsage.cacheHitRate")}</div>
      </Card>
      <Card className={styles.card}>
        <div className={styles.cardValue}>
          {formatCompact(totalCompletionTokens)}
        </div>
        <div className={styles.cardLabel}>
          {t("tokenUsage.completionTokens")}
        </div>
      </Card>
    </div>
  );
}
