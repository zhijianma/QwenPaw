import styles from "../index.module.less";

interface FallbackBlockProps {
  block: Record<string, unknown>;
}

export default function FallbackBlock({ block }: FallbackBlockProps) {
  return (
    <div className={styles.fallbackBlock}>
      <span>
        Unknown block type: <code>{String(block.type)}</code>
      </span>
      <pre>{JSON.stringify(block, null, 2)}</pre>
    </div>
  );
}
