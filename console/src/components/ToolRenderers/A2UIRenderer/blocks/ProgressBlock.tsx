import { Progress } from "antd";
import styles from "../index.module.less";

interface ProgressBlockProps {
  block: {
    label?: string;
    value?: number;
    max?: number;
    status?: "running" | "success" | "error";
  };
}

const STATUS_MAP: Record<string, "active" | "success" | "exception"> = {
  running: "active",
  success: "success",
  error: "exception",
};

export default function ProgressBlock({ block }: ProgressBlockProps) {
  const max = block.max || 100;
  const percent = Math.round(((block.value || 0) / max) * 100);
  const antStatus = STATUS_MAP[block.status || "running"] || "active";

  return (
    <div className={styles.progressBlock}>
      {block.label && (
        <span className={styles.progressLabel}>{block.label}</span>
      )}
      <Progress
        percent={percent}
        status={antStatus}
        style={{ flex: 1 }}
        size="small"
      />
    </div>
  );
}
