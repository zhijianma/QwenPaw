import { Statistic, Tag } from "antd";
import { ArrowUpOutlined, ArrowDownOutlined } from "@ant-design/icons";
import styles from "../index.module.less";

interface StatItem {
  label: string;
  value: string | number;
  prefix?: string;
  suffix?: string;
  trend?: "up" | "down";
  trendValue?: string;
}

interface StatBlockProps {
  block: {
    stats?: StatItem[];
  };
}

export default function StatBlock({ block }: StatBlockProps) {
  if (!block.stats?.length) return null;

  return (
    <div className={styles.statBlock}>
      {block.stats.map((stat, i) => (
        <div key={i} className={styles.statItem}>
          <Statistic
            title={stat.label}
            value={stat.value}
            prefix={stat.prefix}
            suffix={stat.suffix}
          />
          {stat.trend && stat.trendValue && (
            <Tag
              color={stat.trend === "up" ? "green" : "red"}
              icon={stat.trend === "up" ? <ArrowUpOutlined /> : <ArrowDownOutlined />}
              className={styles.statTrend}
            >
              {stat.trendValue}
            </Tag>
          )}
        </div>
      ))}
    </div>
  );
}
