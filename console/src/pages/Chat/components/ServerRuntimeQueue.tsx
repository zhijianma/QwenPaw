import { Button, Tooltip } from "antd";
import { GripVertical, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { chatApi } from "../../../api/modules/chat";
import type {
  ConversationRuntimeProjection,
  TurnSubmission,
} from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useTheme } from "../../../contexts/ThemeContext";
import { useConversationRuntimeProjection } from "../runtimeProjectionStore";

interface ServerRuntimeQueueProps {
  active: boolean;
  agentId: string;
  chatId?: string;
  onSettled?: () => void | Promise<void>;
}

const STANDALONE_QUEUE_GRACE_MS = 500;

function queuedSubmissions(
  projection: ConversationRuntimeProjection | null,
): TurnSubmission[] {
  return (projection?.queue.submissions ?? [])
    .filter((submission) => submission.status === "queued")
    .sort((left, right) => left.queue_position - right.queue_position);
}

export default function ServerRuntimeQueue({
  active,
  agentId,
  chatId,
  onSettled,
}: ServerRuntimeQueueProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { isDark } = useTheme();
  const { projection, refresh, settledGeneration } =
    useConversationRuntimeProjection({
      active,
      agentId,
      chatId,
    });
  const [busy, setBusy] = useState(false);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const settledCallbackRef = useRef(onSettled);
  settledCallbackRef.current = onSettled;
  const observedSettlementRef = useRef(settledGeneration);

  useEffect(() => {
    if (settledGeneration > observedSettlementRef.current) {
      void settledCallbackRef.current?.();
    }
    observedSettlementRef.current = settledGeneration;
  }, [settledGeneration]);

  const items = useMemo(() => queuedSubmissions(projection), [projection]);
  const hasActiveSubmission = Boolean(
    projection?.queue.active_submission_id,
  );
  const standaloneQueueKey = items
    .map((item) => item.submission_id)
    .join("\u0000");
  const [visibleStandaloneQueueKey, setVisibleStandaloneQueueKey] = useState<
    string | null
  >(null);

  useEffect(() => {
    if (items.length === 0 || hasActiveSubmission) {
      setVisibleStandaloneQueueKey(null);
      return undefined;
    }

    const timer = window.setTimeout(() => {
      setVisibleStandaloneQueueKey(standaloneQueueKey);
    }, STANDALONE_QUEUE_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, [hasActiveSubmission, items.length, standaloneQueueKey]);

  const cancel = useCallback(
    async (submission: TurnSubmission) => {
      if (!chatId || !projection || busy) return;
      setBusy(true);
      try {
        await chatApi.cancelQueued(
          chatId,
          submission.submission_id,
          {
            idempotency_key: crypto.randomUUID(),
            expected_revision: projection.queue.revision,
          },
          agentId,
        );
        await refresh();
      } catch (error) {
        message.error(
          error instanceof Error ? error.message : t("chat.queue.sendFailed"),
        );
      } finally {
        setBusy(false);
      }
    },
    [agentId, busy, chatId, message, projection, refresh, t],
  );

  const reorder = useCallback(
    async (sourceId: string, targetId: string) => {
      if (!chatId || !projection || busy || sourceId === targetId) return;
      const sourceIndex = items.findIndex(
        (item) => item.submission_id === sourceId,
      );
      const targetIndex = items.findIndex(
        (item) => item.submission_id === targetId,
      );
      if (sourceIndex < 0 || targetIndex < 0) return;
      const next = [...items];
      const [moved] = next.splice(sourceIndex, 1);
      next.splice(targetIndex, 0, moved);
      setBusy(true);
      try {
        await chatApi.reorderQueue(
          chatId,
          {
            idempotency_key: crypto.randomUUID(),
            expected_revision: projection.queue.revision,
            ordered_submission_ids: next.map((item) => item.submission_id),
          },
          agentId,
        );
        await refresh();
      } catch (error) {
        message.error(
          error instanceof Error ? error.message : t("chat.queue.sendFailed"),
        );
      } finally {
        setBusy(false);
        setDraggedId(null);
      }
    },
    [agentId, busy, chatId, items, message, projection, refresh, t],
  );

  if (
    items.length === 0 ||
    (!hasActiveSubmission &&
      visibleStandaloneQueueKey !== standaloneQueueKey)
  ) {
    return null;
  }

  const border = isDark ? "rgba(255,255,255,0.10)" : "rgba(31,42,35,0.10)";
  const surface = isDark ? "rgba(255,255,255,0.035)" : "rgba(247,249,246,0.96)";
  const row = isDark ? "rgba(255,255,255,0.045)" : "rgba(255,255,255,0.92)";

  return (
    <section
      aria-label={t("chat.queue.title")}
      style={{
        border: `1px solid ${border}`,
        borderRadius: 10,
        background: surface,
        padding: 8,
      }}
    >
      <div
        style={{
          color: isDark ? "#d8ddd8" : "#344038",
          fontSize: 12,
          fontWeight: 600,
          margin: "0 4px 6px",
        }}
      >
        {t("chat.queue.title")} · {items.length}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        {items.map((item) => (
          <div
            key={item.submission_id}
            draggable={!busy}
            onDragStart={() => setDraggedId(item.submission_id)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => {
              if (draggedId) void reorder(draggedId, item.submission_id);
            }}
            onDragEnd={() => setDraggedId(null)}
            style={{
              alignItems: "center",
              background: row,
              border: `1px solid ${border}`,
              borderRadius: 8,
              display: "flex",
              gap: 7,
              minHeight: 34,
              padding: "5px 7px",
              opacity: busy ? 0.65 : 1,
            }}
          >
            <GripVertical
              aria-hidden
              size={15}
              color={isDark ? "#879087" : "#8b958d"}
            />
            <span
              title={item.content}
              style={{
                color: isDark ? "#e3e8e3" : "#263129",
                flex: 1,
                fontSize: 12,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {item.content}
            </span>
            <Tooltip title={t("chat.queue.delete")}>
              <Button
                aria-label={t("chat.queue.delete")}
                disabled={busy}
                icon={<Trash2 size={14} />}
                onClick={() => void cancel(item)}
                size="small"
                type="text"
              />
            </Tooltip>
          </div>
        ))}
      </div>
    </section>
  );
}
