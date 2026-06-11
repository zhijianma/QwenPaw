import styles from "../index.module.less";

interface AudioBlockProps {
  block: { url?: string; title?: string };
}

export default function AudioBlock({ block }: AudioBlockProps) {
  if (!block.url) return null;
  return (
    <div className={styles.audioBlock}>
      {block.title && <div className={styles.mediaTitle}>{block.title}</div>}
      <audio controls preload="metadata" className={styles.audioPlayer}>
        <source src={block.url} />
      </audio>
    </div>
  );
}
