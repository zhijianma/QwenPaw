import React from "react";
import { BulbOutlined } from "@ant-design/icons";
import type { ThinkingContent } from "../types";
import styles from "./MessageList.module.less";

interface ThinkingBlockProps {
  content: ThinkingContent;
  isStreaming: boolean;
}

const ThinkingBlock: React.FC<ThinkingBlockProps> = ({
  content,
  isStreaming,
}) => {
  const isThinking = isStreaming && !content.collapsed;

  // Thinking in progress: open, show content
  // Thinking done: collapsed
  return (
    <details
      className={`${styles.toolCallCompact} ${
        isThinking ? styles.toolCallCompactLoading : ""
      }`}
      open={isThinking}
    >
      <summary className={`${styles.toolCallCompactSummary} ${styles.hasIcon}`}>
        {isThinking ? (
          <span className={styles.toolCallSpinner} />
        ) : (
          <span
            className={`${styles.toolCallIcon} ${styles.toolCallIconSuccess}`}
          >
            <BulbOutlined />
          </span>
        )}
        <span className={styles.toolCallLabel}>
          {isThinking ? "思考中" : "完成思考"}
        </span>
      </summary>
      <div className={styles.thinkingContent}>{content.text || "..."}</div>
    </details>
  );
};

export default ThinkingBlock;
