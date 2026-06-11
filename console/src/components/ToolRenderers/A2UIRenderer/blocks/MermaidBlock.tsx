import { MermaidCodeBlock } from "@/components/MermaidCodeBlock/MermaidCodeBlock";
import styles from "../index.module.less";

interface MermaidBlockProps {
  block: {
    code?: string;
    title?: string;
  };
}

export default function MermaidBlock({ block }: MermaidBlockProps) {
  if (!block.code) return null;

  return (
    <div className={styles.mermaidBlockWrap}>
      {block.title && <div className={styles.mermaidBlockTitle}>{block.title}</div>}
      <MermaidCodeBlock chart={block.code} />
    </div>
  );
}
