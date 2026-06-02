import { Card, Tag } from "antd";
import styles from "../index.module.less";

interface CardBlockProps {
  block: {
    title?: string;
    content?: string;
    tags?: string[];
    image?: string;
  };
}

export default function CardBlock({ block }: CardBlockProps) {
  return (
    <Card className={styles.card} size="small" hoverable>
      {block.image && (
        <img src={block.image} alt="" className={styles.cardImage} />
      )}
      {block.title && (
        <Card.Meta title={block.title} description={block.content} />
      )}
      {block.tags?.length ? (
        <div style={{ marginTop: 8 }}>
          {block.tags.map((tag, i) => (
            <Tag key={i}>{tag}</Tag>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
