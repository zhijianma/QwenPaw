import styles from "../index.module.less";

interface CodeBlockProps {
  block: { content?: string; language?: string; filename?: string };
}

export default function CodeBlock({ block }: CodeBlockProps) {
  if (!block.content) return null;
  return (
    <div className={styles.codeBlock}>
      {block.filename && (
        <div className={styles.codeHeader}>{block.filename}</div>
      )}
      <pre className={styles.codePre}>
        <code>{block.content}</code>
      </pre>
    </div>
  );
}
