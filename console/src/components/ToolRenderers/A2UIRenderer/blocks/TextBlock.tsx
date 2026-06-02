import Markdown from "@ant-design/x-markdown";
import styles from "../index.module.less";

interface TextBlockProps {
  block: { content?: string };
}

export default function TextBlock({ block }: TextBlockProps) {
  if (!block.content) return null;
  return (
    <div className={styles.textBlock}>
      <Markdown>{block.content}</Markdown>
    </div>
  );
}
