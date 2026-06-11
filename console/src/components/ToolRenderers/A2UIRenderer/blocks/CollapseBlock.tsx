import { Collapse } from "antd";
import Markdown from "@ant-design/x-markdown";
import styles from "../index.module.less";

interface CollapseItem {
  title: string;
  content: string;
}

interface CollapseBlockProps {
  block: {
    items?: CollapseItem[];
    defaultOpen?: boolean;
  };
}

export default function CollapseBlock({ block }: CollapseBlockProps) {
  if (!block.items?.length) return null;

  const items = block.items.map((item, i) => ({
    key: String(i),
    label: item.title,
    children: (
      <div className={styles.textBlock}>
        <Markdown>{item.content}</Markdown>
      </div>
    ),
  }));

  return (
    <div className={styles.collapseBlock}>
      <Collapse
        items={items}
        defaultActiveKey={block.defaultOpen ? items.map((_, i) => String(i)) : []}
        bordered={false}
      />
    </div>
  );
}
