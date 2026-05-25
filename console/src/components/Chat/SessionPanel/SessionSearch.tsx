import React from "react";
import { Input } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import styles from "./SessionPanel.module.less";

interface SessionSearchProps {
  value: string;
  onChange: (value: string) => void;
}

const SessionSearch: React.FC<SessionSearchProps> = ({ value, onChange }) => {
  return (
    <div className={styles.searchContainer}>
      <Input
        prefix={<SearchOutlined />}
        placeholder="Search conversations..."
        value={value}
        onChange={(e) => onChange(e.target.value)}
        allowClear
        size="small"
        className={styles.searchInput}
      />
    </div>
  );
};

export default SessionSearch;
