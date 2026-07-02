import React from "react";
import { Popover } from "antd";
import { SparkBarChartLine } from "@agentscope-ai/icons";
import { IconButton } from "@agentscope-ai/design";
import { useTranslation } from "react-i18next";
import { readTurnUsageFromResponseCardData } from "../turnUsage";
import { formatCompact } from "../../../utils/formatNumber";

interface TurnUsageActionProps {
  data: { data?: Record<string, unknown> };
}

const TurnUsageAction: React.FC<TurnUsageActionProps> = ({ data }) => {
  const { t } = useTranslation();

  const snapshot = readTurnUsageFromResponseCardData(data?.data ?? null);
  if (!snapshot?.usage) return null;

  const usage = snapshot.usage;
  const prompt = usage.prompt_tokens || 0;
  const completion = usage.completion_tokens || 0;
  const total = usage.total_tokens || prompt + completion;

  return (
    <Popover
      trigger={["hover", "click"]}
      mouseEnterDelay={0.15}
      content={
        <div
          style={{
            width: 200,
            fontSize: 13,
            lineHeight: 1.6,
          }}
        >
          {usage.model_name && (
            <div
              style={{
                fontWeight: 600,
                marginBottom: 6,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {usage.model_name}
            </div>
          )}
          {[
            {
              color: "#1890ff",
              label: t("chat.turnUsagePopover.input", "Input"),
              value: prompt,
            },
            {
              color: "#722ed1",
              label: t("chat.turnUsagePopover.output", "Output"),
              value: completion,
            },
          ].map((row) => (
            <div
              key={row.label}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "2px 0",
              }}
            >
              <span
                style={{
                  display: "flex",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    display: "inline-block",
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    marginRight: 8,
                    backgroundColor: row.color,
                  }}
                />
                {row.label}
              </span>
              <span
                style={{
                  fontVariantNumeric: "tabular-nums",
                  fontWeight: 500,
                }}
              >
                {formatCompact(row.value)}
              </span>
            </div>
          ))}
          <div
            style={{
              borderTop: "1px solid rgba(0,0,0,0.06)",
              marginTop: 4,
              paddingTop: 4,
              display: "flex",
              justifyContent: "space-between",
              fontWeight: 600,
            }}
          >
            <span>{t("chat.turnUsagePopover.total", "Total")}</span>
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {formatCompact(total)}
            </span>
          </div>
        </div>
      }
    >
      <IconButton
        bordered={false}
        size="small"
        icon={<SparkBarChartLine />}
        title={t("chat.turnUsagePopover.ariaLabel")}
      />
    </Popover>
  );
};

export default TurnUsageAction;
