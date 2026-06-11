import styles from "../index.module.less";

interface VideoBlockProps {
  block: { url?: string; title?: string; poster?: string; width?: number };
}

export default function VideoBlock({ block }: VideoBlockProps) {
  if (!block.url) return null;
  return (
    <div className={styles.videoBlock}>
      {block.title && <div className={styles.mediaTitle}>{block.title}</div>}
      <video
        controls
        preload="metadata"
        poster={block.poster}
        className={styles.videoPlayer}
        style={block.width ? { maxWidth: block.width } : undefined}
      >
        <source src={block.url} />
      </video>
    </div>
  );
}
