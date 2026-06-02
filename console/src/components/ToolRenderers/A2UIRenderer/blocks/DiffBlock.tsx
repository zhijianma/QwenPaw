import { DiffEditor } from "@monaco-editor/react";
import { useTheme } from "@/contexts/ThemeContext";
import styles from "../index.module.less";

interface DiffBlockProps {
  block: {
    file?: string;
    language?: string;
    old_content?: string;
    new_content?: string;
  };
}

export default function DiffBlock({ block }: DiffBlockProps) {
  const { isDark } = useTheme();
  const original = block.old_content ?? "";
  const modified = block.new_content ?? "";
  const lineCount = Math.max(
    original.split("\n").length,
    modified.split("\n").length,
  );
  const height = Math.min(Math.max(lineCount * 20 + 20, 80), 400);

  return (
    <div className={styles.diffBlock}>
      {block.file && <div className={styles.diffHeader}>{block.file}</div>}
      <DiffEditor
        height={height}
        language={block.language || "plaintext"}
        original={original}
        modified={modified}
        theme={isDark ? "vs-dark" : "light"}
        options={{
          readOnly: true,
          renderSideBySide: true,
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
          lineNumbers: "on",
          folding: false,
          fontSize: 13,
        }}
      />
    </div>
  );
}
