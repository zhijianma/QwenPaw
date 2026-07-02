import React from "react";
import { Popover, Progress, Button, Space } from "antd";
import { PlusCircleOutlined, CompressOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { formatCompact } from "../../../utils/formatNumber";
import type { ContextUsage } from "../turnUsage";

const RING_SIZE = 18;
const RING_STROKE = 3;
const RING_R = (RING_SIZE - RING_STROKE) / 2;
const RING_CIRC = 2 * Math.PI * RING_R;

function ringColor(ratio: number): string {
  if (ratio >= 95) return "#cf1322";
  if (ratio >= 85) return "#f5222d";
  if (ratio >= 75) return "#fa8c16";
  if (ratio >= 50) return "#faad14";
  return "#52c41a";
}

function UsageRing({ ratio }: { ratio: number }) {
  const pct = Math.max(0, Math.min(ratio, 100));
  const cx = RING_SIZE / 2;
  const color = ringColor(pct);
  return (
    <svg width={RING_SIZE} height={RING_SIZE} aria-hidden>
      <circle
        cx={cx}
        cy={cx}
        r={RING_R}
        fill="none"
        stroke="currentColor"
        strokeOpacity={0.2}
        strokeWidth={RING_STROKE}
      />
      <circle
        cx={cx}
        cy={cx}
        r={RING_R}
        fill="none"
        stroke={color}
        strokeWidth={RING_STROKE}
        strokeDasharray={`${RING_CIRC} ${RING_CIRC}`}
        strokeDashoffset={RING_CIRC * (1 - pct / 100)}
        strokeLinecap="round"
        transform={`rotate(-90 ${cx} ${cx})`}
      />
    </svg>
  );
}

function PopoverContent({
  context,
  onNewChat,
  onCompact,
}: {
  context: ContextUsage;
  onNewChat: () => void;
  onCompact: () => void;
}) {
  const { t } = useTranslation();
  const ratio = Math.max(
    0,
    Math.min(Number(context.context_usage_ratio) || 0, 100),
  );
  const pctLabel =
    ratio > 0 && ratio < 1 ? `${ratio.toFixed(1)}%` : `${Math.round(ratio)}%`;

  return (
    <div style={{ width: 260, fontSize: 13, lineHeight: 1.6 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          marginBottom: 6,
        }}
      >
        <span style={{ fontWeight: 600 }}>
          {t("chat.contextIndicator.title", "Context")} {pctLabel}
        </span>
        <span style={{ opacity: 0.6, fontSize: 12 }}>
          {formatCompact(context.estimated_tokens)}/
          {formatCompact(context.max_input_length)}
        </span>
      </div>
      <Progress
        percent={ratio}
        showInfo={false}
        strokeColor={ringColor(ratio)}
        size="small"
        style={{ marginBottom: 12 }}
      />
      <Space size={8}>
        <Button size="small" icon={<PlusCircleOutlined />} onClick={onNewChat}>
          {t("chat.contextIndicator.newChat", "New Chat")}
        </Button>
        <Button size="small" icon={<CompressOutlined />} onClick={onCompact}>
          {t("chat.contextIndicator.compact", "Compact")}
        </Button>
      </Space>
    </div>
  );
}

interface ContextUsageIndicatorProps {
  context: ContextUsage | null;
  currentMaxInputLength?: number | null;
  onNewChat: () => void;
  onCompact: () => void;
}

const ContextUsageIndicator: React.FC<ContextUsageIndicatorProps> = ({
  context,
  currentMaxInputLength,
  onNewChat,
  onCompact,
}) => {
  if (!context) return null;

  if (!currentMaxInputLength || currentMaxInputLength <= 0) return null;

  const effectiveContext: ContextUsage = {
    ...context,
    max_input_length: currentMaxInputLength,
    context_usage_ratio: Math.min(
      (context.estimated_tokens / currentMaxInputLength) * 100,
      100,
    ),
  };

  const ratio = Math.max(
    0,
    Math.min(Number(effectiveContext.context_usage_ratio) || 0, 100),
  );

  return (
    <Popover
      trigger={["hover", "click"]}
      mouseEnterDelay={0.15}
      content={
        <PopoverContent
          context={effectiveContext}
          onNewChat={onNewChat}
          onCompact={onCompact}
        />
      }
    >
      <span
        role="button"
        tabIndex={0}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          cursor: "default",
          opacity: 0.75,
          fontSize: 11,
          lineHeight: "22px",
          padding: "0 4px",
        }}
      >
        <UsageRing ratio={ratio} />
        <span style={{ fontWeight: 500, color: ringColor(ratio) }}>
          {Math.round(ratio)}%
        </span>
      </span>
    </Popover>
  );
};

export default ContextUsageIndicator;
