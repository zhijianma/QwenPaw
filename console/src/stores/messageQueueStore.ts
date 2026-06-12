import { create } from "zustand";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type QueueItemStatus = "pending" | "sending" | "failed" | "sent";

export type QueueRunState = "idle" | "running" | "paused" | "error";

/** Attachment reference (URL only, binary not stored in queue) */
export interface QueueAttachment {
  url: string;
  name?: string;
  type?: string;
  size?: number;
}

/** Image reference */
export interface QueueImage {
  url: string;
  thumbUrl?: string;
}

/** Mention reference */
export interface QueueMention {
  id: string;
  name: string;
}

/** Quote reference */
export interface QueueQuote {
  messageId: string;
  text: string;
}

/** Full message body for a queued item (Phase 3 ready) */
export interface QueueItem {
  id: string;
  text: string;
  attachments?: QueueAttachment[];
  images?: QueueImage[];
  mentions?: QueueMention[];
  quote?: QueueQuote;
  status: QueueItemStatus;
  retryCount: number;
  errorMessage?: string;
  createdAt: number;
}

/** Data required to enqueue a new item */
export interface QueueItemInput {
  text: string;
  attachments?: QueueAttachment[];
  images?: QueueImage[];
  mentions?: QueueMention[];
  quote?: QueueQuote;
}

// ---------------------------------------------------------------------------
// Storage helpers
// ---------------------------------------------------------------------------

const STORAGE_PREFIX = "qwenpaw:message-queue:";

/** Shape persisted in sessionStorage per session */
interface PersistedQueue {
  items: QueueItem[];
  runState: QueueRunState;
}

function getStorageKey(sessionId: string): string {
  return `${STORAGE_PREFIX}${sessionId}`;
}

function readQueueFromStorage(sessionId: string): PersistedQueue | null {
  try {
    const saved = sessionStorage.getItem(getStorageKey(sessionId));
    if (saved) {
      const parsed = JSON.parse(saved);
      // Backward compat: old format was QueueItem[]
      if (Array.isArray(parsed)) {
        return { items: parsed as QueueItem[], runState: "idle" };
      }
      return parsed as PersistedQueue;
    }
  } catch {
    // ignore
  }
  return null;
}

function writeQueueToStorage(
  sessionId: string,
  items: QueueItem[],
  runState: QueueRunState,
) {
  try {
    if (items.length > 0) {
      sessionStorage.setItem(
        getStorageKey(sessionId),
        JSON.stringify({ items, runState }),
      );
    } else {
      sessionStorage.removeItem(getStorageKey(sessionId));
    }
  } catch {
    // ignore storage errors
  }
}

export function removeQueueFromStorage(sessionId: string) {
  try {
    sessionStorage.removeItem(getStorageKey(sessionId));
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// ID generator
// ---------------------------------------------------------------------------

let _nextQueueId = 0;
export function nextQueueId(): string {
  return "mq-" + Date.now().toString(36) + "-" + (++_nextQueueId).toString(36);
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

interface MessageQueueStore {
  /** All session queues: sessionId -> items */
  queues: Record<string, QueueItem[]>;
  /** Currently active session ID */
  activeSessionId: string | null;
  /** Overall queue run state */
  runState: QueueRunState;
  /** Currently sending item ID */
  currentSendingId: string | null;

  // Actions
  setActiveSessionId: (sessionId: string | null) => void;
  enqueue: (sessionId: string, input: QueueItemInput) => void;
  remove: (sessionId: string, id: string) => void;
  edit: (sessionId: string, id: string, text: string) => void;
  reorder: (sessionId: string, items: QueueItem[]) => void;
  clear: (sessionId: string) => void;
  setItemStatus: (
    sessionId: string,
    id: string,
    status: QueueItemStatus,
    errorMessage?: string,
  ) => void;
  setRunState: (state: QueueRunState) => void;
  setCurrentSendingId: (id: string | null) => void;
  /** Get queue for a session (read-only) */
  getQueue: (sessionId: string) => QueueItem[];
  /** Persist queue to sessionStorage */
  persistToStorage: (sessionId: string) => void;
  /** Load queue from sessionStorage into memory */
  loadFromStorage: (sessionId: string) => void;
}

/** Maximum number of items allowed in a single session queue */
export const MAX_QUEUE_SIZE = 50;

export const useMessageQueueStore = create<MessageQueueStore>((set, get) => ({
  queues: {},
  activeSessionId: null,
  runState: "idle",
  currentSendingId: null,

  setActiveSessionId: (sessionId: string | null) => {
    set({ activeSessionId: sessionId });
  },

  enqueue: (sessionId: string, input: QueueItemInput) => {
    const current = get().queues[sessionId] ?? [];
    if (current.length >= MAX_QUEUE_SIZE) {
      // Queue is full, reject
      return;
    }
    const item: QueueItem = {
      id: nextQueueId(),
      text: input.text,
      attachments: input.attachments,
      images: input.images,
      mentions: input.mentions,
      quote: input.quote,
      status: "pending",
      retryCount: 0,
      createdAt: Date.now(),
    };
    set((state) => {
      const nextItems = [...current, item];
      const next = { ...state.queues, [sessionId]: nextItems };
      // Persist immediately
      writeQueueToStorage(sessionId, nextItems, get().runState);
      return { queues: next };
    });
  },

  remove: (sessionId: string, id: string) => {
    set((state) => {
      const current = state.queues[sessionId] ?? [];
      const nextItems = current.filter((it) => it.id !== id);
      const next = { ...state.queues, [sessionId]: nextItems };
      writeQueueToStorage(sessionId, nextItems, get().runState);
      return { queues: next };
    });
  },

  edit: (sessionId: string, id: string, text: string) => {
    set((state) => {
      const current = state.queues[sessionId] ?? [];
      const nextItems = current.map((it) =>
        it.id === id ? { ...it, text } : it,
      );
      const next = { ...state.queues, [sessionId]: nextItems };
      writeQueueToStorage(sessionId, nextItems, get().runState);
      return { queues: next };
    });
  },

  reorder: (sessionId: string, items: QueueItem[]) => {
    set((state) => {
      const next = { ...state.queues, [sessionId]: items };
      writeQueueToStorage(sessionId, items, get().runState);
      return { queues: next };
    });
  },

  clear: (sessionId: string) => {
    set((state) => {
      const next = { ...state.queues };
      delete next[sessionId];
      writeQueueToStorage(sessionId, [], get().runState);
      return { queues: next };
    });
  },

  setItemStatus: (
    sessionId: string,
    id: string,
    status: QueueItemStatus,
    errorMessage?: string,
  ) => {
    set((state) => {
      const current = state.queues[sessionId] ?? [];
      const nextItems = current.map((it) =>
        it.id === id
          ? {
              ...it,
              status,
              errorMessage,
              retryCount: it.retryCount + (status === "failed" ? 1 : 0),
            }
          : it,
      );
      const next = { ...state.queues, [sessionId]: nextItems };
      writeQueueToStorage(sessionId, nextItems, get().runState);
      return { queues: next };
    });
  },

  setRunState: (runState: QueueRunState) => {
    set({ runState });
    // Persist runState together with the active session queue
    const sid = get().activeSessionId;
    if (sid) {
      const items = get().queues[sid] ?? [];
      writeQueueToStorage(sid, items, runState);
    }
  },

  setCurrentSendingId: (currentSendingId: string | null) => {
    set({ currentSendingId });
  },

  getQueue: (sessionId: string) => {
    return get().queues[sessionId] ?? [];
  },

  persistToStorage: (sessionId: string) => {
    const items = get().queues[sessionId] ?? [];
    writeQueueToStorage(sessionId, items, get().runState);
  },

  loadFromStorage: (sessionId: string) => {
    const saved = readQueueFromStorage(sessionId);
    if (saved) {
      set((state) => ({
        queues: { ...state.queues, [sessionId]: saved.items },
      }));
      // Restore runState only if it was paused (avoid auto-send after refresh)
      if (saved.runState === "paused") {
        set({ runState: "paused" });
      }
    }
  },
}));
