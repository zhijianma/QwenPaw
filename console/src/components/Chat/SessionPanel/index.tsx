import React, { useCallback, useState } from "react";
import { Button, Modal } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { useChatSessions } from "../hooks/useChatSessions";
import SessionItem from "./SessionItem";
import SessionSearch from "./SessionSearch";
import SessionGroup from "./SessionGroup";
import styles from "./SessionPanel.module.less";

export interface SessionPanelProps {
  /** Whether the panel is collapsed */
  collapsed?: boolean;
  /** Toggle collapse */
  onToggleCollapse?: () => void;
  /** Custom header */
  header?: React.ReactNode;
}

const SessionPanel: React.FC<SessionPanelProps> = ({
  collapsed = false,
  header,
}) => {
  const {
    sessions,
    groupedSessions,
    loading,
    activeSessionId,
    searchQuery,
    selectSession,
    createSession,
    deleteSession,
    renameSession,
    pinSession,
    setSearchQuery,
  } = useChatSessions();

  const [editingId, setEditingId] = useState<string | null>(null);

  const handleCreate = useCallback(async () => {
    await createSession();
  }, [createSession]);

  const handleDelete = useCallback(
    async (id: string) => {
      Modal.confirm({
        title: "Delete conversation?",
        content: "This action cannot be undone.",
        okText: "Delete",
        okType: "danger",
        onOk: () => deleteSession(id),
      });
    },
    [deleteSession],
  );

  const handleRename = useCallback(
    async (id: string, name: string) => {
      await renameSession(id, name);
      setEditingId(null);
    },
    [renameSession],
  );

  const handlePin = useCallback(
    async (id: string, pinned: boolean) => {
      await pinSession(id, pinned);
    },
    [pinSession],
  );

  if (collapsed) {
    return null;
  }

  return (
    <div className={styles.sessionPanel}>
      {header || (
        <div className={styles.panelHeader}>
          <span className={styles.panelTitle}>Conversations</span>
          <Button
            type="text"
            size="small"
            icon={<PlusOutlined />}
            onClick={handleCreate}
          />
        </div>
      )}

      <SessionSearch value={searchQuery} onChange={setSearchQuery} />

      <div className={styles.sessionList}>
        {loading && sessions.length === 0 && (
          <div className={styles.loadingState}>Loading...</div>
        )}
        {!loading && sessions.length === 0 && (
          <div className={styles.emptyState}>No conversations yet</div>
        )}
        {searchQuery.trim()
          ? // Flat list when searching
            sessions.map((session) => (
              <SessionItem
                key={session.id}
                session={session}
                isActive={session.id === activeSessionId}
                isEditing={session.id === editingId}
                onClick={() => selectSession(session.id)}
                onDelete={() => handleDelete(session.id)}
                onRename={(name) => handleRename(session.id, name)}
                onPin={() => handlePin(session.id, !session.pinned)}
                onStartEdit={() => setEditingId(session.id)}
                onCancelEdit={() => setEditingId(null)}
              />
            ))
          : // Grouped by date
            groupedSessions.map((group) => (
              <SessionGroup
                key={group.label}
                group={group}
                activeSessionId={activeSessionId}
                editingId={editingId}
                onSelect={selectSession}
                onDelete={handleDelete}
                onRename={handleRename}
                onPin={handlePin}
                onStartEdit={setEditingId}
                onCancelEdit={() => setEditingId(null)}
              />
            ))}
      </div>
    </div>
  );
};

export default SessionPanel;
