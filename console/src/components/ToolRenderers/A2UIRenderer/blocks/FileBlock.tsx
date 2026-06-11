import { Button } from "antd";
import { DownloadOutlined, FileOutlined } from "@ant-design/icons";
import styles from "../index.module.less";

interface FileBlockProps {
  block: { url?: string; filename?: string; size?: string };
}

export default function FileBlock({ block }: FileBlockProps) {
  if (!block.url) return null;
  const name = block.filename || block.url.split("/").pop() || "file";
  return (
    <div className={styles.fileBlock}>
      <FileOutlined className={styles.fileIcon} />
      <div className={styles.fileInfo}>
        <span className={styles.fileName}>{name}</span>
        {block.size && <span className={styles.fileSize}>{block.size}</span>}
      </div>
      <Button
        type="primary"
        size="small"
        icon={<DownloadOutlined />}
        href={block.url}
        target="_blank"
        rel="noopener noreferrer"
      >
        Download
      </Button>
    </div>
  );
}
