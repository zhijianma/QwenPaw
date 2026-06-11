import { Alert } from "antd";
import styles from "../index.module.less";

interface AlertBlockProps {
  block: {
    message: string;
    description?: string;
    alertType?: "info" | "success" | "warning" | "error";
    showIcon?: boolean;
  };
}

export default function AlertBlock({ block }: AlertBlockProps) {
  return (
    <div className={styles.alertBlock}>
      <Alert
        message={block.message}
        description={block.description}
        type={block.alertType || "info"}
        showIcon={block.showIcon !== false}
      />
    </div>
  );
}
