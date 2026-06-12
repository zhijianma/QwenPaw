import { useState, useCallback, useRef } from "react";
import { Input } from "antd";
import {
  SparkDragDotLine,
  SparkEditLine,
  SparkSendLine,
  SparkDeleteLine,
} from "@agentscope-ai/icons";
import { useTheme } from "../../../contexts/ThemeContext";
import {
  type QueueItem,
  type QueueRunState,
} from "../../../stores/messageQueueStore";

export type { QueueItem };

interface MessageQueuePanelProps {
  items: QueueItem[];
  runState: QueueRunState;
  onRemove: (id: string) => void;
  onEdit: (id: string, text: string) => void;
  onReorder: (items: QueueItem[]) => void;
  onInterruptAndSend: (item: QueueItem) => void;
  onClear: () => void;
  onPauseResume: () => void;
  onRetry: (id: string) => void;
  onSkip: (id: string) => void;
}

export default function MessageQueuePanel({
  items,
  runState,
  onRemove,
  onEdit,
  onReorder,
  onInterruptAndSend,
  onClear,
  onPauseResume,
  onRetry,
  onSkip,
}: MessageQueuePanelProps) {
  const { isDark } = useTheme();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [dragOverId, setDragOverId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
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
  const handleDragOver = useCallback((e: React.DragEvent, id: string) => {
    e.preventDefault();
    if (
      dragItemRef.current &&
      dragItemRef.current !== id &&
      dragOverIdRef.current !== id
    ) {
      dragOverIdRef.current = id;
      setDragOverId(id);
    }
  }, []);

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
  const hoverBg = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.05)";
  const dragOverBg = isDark ? "rgba(255,255,255,0.1)" : "rgba(0,0,0,0.06)";
  const borderColor = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 3,
        padding: "8px 12px",
        maxHeight: 220,
        overflowY: "auto",
        borderRadius: 8,
        background: isDark ? "rgba(255,255,255,0.02)" : "rgba(0,0,0,0.01)",
        border: `1px solid ${borderColor}`,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 4,
          position: "sticky",
          top: 0,
          zIndex: 1,
          background: isDark ? "#1f1f1f" : "#fff",
          paddingTop: 4,
          paddingBottom: 6,
          borderBottom: `1px solid ${borderColor}`,
        }}
      >
        <span
          style={{
            fontSize: 12,
            fontWeight: 500,
            color: isDark ? "#bbb" : "#555",
            userSelect: "none",
          }}
        >
          消息队列 ({items.length})
          {runState === "paused" && (
            <span
              style={{
                marginLeft: 8,
                fontSize: 11,
                color: "#faad14",
                fontWeight: 400,
              }}
            >
              ⏸ 已暂停
            </span>
          )}
          {runState === "error" && (
            <span
              style={{
                marginLeft: 8,
                fontSize: 11,
                color: "#ff4d4f",
                fontWeight: 400,
              }}
            >
              ⚠ 发送失败
            </span>
          )}
        </span>
        <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span
            onClick={onPauseResume}
            style={{
              fontSize: 11,
              fontWeight: 500,
              color:
                runState === "paused" || runState === "error"
                  ? "#52c41a"
                  : "#faad14",
              cursor: "pointer",
              userSelect: "none",
              padding: "2px 6px",
              borderRadius: 4,
              background:
                runState === "paused" || runState === "error"
                  ? "rgba(82,196,26,0.1)"
                  : "rgba(250,173,20,0.1)",
            }}
            title={
              runState === "paused" || runState === "error"
                ? "继续发送队列"
                : "暂停发送队列"
            }
          >
            {runState === "paused" || runState === "error"
              ? "▶ 继续"
              : "⏸ 暂停"}
          </span>
          {items.length > 1 && (
            <span
              onClick={onClear}
              style={{
                fontSize: 11,
                color: muted,
                cursor: "pointer",
                userSelect: "none",
                padding: "2px 6px",
                borderRadius: 4,
              }}
              title="清空队列"
            >
              清空
            </span>
          )}
        </span>
      </div>

      {items.map((item) => {
        const statusColor =
          item.status === "failed"
            ? "#ff4d4f"
            : item.status === "sending"
            ? "#1890ff"
            : "#52c41a";
        return (
          <div
            key={item.id}
            draggable={editingId !== item.id && item.status !== "sending"}
            onDragStart={() => handleDragStart(item.id)}
            onDragOver={(e) => handleDragOver(e, item.id)}
            onDrop={() => handleDrop(item.id)}
            onDragEnd={handleDragEnd}
            onMouseEnter={() => setHoveredId(item.id)}
            onMouseLeave={() =>
              setHoveredId((prev) => (prev === item.id ? null : prev))
            }
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "5px 10px",
              borderRadius: 6,
              fontSize: 12,
              background:
                dragOverId === item.id
                  ? dragOverBg
                  : hoveredId === item.id
                  ? hoverBg
                  : rowBg,
              cursor: editingId === item.id ? "default" : "grab",
              transition: "background 0.15s ease",
              position: "relative",
              overflow: "hidden",
            }}
          >
            {/* Status indicator bar */}
            <div
              style={{
                position: "absolute",
                left: 0,
                top: 6,
                bottom: 6,
                width: 3,
                borderRadius: 2,
                background: statusColor,
                opacity: 0.8,
              }}
            />

            {/* Drag handle */}
            <span
              style={{
                color: muted,
                fontSize: 10,
                minWidth: 20,
                userSelect: "none",
                cursor: "grab",
                paddingLeft: 4,
                display: "inline-flex",
                alignItems: "center",
                gap: 2,
              }}
              title="拖动排序"
            >
              <SparkDragDotLine style={{ fontSize: 12, flexShrink: 0 }} />
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
                    color:
                      item.status === "failed"
                        ? "#ff4d4f"
                        : item.status === "sending"
                        ? "#1890ff"
                        : isDark
                        ? "#ddd"
                        : "#333",
                    fontWeight: item.status === "sending" ? 500 : 400,
                  }}
                >
                  {item.text}
                  {item.errorMessage && (
                    <span
                      style={{
                        fontSize: 10,
                        color: "#ff4d4f",
                        marginLeft: 4,
                      }}
                    >
                      ({item.errorMessage})
                    </span>
                  )}
                </span>

                {/* Attachment previews */}
                {(item.attachments?.length || 0) > 0 && (
                  <span
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 4,
                      marginLeft: 6,
                      flexShrink: 0,
                    }}
                  >
                    {item.attachments!.map((att, ai) => {
                      const isImage = att.type?.startsWith("image/");
                      return (
                        <span
                          key={ai}
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            gap: 3,
                            padding: "2px 5px",
                            borderRadius: 4,
                            background: isDark
                              ? "rgba(255,255,255,0.06)"
                              : "rgba(0,0,0,0.04)",
                            fontSize: 10,
                            color: muted,
                            maxWidth: 120,
                            overflow: "hidden",
                            whiteSpace: "nowrap" as const,
                          }}
                          title={att.name || att.url}
                        >
                          {isImage ? (
                            <img
                              src={att.url}
                              alt={att.name || "image"}
                              style={{
                                width: 18,
                                height: 18,
                                objectFit: "cover" as const,
                                borderRadius: 3,
                                flexShrink: 0,
                              }}
                            />
                          ) : (
                            <span style={{ flexShrink: 0 }}>📎</span>
                          )}
                          <span
                            style={{
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {att.name || "文件"}
                          </span>
                        </span>
                      );
                    })}
                  </span>
                )}

                {/* Action buttons */}
                <span
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    marginLeft: 8,
                    flexShrink: 0,
                  }}
                >
                  {hoveredId === item.id ? (
                    <>
                      <span
                        onClick={() => startEdit(item)}
                        style={{
                          cursor: "pointer",
                          fontSize: 12,
                          opacity: 0.7,
                          whiteSpace: "nowrap",
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          width: 20,
                          height: 20,
                          borderRadius: 4,
                        }}
                        title="编辑"
                      >
                        <SparkEditLine style={{ fontSize: 14 }} />
                      </span>

                      {item.status === "failed" && (
                        <>
                          <span
                            onClick={() => onRetry(item.id)}
                            style={{
                              cursor: "pointer",
                              fontSize: 13,
                              color: "#1890ff",
                              whiteSpace: "nowrap",
                              display: "inline-flex",
                              alignItems: "center",
                              justifyContent: "center",
                              width: 20,
                              height: 20,
                              borderRadius: 4,
                            }}
                            title="重试"
                          >
                            ↻
                          </span>
                          <span
                            onClick={() => onSkip(item.id)}
                            style={{
                              cursor: "pointer",
                              fontSize: 13,
                              color: muted,
                              whiteSpace: "nowrap",
                              display: "inline-flex",
                              alignItems: "center",
                              justifyContent: "center",
                              width: 20,
                              height: 20,
                              borderRadius: 4,
                            }}
                            title="跳过"
                          >
                            ⏭
                          </span>
                        </>
                      )}

                      <span
                        onClick={() => onInterruptAndSend(item)}
                        style={{
                          cursor: "pointer",
                          fontSize: 12,
                          opacity: 0.7,
                          whiteSpace: "nowrap",
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          width: 20,
                          height: 20,
                          borderRadius: 4,
                        }}
                        title="打断并发送"
                      >
                        <SparkSendLine style={{ fontSize: 14 }} />
                      </span>

                      <span
                        onClick={() => onRemove(item.id)}
                        style={{
                          cursor: "pointer",
                          fontSize: 14,
                          opacity: 0.6,
                          lineHeight: 1,
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          width: 20,
                          height: 20,
                          borderRadius: 4,
                        }}
                        title="删除"
                      >
                        <SparkDeleteLine style={{ fontSize: 14 }} />
                      </span>
                    </>
                  ) : (
                    /* Hidden placeholders to keep layout stable */
                    <>
                      <span
                        style={{
                          display: "inline-flex",
                          width: 20,
                          height: 20,
                        }}
                      />
                      {item.status === "failed" && (
                        <>
                          <span
                            style={{
                              display: "inline-flex",
                              width: 20,
                              height: 20,
                            }}
                          />
                          <span
                            style={{
                              display: "inline-flex",
                              width: 20,
                              height: 20,
                            }}
                          />
                        </>
                      )}
                      <span
                        style={{
                          display: "inline-flex",
                          width: 20,
                          height: 20,
                        }}
                      />
                      <span
                        style={{
                          display: "inline-flex",
                          width: 20,
                          height: 20,
                        }}
                      />
                    </>
                  )}
                </span>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
