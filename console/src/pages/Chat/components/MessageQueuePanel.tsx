import { useState, useCallback, useRef } from "react";
import { Input } from "antd";
import { useTheme } from "../../../contexts/ThemeContext";

export interface QueueItem {
  id: string;
  text: string;
}

let _nextQueueId = 0;
export function nextQueueId(): string {
  return "mq-" + Date.now().toString(36) + "-" + (++_nextQueueId).toString(36);
}

interface MessageQueuePanelProps {
  items: QueueItem[];
  onRemove: (id: string) => void;
  onEdit: (id: string, text: string) => void;
  onReorder: (items: QueueItem[]) => void;
  onInterruptAndSend: (item: QueueItem) => void;
  onClear: () => void;
}

export default function MessageQueuePanel({
  items,
  onRemove,
  onEdit,
  onReorder,
  onInterruptAndSend,
  onClear,
}: MessageQueuePanelProps) {
  const { isDark } = useTheme();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [dragOverId, setDragOverId] = useState<string | null>(null);
  const dragItemRef = useRef<string | null>(null);

  const startEdit = useCallback((item: QueueItem) => {
    setEditingId(item.id);
    setEditText(item.text);
  }, []);

  const confirmEdit = useCallback(() => {
    if (editingId && editText.trim()) {
      onEdit(editingId, editText.trim());
    }
    setEditingId(null);
    setEditText("");
  }, [editingId, editText, onEdit]);

  const cancelEdit = useCallback(() => {
    setEditingId(null);
    setEditText("");
  }, []);

  const handleDragStart = useCallback((id: string) => {
    dragItemRef.current = id;
  }, []);

  const dragOverIdRef = useRef<string | null>(null);
  const handleDragOver = useCallback(
    (e: React.DragEvent, id: string) => {
      e.preventDefault();
      if (
        dragItemRef.current &&
        dragItemRef.current !== id &&
        dragOverIdRef.current !== id
      ) {
        dragOverIdRef.current = id;
        setDragOverId(id);
      }
    },
    [],
  );

  const handleDrop = useCallback(
    (targetId: string) => {
      const fromId = dragItemRef.current;
      if (!fromId || fromId === targetId) {
        setDragOverId(null);
        return;
      }
      const fromIdx = items.findIndex((it) => it.id === fromId);
      const toIdx = items.findIndex((it) => it.id === targetId);
      if (fromIdx === -1 || toIdx === -1) {
        setDragOverId(null);
        return;
      }
      const next = [...items];
      const [moved] = next.splice(fromIdx, 1);
      next.splice(toIdx, 0, moved);
      onReorder(next);
      setDragOverId(null);
      dragItemRef.current = null;
      dragOverIdRef.current = null;
    },
    [items, onReorder],
  );

  const handleDragEnd = useCallback(() => {
    setDragOverId(null);
    dragItemRef.current = null;
    dragOverIdRef.current = null;
  }, []);

  if (items.length === 0) return null;

  const muted = isDark ? "#888" : "#999";
  const rowBg = isDark ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.02)";
  const dragOverBg = isDark ? "rgba(255,255,255,0.1)" : "rgba(0,0,0,0.06)";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 2,
        padding: "6px 12px",
        maxHeight: 200,
        overflowY: "auto",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 2,
        }}
      >
        <span style={{ fontSize: 11, color: muted, userSelect: "none" }}>
          Queue ({items.length})
        </span>
        {items.length > 1 && (
          <span
            onClick={onClear}
            style={{
              fontSize: 11,
              color: muted,
              cursor: "pointer",
              userSelect: "none",
            }}
          >
            清空
          </span>
        )}
      </div>

      {items.map((item, idx) => (
        <div
          key={item.id}
          draggable={editingId !== item.id}
          onDragStart={() => handleDragStart(item.id)}
          onDragOver={(e) => handleDragOver(e, item.id)}
          onDrop={() => handleDrop(item.id)}
          onDragEnd={handleDragEnd}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "3px 6px",
            borderRadius: 4,
            fontSize: 12,
            background: dragOverId === item.id ? dragOverBg : rowBg,
            cursor: editingId === item.id ? "default" : "grab",
            transition: "background 0.15s",
          }}
        >
          <span
            style={{
              color: muted,
              fontSize: 11,
              minWidth: 16,
              userSelect: "none",
            }}
          >
            {idx + 1}.
          </span>

          {editingId === item.id ? (
            <Input
              size="small"
              autoFocus
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onPressEnter={confirmEdit}
              onBlur={confirmEdit}
              onKeyDown={(e) => {
                if (e.key === "Escape") cancelEdit();
              }}
              style={{ flex: 1, fontSize: 12 }}
            />
          ) : (
            <>
              <span
                style={{
                  flex: 1,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  color: isDark ? "#ddd" : "#333",
                  cursor: "text",
                }}
                onClick={() => startEdit(item)}
              >
                {item.text}
              </span>

              <span
                onClick={() => onInterruptAndSend(item)}
                style={{
                  cursor: "pointer",
                  fontSize: 11,
                  opacity: 0.5,
                  flexShrink: 0,
                  whiteSpace: "nowrap",
                }}
              >
                ⚡打断发送
              </span>

              <span
                onClick={() => onRemove(item.id)}
                style={{
                  cursor: "pointer",
                  fontSize: 13,
                  opacity: 0.4,
                  flexShrink: 0,
                  lineHeight: 1,
                }}
                title="删除"
              >
                ×
              </span>
            </>
          )}
        </div>
      ))}
    </div>
  );
}
