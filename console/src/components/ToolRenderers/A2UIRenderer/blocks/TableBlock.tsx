import { Table } from "antd";
import styles from "../index.module.less";

interface TableBlockProps {
  block: { headers?: string[]; rows?: string[][] };
}

export default function TableBlock({ block }: TableBlockProps) {
  const { headers, rows } = block;
  if (!headers?.length || !rows?.length) return null;

  const columns = headers.map((h, i) => ({
    title: h,
    dataIndex: String(i),
    key: String(i),
  }));

  const dataSource = rows.map((row, ri) => {
    const record: Record<string, string> = { key: String(ri) };
    row.forEach((cell, ci) => {
      record[String(ci)] = cell;
    });
    return record;
  });

  return (
    <div className={styles.tableBlock}>
      <Table
        columns={columns}
        dataSource={dataSource}
        pagination={false}
        size="small"
        bordered
      />
    </div>
  );
}
