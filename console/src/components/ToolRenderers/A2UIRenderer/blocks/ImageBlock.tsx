import styles from "../index.module.less";

interface ImageBlockProps {
  block: { url?: string; alt?: string; width?: number };
}

export default function ImageBlock({ block }: ImageBlockProps) {
  if (!block.url) return null;
  return (
    <div className={styles.imageBlock}>
      <img
        src={block.url}
        alt={block.alt || ""}
        style={block.width ? { maxWidth: block.width } : undefined}
      />
    </div>
  );
}
