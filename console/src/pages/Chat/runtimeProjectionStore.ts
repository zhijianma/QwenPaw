import { useCallback, useSyncExternalStore } from "react";

import { chatApi } from "../../api/modules/chat";
import { streamRuntimeProjection } from "../../api/runtimeProjectionStream";
import type { ConversationRuntimeProjection } from "../../api/types";

const RECONNECT_DELAY_MS = 500;

interface RuntimeProjectionEntry {
  agentId: string;
  chatId: string;
  controller: AbortController;
  listeners: Set<() => void>;
  projection: ConversationRuntimeProjection | null;
  hadPendingRuntime: boolean;
  settledGeneration: number;
}

interface RuntimeProjectionSubscription {
  active: boolean;
  agentId: string;
  chatId?: string;
}

const entries = new Map<string, RuntimeProjectionEntry>();

function projectionKey(agentId: string, chatId: string): string {
  return `${agentId}\u0000${chatId}`;
}

function publish(
  entry: RuntimeProjectionEntry,
  projection: ConversationRuntimeProjection,
): void {
  const pending = projection.queue.submissions.some((submission) =>
    ["queued", "admitted", "running", "interrupting"].includes(
      submission.status,
    ),
  );
  if (entry.hadPendingRuntime && !pending) {
    entry.settledGeneration += 1;
  }
  entry.hadPendingRuntime = pending;
  entry.projection = projection;
  for (const listener of entry.listeners) listener();
}

async function follow(entry: RuntimeProjectionEntry): Promise<void> {
  let cursor: string | undefined;
  const { controller } = entry;
  while (!controller.signal.aborted) {
    try {
      cursor = await streamRuntimeProjection({
        chatId: entry.chatId,
        agentId: entry.agentId,
        afterCursor: cursor,
        signal: controller.signal,
        onSnapshot: (projection) => publish(entry, projection),
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      console.warn("[ChatRuntime] runtime stream failed", error);
      try {
        const projection = await chatApi.getRuntime(entry.chatId, {
          agentId: entry.agentId,
          signal: controller.signal,
        });
        cursor = projection.cursor;
        publish(entry, projection);
      } catch (fallbackError) {
        if (!controller.signal.aborted) {
          console.warn("[ChatRuntime] failed to refresh", fallbackError);
        }
      }
    }
    if (!controller.signal.aborted) {
      await new Promise((resolve) => setTimeout(resolve, RECONNECT_DELAY_MS));
    }
  }
}

function subscribe(
  agentId: string,
  chatId: string,
  listener: () => void,
): () => void {
  const key = projectionKey(agentId, chatId);
  let entry = entries.get(key);
  let created = false;
  if (!entry) {
    entry = {
      agentId,
      chatId,
      controller: new AbortController(),
      listeners: new Set(),
      projection: null,
      hadPendingRuntime: false,
      settledGeneration: 0,
    };
    entries.set(key, entry);
    created = true;
  }
  entry.listeners.add(listener);
  if (created) void follow(entry);
  return () => {
    entry?.listeners.delete(listener);
    if (entry?.listeners.size === 0) {
      entry.controller.abort();
      entries.delete(key);
    }
  };
}

function snapshot(
  agentId: string,
  chatId?: string,
): ConversationRuntimeProjection | null {
  if (!chatId) return null;
  return entries.get(projectionKey(agentId, chatId))?.projection ?? null;
}

export function useConversationRuntimeProjection({
  active,
  agentId,
  chatId,
}: RuntimeProjectionSubscription) {
  const enabled = active && Boolean(agentId && chatId);
  const projection = useSyncExternalStore(
    useCallback(
      (listener) =>
        enabled && chatId
          ? subscribe(agentId, chatId, listener)
          : () => undefined,
      [agentId, chatId, enabled],
    ),
    useCallback(
      () => (enabled ? snapshot(agentId, chatId) : null),
      [agentId, chatId, enabled],
    ),
    () => null,
  );

  const refresh = useCallback(async () => {
    if (!enabled || !chatId) return null;
    const next = await chatApi.getRuntime(chatId, { agentId });
    const entry = entries.get(projectionKey(agentId, chatId));
    if (entry) publish(entry, next);
    return next;
  }, [agentId, chatId, enabled]);

  const settledGeneration =
    enabled && chatId
      ? entries.get(projectionKey(agentId, chatId))?.settledGeneration ?? 0
      : 0;

  return { projection, refresh, settledGeneration };
}

export function resetRuntimeProjectionStoreForTests(): void {
  for (const entry of entries.values()) entry.controller.abort();
  entries.clear();
}
