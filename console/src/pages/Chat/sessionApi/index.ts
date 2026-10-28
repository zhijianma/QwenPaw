import type {
  IAgentScopeRuntimeWebUICreateSessionResult,
  IAgentScopeRuntimeWebUISession,
  IAgentScopeRuntimeWebUISessionAPI,
  IAgentScopeRuntimeWebUIMessage,
} from "@agentscope-ai/chat";
import api, {
  type ChatSpec,
  type ChatHistory,
  type ChatStatus,
  type Message,
} from "../../../api";
import { ApiError } from "../../../api/request";
import { toDisplayUrl } from "../utils";
import { useAgentStore } from "../../../stores/agentStore";
import {
  extractTurnUsageFromOutputMessages,
  extractLatestSnapshotFromCards,
} from "../turnUsage";
import { useTurnUsageStore } from "../turnUsageStore";
import { extractClientMessageId } from "../../../utils/clientMessageId";
import { useMessageQueueStore } from "../../../stores/messageQueueStore";
import { syncSessionsGlobal } from "../../../stores/sessionListStore";
import {
  selectLoadedBackendMessages,
  selectWindowBackendMessages,
  type ChatHistoryOwner,
  useChatHistoryStore,
} from "../chatHistoryStore";
import {
  createTimelineWindowAtTurn,
  createTailTimelineWindow,
  extendTimelineWindowNewer,
  extendTimelineWindowOlder,
  timelineWindowCanExtendNewer,
  timelineWindowCanExtendOlder,
  TURN_PAGE_LIMIT,
  type ChatTimelineWindow,
} from "../chatTimelineWindow";
import {
  type LoadedChatBlockFragment,
  validateChatBlockFragment,
} from "../chatBlockFragments";
import { ChatLiveController } from "../chatLiveController";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DEFAULT_USER_ID = "default";
const DEFAULT_CHANNEL = "console";
const DEFAULT_SESSION_NAME = "New Chat";
const ROLE_TOOL = "tool";
const ROLE_USER = "user";
const ROLE_ASSISTANT = "assistant";
const TYPE_PLUGIN_CALL_OUTPUT = "plugin_call_output";
const CARD_RESPONSE = "AgentScopeRuntimeResponseCard";
const CHILD_PREFETCH_BYTE_LIMIT = 2 * 1024 * 1024;
const CHILD_PREFETCH_REQUEST_LIMIT = 8;

function hydrateTurnUsageFromMessages(
  messages: IAgentScopeRuntimeWebUIMessage[],
): void {
  useTurnUsageStore.getState().invalidateTurn();
  const snap = extractLatestSnapshotFromCards(messages);
  useTurnUsageStore.getState().setSnapshot(snap);
}

// ---------------------------------------------------------------------------
// Local helper types
// ---------------------------------------------------------------------------

/** A single item inside a message's content array. */
interface ContentItem {
  type: string;
  text?: string;
  [key: string]: unknown;
}

/** A backend message after role-normalisation (output of toOutputMessage). */
interface OutputMessage extends Omit<Message, "role"> {
  role: string;
  metadata: unknown;
  sequence_number?: number;
}

/**
 * Extended session carrying extra fields that the library type does not define
 * but our backend / window globals require.
 */
interface ExtendedSession extends IAgentScopeRuntimeWebUISession {
  /** Session identifier (channel:user_id format) */
  sessionId: string;
  /** User identifier */
  userId: string;
  /** Channel name */
  channel: string;
  /** Additional metadata */
  meta: Record<string, unknown>;
  /** Real backend UUID, used when id is overridden with a local timestamp. */
  realId?: string;
  /** Conversation status from backend. */
  status?: ChatStatus;
  /** ISO 8601 creation timestamp from backend. */
  createdAt?: string | null;
  /** ISO 8601 last-updated timestamp from backend. */
  updatedAt?: string | null;
  /** ISO 8601 completion time of the most recent task. */
  lastFinishedAt?: string | null;
  /** Whether the backend is still generating a response for this session. */
  generating?: boolean;
  /** Whether the chat is pinned to the top. */
  pinned?: boolean;
  /** Whether the chat is archived. */
  archived?: boolean;
  /** ISO 8601 archive timestamp from backend. */
  archivedAt?: string | null;
  source?: ChatSpec["source"];
  groupId?: string | null;
  parentSessionId?: string | null;
  rootSessionId?: string | null;
  /** Loaded from normalized chat_* pagination instead of legacy transcript. */
  historyV2?: boolean;
}

export interface HistoryLoadResult {
  messages: IAgentScopeRuntimeWebUIMessage[];
  noMore: boolean;
  replacedWindow: boolean;
  windowAtLimit: boolean;
  turnCount: number;
  atOlderLimit: boolean;
  atTail: boolean;
  targetBubbleId?: string;
}

export interface HistoryLoadOptions {
  extendOnly?: boolean;
}

export interface SessionIdentity {
  sdkSessionId: string;
  chatId?: string;
  sessionId: string;
  userId: string;
  channel: string;
}

// ---------------------------------------------------------------------------
// Message conversion helpers: backend flat messages → card-based UI format
// ---------------------------------------------------------------------------

/**
 * Cryptographically random base36 suffix for locally generated ids.
 * The ids are not secrets, but sourcing them from the CSPRNG avoids any
 * predictability concern and keeps static analysis clean.
 */
function randomBase36(length: number): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  let out = "";
  for (const byte of bytes) out += (byte % 36).toString(36);
  return out;
}

function generateId(): string {
  return `${Date.now()}-${randomBase36(9)}`;
}

/**
 * Parse a metadata time string (e.g. "2026-05-27 10:44:53.362") to unix
 * seconds; returns 0 when the value is absent or not parseable.
 */
const metadataTimeToSeconds = (ts: unknown): number => {
  if (!ts || typeof ts !== "string") return 0;
  const ms = new Date(ts.replace(" ", "T")).getTime();
  return Number.isNaN(ms) ? 0 : Math.floor(ms / 1000);
};

/** Parse metadata.timestamp string (e.g. "2026-05-27 10:44:53.362") to unix seconds. */
const parseTimestamp = (msg: Record<string, unknown>): number =>
  metadataTimeToSeconds((msg.metadata as Record<string, unknown>)?.timestamp);

/**
 * Parse metadata.finished_at string to unix seconds (0 when absent).
 * `finished_at` is stamped when the reply actually ended; `timestamp` is
 * the created_at alias pinned at the first saved segment, which can be far
 * earlier for turns with long tool calls.
 */
const parseFinishedAt = (msg: Record<string, unknown>): number =>
  metadataTimeToSeconds((msg.metadata as Record<string, unknown>)?.finished_at);

/** Extract plain text from a message's content array. */
const extractTextFromContent = (content: unknown): string => {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return String(content || "");
  return (content as ContentItem[])
    .filter((c) => c.type === "text")
    .map((c) => c.text || "")
    .filter(Boolean)
    .join("\n");
};

function resolveContentItemUrl(c: ContentItem): ContentItem {
  if (c.type === "image" && c.image_url) {
    return { ...c, image_url: toDisplayUrl(c.image_url as string) };
  }
  if (c.type === "audio" && c.data) {
    return { ...c, data: toDisplayUrl(c.data as string) };
  }
  if (c.type === "video" && c.video_url) {
    return { ...c, video_url: toDisplayUrl(c.video_url as string) };
  }
  if (c.type === "file" && (c.file_url || c.file_id)) {
    return {
      ...c,
      file_url: toDisplayUrl((c.file_url as string) || (c.file_id as string)),
      file_name: (c.filename as string) || (c.file_name as string) || "file",
    };
  }
  return c;
}

/** Map backend message content to request card content (text + image + file). */
function contentToRequestParts(
  content: unknown,
): Array<Record<string, unknown>> {
  if (typeof content === "string") {
    return [{ type: "text", text: content, status: "created" }];
  }
  if (!Array.isArray(content)) {
    return [{ type: "text", text: String(content || ""), status: "created" }];
  }
  const parts = (content as ContentItem[])
    .map(resolveContentItemUrl)
    .map((c) => ({ ...c, status: "created" }));

  if (parts.length === 0) {
    return [{ type: "text", text: "", status: "created" }];
  }

  return parts;
}
function normalizeOutputMessageContent(content: unknown): unknown {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return content;
  return (content as ContentItem[]).map((c) => {
    if (c.type === "file") {
      return {
        ...c,
        file_name: (c.filename as string) || (c.file_name as string) || "file",
      };
    }
    return c;
  });
}

/**
 * Convert a backend message to a response output message.
 * Maps system + plugin_call_output → role "tool" and strips metadata.
 */
const toOutputMessage = (msg: Message): OutputMessage => ({
  ...msg,
  role:
    msg.type === TYPE_PLUGIN_CALL_OUTPUT && msg.role === "system"
      ? ROLE_TOOL
      : msg.role,
  metadata: msg.metadata ?? null,
});

/** Build a user card (AgentScopeRuntimeRequestCard) from a user message. */
function buildUserCard(msg: Message): IAgentScopeRuntimeWebUIMessage {
  const contentParts = contentToRequestParts(msg.content);
  const chatTurnId =
    typeof msg.chat_turn_id === "string" ? msg.chat_turn_id : undefined;
  return {
    id: (msg.id as string) || generateId(),
    role: "user",
    cards: [
      {
        code: "AgentScopeRuntimeRequestCard",
        data: {
          created_at: parseTimestamp(msg),
          ...(chatTurnId ? { chat_turn_id: chatTurnId } : {}),
          input: [
            {
              role: "user",
              type: "message",
              content: contentParts,
              metadata: msg.metadata ?? null,
            },
          ],
        },
      },
    ],
  };
}

/**
 * Build an assistant response card (AgentScopeRuntimeResponseCard)
 * wrapping a group of consecutive non-user output messages.
 */
const buildResponseCard = (
  outputMessages: OutputMessage[],
  terminal?: { status?: string; error?: unknown },
  turnIdentity?: { turnId: string },
): IAgentScopeRuntimeWebUIMessage => {
  const fallbackNow = Math.floor(Date.now() / 1000);
  const maxSeq = outputMessages.reduce(
    (max, m) => Math.max(max, m.sequence_number || 0),
    0,
  );

  const firstTs = outputMessages.length ? parseTimestamp(outputMessages[0]) : 0;
  const lastTs = outputMessages.length
    ? parseTimestamp(outputMessages[outputMessages.length - 1])
    : 0;
  // Prefer the real reply-end time (finished_at) over timestamp so turns
  // with long tool calls show the true completion time (#6826). Falls
  // back to timestamp for legacy sessions without the stamp.
  const finishedAt = outputMessages.reduce(
    (max, m) => Math.max(max, parseFinishedAt(m)),
    0,
  );

  const normalizedMessages = outputMessages.map((msg) => ({
    ...msg,
    content: normalizeOutputMessageContent(msg.content),
  }));

  const turnUsage = extractTurnUsageFromOutputMessages(outputMessages);
  const generating =
    terminal === undefined &&
    outputMessages.some(
      (message) =>
        message.status === "in_progress" || message.status === "running",
    );
  const responseId = turnIdentity?.turnId
    ? `response_${turnIdentity.turnId}`
    : `response_${String(outputMessages[0]?.id ?? generateId())}`;

  return {
    id: responseId,
    role: ROLE_ASSISTANT,
    cards: [
      {
        code: CARD_RESPONSE,
        data: {
          id: responseId,
          output: normalizedMessages,
          object: "response",
          status:
            terminal?.status || (generating ? "in_progress" : "completed"),
          created_at: firstTs || fallbackNow,
          sequence_number: maxSeq + 1,
          error: terminal?.error ?? null,
          completed_at: finishedAt || lastTs || fallbackNow,
          usage: turnUsage?.usage ?? null,
          context_usage: turnUsage?.context_usage ?? null,
          ...(turnIdentity
            ? {
                chat_turn_id: turnIdentity.turnId,
              }
            : {}),
        },
      },
    ],
    msgStatus:
      terminal?.status === "canceled"
        ? "interrupted"
        : generating
        ? "generating"
        : "finished",
  };
};

/**
 * Convert flat backend messages into the card-based format expected by
 * the @agentscope-ai/chat component.
 *
 * - User messages → AgentScopeRuntimeRequestCard
 * - Consecutive non-user messages (assistant / system / tool) → grouped
 *   into a single AgentScopeRuntimeResponseCard with all output messages.
 */
const convertMessages = (
  messages: Message[],
): IAgentScopeRuntimeWebUIMessage[] => {
  const result: IAgentScopeRuntimeWebUIMessage[] = [];
  const len = messages.length;
  let i = 0;

  while (i < len) {
    let terminal: { status?: string; error?: unknown } | undefined;
    let turnIdentity: { turnId: string } | undefined;
    if (messages[i].role === ROLE_USER) {
      const user = messages[i++];
      if (typeof user.chat_turn_id === "string") {
        turnIdentity = {
          turnId: user.chat_turn_id,
        };
      }
      const meta = user.metadata as Record<string, unknown> | undefined;
      const nested = meta?.metadata;
      const terminalSource =
        nested && typeof nested === "object"
          ? (nested as Record<string, unknown>)
          : meta;
      const candidate = terminalSource?.qwenpaw_turn_state;
      terminal =
        candidate && typeof candidate === "object"
          ? (candidate as { status?: string; error?: unknown })
          : undefined;
      result.push(buildUserCard(user));
    }
    const startIdx = i;
    while (i < len && messages[i].role !== ROLE_USER) i++;
    const outputMsgs = messages.slice(startIdx, i).map(toOutputMessage);
    if (
      outputMsgs.length ||
      terminal?.status === "failed" ||
      terminal?.status === "canceled"
    ) {
      result.push(buildResponseCard(outputMsgs, terminal, turnIdentity));
    }
  }

  return result;
};

const chatSpecToSession = (chat: ChatSpec): ExtendedSession =>
  ({
    id: chat.id,
    name: chat.name || DEFAULT_SESSION_NAME,
    sessionId: chat.session_id,
    userId: chat.user_id,
    channel: chat.channel,
    messages: [],
    meta: chat.meta || {},
    status: chat.status ?? "idle",
    createdAt: chat.created_at ?? null,
    updatedAt: chat.updated_at ?? null,
    lastFinishedAt: chat.last_finished_at ?? null,
    pinned: chat.pinned ?? false,
    source: chat.source ?? "chat",
    groupId: chat.group_id ?? null,
    parentSessionId: chat.parent_session_id ?? null,
    rootSessionId: chat.root_session_id ?? null,
    archived: chat.archived ?? false,
    archivedAt: chat.archived_at ?? null,
  }) as ExtendedSession;

/** Returns true when id is a local session id (timestamp-random, not a backend UUID). */
const isLocalTimestamp = (id: string): boolean => /^\d+-[a-z0-9]+$/.test(id);

/** Detect if backend is still generating content for this chat.
 *  Only trust the explicit `status` field from the backend.
 *  When status is missing (undefined) treat the chat as idle to avoid
 *  false-positive reconnects that cause infinite loading (issue #4903).
 */
const isGenerating = (chatHistory: ChatHistory): boolean => {
  return chatHistory.status === "running";
};

/**
 * Resolve and persist the real backend UUID for a local timestamp session.
 * Stores the real UUID as realId while keeping the timestamp as id, so the
 * library's internal currentSessionId (timestamp) remains valid.
 * Returns the resolved real UUID, or null if not found.
 */
const resolveRealId = (
  sessionList: IAgentScopeRuntimeWebUISession[],
  tempSessionId: string,
): { list: IAgentScopeRuntimeWebUISession[]; realId: string | null } => {
  // 1) Local display entry already linked to a backend UUID.
  const alreadyResolved = sessionList.find(
    (s) => s.id === tempSessionId && (s as ExtendedSession).realId,
  ) as ExtendedSession | undefined;
  if (alreadyResolved?.realId) {
    return { list: sessionList, realId: alreadyResolved.realId };
  }

  // 2) Backend chat from listChats (UUID id + matching session_id).
  //    Skip the local placeholder whose id equals the timestamp — using that
  //    id as realId causes GET /api/chats/{timestamp} → 404.
  let realSession = sessionList.find(
    (s) =>
      (s as ExtendedSession).sessionId === tempSessionId &&
      !(s as ExtendedSession).realId &&
      s.id !== tempSessionId,
  );

  // 3) Fallback: only local placeholder exists (backend list not merged yet).
  if (!realSession) {
    realSession = sessionList.find(
      (s) => s.id === tempSessionId && !(s as ExtendedSession).realId,
    );
  }

  if (!realSession) return { list: sessionList, realId: null };

  // Never treat a numeric local id as the backend UUID.
  if (isLocalTimestamp(realSession.id)) {
    return { list: sessionList, realId: null };
  }

  const realUUID = realSession.id;
  (realSession as ExtendedSession).realId = realUUID;
  realSession.id = tempSessionId;
  return {
    list: [realSession, ...sessionList.filter((s) => s !== realSession)],
    realId: realUUID,
  };
};

// ---------------------------------------------------------------------------
// Per-session user message persistence (survives page refresh)
// ---------------------------------------------------------------------------

const STORAGE_PREFIX = "qwenpaw_pending_user_msg_";

/** Shape stored in localStorage. Backward compat: old format was plain text. */
interface PendingUserMsg {
  text: string;
  clientMessageId?: string;
  /** Full content array (stored-name format) for rebuilding the user card
   *  with attachments. When absent, only text is displayed. */
  content?: Array<{ type: string; [key: string]: unknown }>;
}

function savePendingUserMessage(
  sessionId: string,
  data: string | PendingUserMsg,
): void {
  const key = `${STORAGE_PREFIX}${sessionId}`;
  const val = typeof data === "string" ? data : JSON.stringify(data);
  try {
    // localStorage is shared across tabs. A tab switching to an agent whose
    // run is owned by another tab can therefore reconstruct the in-flight
    // user message before the backend history flushes it.
    localStorage.setItem(key, val);
    sessionStorage.removeItem(key);
  } catch {
    // Retain the old per-tab fallback when shared storage is unavailable.
    try {
      sessionStorage.setItem(key, val);
    } catch {
      /* quota exceeded – ignore */
    }
  }
}

function loadPendingUserMessage(sessionId: string): PendingUserMsg | null {
  const key = `${STORAGE_PREFIX}${sessionId}`;
  try {
    const shared = localStorage.getItem(key);
    const legacy = shared ? null : sessionStorage.getItem(key);
    const raw = shared ?? legacy;
    if (!raw) return null;
    if (!shared && legacy) {
      try {
        localStorage.setItem(key, legacy);
        sessionStorage.removeItem(key);
      } catch {
        // Keep reading the legacy value in this tab.
      }
    }
    // Try parsing as JSON (new format with content array)
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && "text" in parsed) {
        return parsed as PendingUserMsg;
      }
    } catch {
      /* not JSON — legacy plain-text format */
    }
    return { text: raw };
  } catch {
    return null;
  }
}

function clearPendingUserMessage(sessionId: string): void {
  const key = `${STORAGE_PREFIX}${sessionId}`;
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
  try {
    sessionStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

function normalizePendingUserMessageIds(
  sessionIds: string | readonly (string | null | undefined)[],
): string[] {
  const ids = Array.isArray(sessionIds) ? sessionIds : [sessionIds];
  return [...new Set(ids.filter((id): id is string => Boolean(id)))];
}

// ---------------------------------------------------------------------------
// SessionApi
// ---------------------------------------------------------------------------

/**
 * Ownership token for asynchronous session work. Captured when an operation
 * starts; a result whose token no longer matches the active epoch must not
 * mutate singleton state or notify the current view.
 *
 * The generation is required in addition to the agent id: switching
 * A -> B -> A would otherwise let work from the first A epoch pass an
 * agent-id-only comparison.
 */
export interface SessionOwnerToken {
  agentId: string;
  generation: number;
}

class SessionApi implements IAgentScopeRuntimeWebUISessionAPI {
  private sessionList: IAgentScopeRuntimeWebUISession[] = [];

  /** Previous returned list reference for shallow-compare optimisation. */
  private _prevReturnedList: IAgentScopeRuntimeWebUISession[] | null = null;

  /**
   * When set, getSessionList will move the matching session to the front on the first call,
   * so the library's useMount auto-selects it instead of always defaulting to sessions[0].
   * Cleared after first use.
   */
  preferredChatId: string | null = null;

  /**
   * Tracks the last actively selected chat ID (realId or displayId).
   * Used to restore the correct session when ChatPage re-mounts without
   * a chatId in the URL (e.g. navigating back to /chat from /settings).
   */
  lastActiveChatId: string | null = null;

  // ---------------------------------------------------------------------------
  // Session switch lock (issue #4557)
  // Prevents rapid session switching from causing infinite loops by blocking
  // all clicks until the current switch completes (data loaded + URL updated).
  // ---------------------------------------------------------------------------

  /** Whether a session switch is currently in progress. */
  isSessionSwitching = false;

  /** AbortController for the current switch — aborted when a new switch starts. */
  private switchAbortController: AbortController | null = null;

  /**
   * Start a new session switch. Aborts any in-flight switch and returns a
   * fresh AbortController whose signal should be threaded through all async ops.
   */
  startNewSwitch(): AbortController {
    // Cancel previous in-flight switch
    this.switchAbortController?.abort();
    const controller = new AbortController();
    this.switchAbortController = controller;
    this.isSessionSwitching = true;
    return controller;
  }

  /** Short-lived result cache so the library's subsequent getSession call
   *  (triggered by setCurrentSessionId → useAsyncEffect) can reuse the
   *  already-fetched session without making another network request. Each
   *  entry carries the owner epoch it was produced under and is only served
   *  within that epoch. */
  private sessionResultCache: Map<
    string,
    { session: IAgentScopeRuntimeWebUISession; owner: SessionOwnerToken }
  > = new Map();

  // ---------------------------------------------------------------------------
  // LRU cache for fully-converted sessions (avoids re-fetching on switch-back)
  // ---------------------------------------------------------------------------

  private static readonly CONVERTED_CACHE_MAX = 10;
  private static readonly CONVERTED_CACHE_TTL = 5 * 60 * 1000; // 5 minutes

  /** LRU cache: backendId → { session, timestamp, updatedAt } */
  private convertedSessionCache = new Map<
    string,
    { session: ExtendedSession; timestamp: number; updatedAt: string | null }
  >();

  /** Stable normalized-history owners, addressable by SDK and backend IDs. */
  private historyOwners = new Map<string, ChatHistoryOwner>();
  /** Complete-turn windows currently mounted into the SDK MessageList. */
  private historyWindows = new Map<
    string,
    { epoch: number; window: ChatTimelineWindow }
  >();
  /** Paths whose tail includes a local, not-yet-persisted user card. */
  private pendingHistoryPaths = new Set<string>();
  /** At most one visible V2 stream is retained; keys are backend Chat IDs. */
  private liveControllers = new Map<string, ChatLiveController>();
  private calibratingLive = new Map<string, Promise<void>>();
  private liveTerminalWaiters = new Map<string, Set<() => void>>();

  private getCachedConvertedSession(
    backendId: string,
    currentUpdatedAt?: string | null,
  ): ExtendedSession | null {
    const entry = this.convertedSessionCache.get(backendId);
    if (!entry) return null;
    if (Date.now() - entry.timestamp > SessionApi.CONVERTED_CACHE_TTL) {
      this.convertedSessionCache.delete(backendId);
      return null;
    }
    // Staleness check against the chat's backend updated_at: when a new
    // message arrives from an external channel (e.g. DingTalk), the polled
    // session list carries a newer updated_at than what we cached. The cached
    // messages are therefore stale and must be dropped so callers re-fetch.
    // Without this, switching away and back served stale cached messages and
    // only a full page refresh (which recreates this singleton) showed the
    // new message (issue #6131 follow-up).
    if (
      currentUpdatedAt &&
      (!entry.updatedAt || currentUpdatedAt > entry.updatedAt)
    ) {
      this.convertedSessionCache.delete(backendId);
      return null;
    }
    // LRU: move to end
    this.convertedSessionCache.delete(backendId);
    this.convertedSessionCache.set(backendId, entry);
    return entry.session;
  }

  private setCachedConvertedSession(
    backendId: string,
    session: ExtendedSession,
    updatedAt: string | null,
  ): void {
    if (this.convertedSessionCache.size >= SessionApi.CONVERTED_CACHE_MAX) {
      // Evict oldest (first entry in Map iteration order)
      const oldestKey = this.convertedSessionCache.keys().next().value;
      if (oldestKey) this.convertedSessionCache.delete(oldestKey);
    }
    this.convertedSessionCache.set(backendId, {
      session,
      timestamp: Date.now(),
      updatedAt,
    });
  }

  /** Invalidate the converted cache for a session (call after sending a message). */
  invalidateConvertedCache(backendId: string): void {
    this.convertedSessionCache.delete(backendId);
  }

  /**
   * Pre-load a session's data. Returns the session with its realId resolved.
   * Used by handleSessionClick to load data BEFORE setting currentSessionId,
   * so the library's automatic getSession call hits the result cache.
   */
  async preloadSession(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<{
    session: IAgentScopeRuntimeWebUISession;
    realId: string | null;
  }> {
    const owner = this.getActiveOwner();
    try {
      const session = await this.getSession(sessionId, signal);

      // A preload must not navigate or repopulate an alias after its Agent
      // or canonical Chat identity changes while history is loading.
      const extendedSession = session as ExtendedSession;
      const realId = extendedSession.realId || null;
      if (
        !this.isActiveOwner(owner) ||
        this.getEffectiveSessionId(sessionId) !== (realId ?? session.id)
      ) {
        throw new DOMException("Aborted", "AbortError");
      }

      // Cache the result so subsequent getSession calls return immediately.
      const entry = { session, owner };
      this.sessionResultCache.set(sessionId, entry);
      if (realId) {
        this.sessionResultCache.set(realId, entry);
      }
      // Clear after 3s (enough for the library's useAsyncEffect to fire).
      // Delete by entry identity so a late timer cannot remove a newer
      // entry cached under the same key.
      setTimeout(() => {
        if (this.sessionResultCache.get(sessionId) === entry) {
          this.sessionResultCache.delete(sessionId);
        }
        if (realId && this.sessionResultCache.get(realId) === entry) {
          this.sessionResultCache.delete(realId);
        }
      }, 3000);

      return { session, realId };
    } catch (error) {
      // A stale completion must look like an abort on the error path too:
      // a plain network/backend rejection would otherwise be handled as a
      // failure of the CURRENT switch and select the old agent's session.
      if (!this.isActiveOwner(owner)) {
        throw new DOMException("Aborted", "AbortError");
      }
      // Don't reset switching state on abort — the new switch owns the lock
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      this.isSessionSwitching = false;
      throw error;
    }
  }

  /** Called when a switch completes (or is superseded). */
  finishSessionSwitch(): void {
    this.switchAbortController?.abort();
    this.isSessionSwitching = false;
    this.switchAbortController = null;
  }

  // ---------------------------------------------------------------------------
  // Agent ownership epochs
  // Async session operations capture the active owner token before awaiting;
  // late results from a previous epoch are rejected before they can mutate
  // singleton state or notify the current view.
  // ---------------------------------------------------------------------------

  /** Current ownership epoch. agentId "" = not yet claimed (tests only —
   *  at runtime the module claims the selected agent on load, below). */
  private activeOwner: SessionOwnerToken = { agentId: "", generation: 0 };

  /** Route metadata survives lists that exclude the selected archived Chat. */
  private resolvedRouteSession: ExtendedSession | null = null;
  private routeResolutionVersion = 0;
  private removedRouteIds = new Set<string>();
  private removedCurrentRouteIds = new Set<string>();

  /** Prevent canonical deleted routes from resolving while navigation settles. */
  markRouteRemoved(...ids: Array<string | null | undefined>): void {
    for (const id of ids) {
      if (id) this.removedRouteIds.add(id);
    }
  }

  /** Temporarily suppress aliases only while their deleted route is current. */
  markCurrentRouteRemoved(...ids: Array<string | null | undefined>): void {
    for (const id of ids) {
      if (id) this.removedCurrentRouteIds.add(id);
    }
  }

  isRouteRemoved(id?: string | null): boolean {
    return (
      !!id &&
      (this.removedRouteIds.has(id) || this.removedCurrentRouteIds.has(id))
    );
  }

  clearRouteRemoval(id?: string | null): void {
    if (!id) return;
    this.removedRouteIds.delete(id);
    this.removedCurrentRouteIds.delete(id);
  }

  /** Notify route aliases when their cached existence proof is invalidated. */
  getRouteResolutionVersion(): number {
    return this.routeResolutionVersion;
  }

  registerResolvedChat(chat: ChatSpec, owner: SessionOwnerToken): void {
    if (!this.isActiveOwner(owner)) return;
    const previousIds = new Map(
      [chat.id, chat.session_id].map((id) => [
        id,
        this.getEffectiveSessionId(id),
      ]),
    );
    this.resolvedRouteSession = {
      ...chatSpecToSession(chat),
      realId: chat.id,
    };
    const changedIds = new Set<string>();
    for (const [id, previousId] of previousIds) {
      if (previousId === this.getEffectiveSessionId(id)) continue;
      changedIds.add(id);
      this.sessionRequests.delete(id);
      this.sessionResultCache.delete(id);
      this.lastSelectedIds.delete(id);
    }
    if (changedIds.size) {
      // Confirmed canonical and runtime IDs supersede stale display aliases.
      // Drop alias-keyed work and restore the displaced row's canonical ID.
      this.sessionList = this.sessionList.map((session) => {
        const existing = session as ExtendedSession;
        return changedIds.has(existing.id) && existing.realId
          ? { ...existing, id: existing.realId }
          : session;
      });
      syncSessionsGlobal([...this.sessionList]);
    }
  }

  private creationVisit = 0;
  private pendingCreatedSessions = new Set<string>();

  /** A new route visit must not join or publish an earlier draft's creation. */
  invalidateSessionCreation(): void {
    this.creationVisit++;
    this.sessionCreationRequest = null;
    this.pendingCreatedSessions.clear();
  }

  /** Called only after the SDK accepts and activates the creation result. */
  activateCreatedSession(id?: string): void {
    if (!id || !this.pendingCreatedSessions.delete(id)) return;
    this.onSessionCreated?.(id);
  }

  /** Share only pending creation within an owner epoch, never a completed Chat. */
  private sessionCreationRequest: {
    owner: SessionOwnerToken;
    promise: Promise<IAgentScopeRuntimeWebUICreateSessionResult>;
  } | null = null;

  /**
   * Claims session ownership for an agent. Advances the generation whenever
   * the agent actually changes so that A -> B -> A creates a fresh epoch.
   * Driven by the agent-store subscription at the bottom of this module, so
   * the claim happens synchronously with the agent change — before any React
   * re-render or effect can start new-agent session work.
   */
  setActiveAgent(agentId: string): SessionOwnerToken {
    if (this.activeOwner.agentId === agentId) return this.activeOwner;
    this.stopAllLiveControllers();
    this.activeOwner = { agentId, generation: this.activeOwner.generation + 1 };
    // Drop shared in-flight work and short-lived caches from the previous
    // epoch so the new agent never awaits or reuses them. The underlying
    // promises keep running but their results are rejected by the owner
    // checks at the apply sites.
    this.sessionListRequest = null;
    this.invalidateSessionCreation();
    this.resolvePromise = null;
    this.pendingLocalSessionIds.clear();
    this.sessionRequests.clear();
    this.sessionResultCache.clear();
    this.convertedSessionCache.clear();
    this.historyOwners.clear();
    this.historyWindows.clear();
    this.pendingHistoryPaths.clear();
    // Reset the session list and its comparison state as well: the next
    // agent's chats can share a session_id (channel:user_id) with the old
    // list, and merging against leftover entries would transfer the previous
    // agent's local id / backend UUID onto a different chat.
    this.sessionList = [];
    this.resolvedRouteSession = null;
    this.removedRouteIds.clear();
    this.removedCurrentRouteIds.clear();
    this._prevReturnedList = null;
    this.lastSelectedIds.clear();
    // Release the switch lock: the switch it belonged to is owned by the
    // previous epoch and its completion handler may never run (e.g. the
    // initializer aborted on unmount). Leaving it set would make the new
    // agent permanently skip URL sync, session selection, and list polling.
    this.isSessionSwitching = false;
    this.switchAbortController?.abort();
    this.switchAbortController = null;
    return this.activeOwner;
  }

  getActiveOwner(): SessionOwnerToken {
    return this.activeOwner;
  }

  /** Bind SDK callbacks to their mounted Agent, including late effect calls. */
  bindToOwner(): IAgentScopeRuntimeWebUISessionAPI {
    const owner = this.getActiveOwner();
    return {
      getSessionList: async () =>
        this.isActiveOwner(owner) ? this.getSessionList() : [],
      getSession: async (id) => {
        if (!this.isActiveOwner(owner)) return undefined;
        const session = await this.getSession(id);
        // Direct callers may inspect late results, but an old SDK/adapter
        // must not publish them into the current owner's view or observer.
        return this.isActiveOwner(owner) ? session : undefined;
      },
      updateSession: async (session) =>
        this.isActiveOwner(owner) ? this.updateSession(session) : [],
      removeSession: async (session) =>
        this.isActiveOwner(owner) ? this.removeSession(session) : [],
      createSession: async (session) => {
        if (!this.isActiveOwner(owner)) {
          throw new DOMException("Agent changed", "AbortError");
        }
        return this.createSession(session);
      },
    };
  }

  /** A token is active only when both the agent and its epoch still match. */
  isActiveOwner(token: SessionOwnerToken): boolean {
    return (
      token.agentId === this.activeOwner.agentId &&
      token.generation === this.activeOwner.generation
    );
  }

  /**
   * Applies view-facing turn usage only while the owner epoch that started the
   * load is still active.
   */
  private visibleSessionId: string | null | undefined;

  setVisibleSession(sessionId: string | null): void {
    if (this.visibleSessionId === sessionId) return;
    this.visibleSessionId = sessionId;
    const visibleBackendId = sessionId
      ? this.getEffectiveSessionId(sessionId, null)
      : null;
    for (const [backendId, controller] of this.liveControllers) {
      if (backendId === visibleBackendId) continue;
      controller.stop();
      this.liveControllers.delete(backendId);
      this.settleLiveWaiters(backendId);
    }
    for (const backendId of this.calibratingLive.keys()) {
      if (backendId !== visibleBackendId) {
        this.settleLiveWaiters(backendId);
      }
    }
    const turn = useTurnUsageStore.getState().activeTurn;
    if (
      sessionId &&
      turn?.agentId === this.activeOwner.agentId &&
      this.getBackendSessionId(sessionId) === turn.sessionId
    )
      return;
    useTurnUsageStore.getState().invalidateTurn();
    useTurnUsageStore.getState().setActiveMaxInputLength(null);
  }

  private isVisibleSession(sessionId: string): boolean {
    return (
      this.visibleSessionId === undefined ||
      (this.visibleSessionId !== null &&
        this.getEffectiveSessionId(sessionId, null) ===
          this.getEffectiveSessionId(this.visibleSessionId, null))
    );
  }

  private applySessionView(
    session: ExtendedSession,
    owner: SessionOwnerToken,
  ): void {
    if (!this.isActiveOwner(owner) || !this.isVisibleSession(session.id))
      return;
    // A history response must not invalidate or overwrite a live stream.
    if (useTurnUsageStore.getState().activeTurn) return;
    hydrateTurnUsageFromMessages(session.messages ?? []);
  }

  private stopAllLiveControllers(): void {
    for (const controller of this.liveControllers.values()) controller.stop();
    this.liveControllers.clear();
    this.calibratingLive.clear();
    for (const backendId of this.liveTerminalWaiters.keys()) {
      this.settleLiveWaiters(backendId);
    }
  }

  private settleLiveWaiters(backendId: string): void {
    const waiters = this.liveTerminalWaiters.get(backendId);
    this.liveTerminalWaiters.delete(backendId);
    for (const settle of waiters ?? []) settle();
  }

  hasNormalizedLive(sessionId: string): boolean {
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    const historyOwner =
      this.historyOwners.get(sessionId) ?? this.historyOwners.get(backendId);
    const historyPath = historyOwner
      ? useChatHistoryStore.getState().paths[historyOwner.key]
      : undefined;
    return (
      this.liveControllers.has(backendId) ||
      this.calibratingLive.has(backendId) ||
      Boolean(
        historyOwner &&
          historyPath?.ownerEpoch === historyOwner.epoch &&
          historyPath.view,
      )
    );
  }

  waitForNormalizedLiveTerminal(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<void> {
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    if (
      !this.liveControllers.has(backendId) &&
      !this.calibratingLive.has(backendId)
    ) {
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      const waiters = this.liveTerminalWaiters.get(backendId) ?? new Set();
      const settle = () => {
        signal?.removeEventListener("abort", abort);
        waiters.delete(settle);
        resolve();
      };
      const abort = () => {
        waiters.delete(settle);
        if (waiters.size === 0) this.liveTerminalWaiters.delete(backendId);
        reject(new DOMException("Aborted", "AbortError"));
      };
      waiters.add(settle);
      this.liveTerminalWaiters.set(backendId, waiters);
      signal?.addEventListener("abort", abort, { once: true });
      if (signal?.aborted) abort();
    });
  }

  private publishLiveWindow(
    displayId: string,
    historyOwner: ChatHistoryOwner,
    owner: SessionOwnerToken,
  ): void {
    if (
      !this.isActiveOwner(owner) ||
      !this.isVisibleSession(displayId) ||
      useChatHistoryStore.getState().activeKey !== historyOwner.key
    ) {
      return;
    }
    const entry = this.historyWindows.get(historyOwner.key);
    const path = useChatHistoryStore.getState().paths[historyOwner.key];
    if (!path || entry?.epoch !== historyOwner.epoch) return;
    const messages = convertMessages(
      selectWindowBackendMessages(
        historyOwner,
        entry.window.turnIds,
        entry.window.atTail,
      ),
    );
    this.onLiveMessages?.(displayId, messages);
  }

  private async runLiveCalibration(
    displayId: string,
    backendId: string,
    owner: SessionOwnerToken,
  ): Promise<void> {
    const current = this.liveControllers.get(backendId);
    current?.stop();
    if (this.liveControllers.get(backendId) === current) {
      this.liveControllers.delete(backendId);
    }
    let retryDelay = 250;
    while (this.isActiveOwner(owner) && this.isVisibleSession(displayId)) {
      try {
        const session = await this.refreshSession(displayId);
        if (
          session &&
          this.isActiveOwner(owner) &&
          this.isVisibleSession(displayId)
        ) {
          this.onLiveMessages?.(displayId, session.messages ?? []);
        }
        return;
      } catch (error) {
        const retryable =
          (error instanceof DOMException && error.name === "AbortError") ||
          error instanceof TypeError ||
          (error instanceof ApiError &&
            (error.status === 409 || error.status >= 500));
        if (!retryable) {
          console.error("Failed to calibrate Chat live history", error);
          return;
        }
        try {
          const view = await api.getChatView(backendId, {
            agentId: owner.agentId,
          });
          if (!view.active_run) retryDelay = 250;
        } catch {
          // Keep the bounded backoff below; the next refresh retries both
          // the durable history read and the authoritative run-state probe.
        }
      }
      await new Promise<void>((resolve) => {
        setTimeout(resolve, retryDelay);
      });
      retryDelay = Math.min(retryDelay * 2, 4000);
    }
  }

  private async calibrateLiveHistory(
    displayId: string,
    backendId: string,
    owner: SessionOwnerToken,
  ): Promise<void> {
    const existing = this.calibratingLive.get(backendId);
    if (existing) return existing;
    const calibration = this.runLiveCalibration(displayId, backendId, owner);
    this.calibratingLive.set(backendId, calibration);
    try {
      await calibration;
    } finally {
      if (this.calibratingLive.get(backendId) === calibration) {
        this.calibratingLive.delete(backendId);
      }
      // A successful running refresh installs a replacement controller. If
      // no controller remains, the run is terminal, inaccessible, or the
      // user navigated away, so reconnect waiters must not hang forever.
      if (!this.liveControllers.has(backendId)) {
        this.settleLiveWaiters(backendId);
      }
    }
  }

  private configureLiveController(
    displayId: string,
    backendId: string,
    historyOwner: ChatHistoryOwner,
    owner: SessionOwnerToken,
    enabled: boolean,
  ): void {
    const previous = this.liveControllers.get(backendId);
    previous?.stop();
    this.liveControllers.delete(backendId);
    if (!enabled || !this.isVisibleSession(displayId)) {
      useChatHistoryStore.getState().clearLive(historyOwner);
      return;
    }
    const controller = new ChatLiveController(
      backendId,
      (cursor, signal) =>
        api.openChatLive(backendId, {
          cursor,
          signal,
          agentId: owner.agentId,
        }),
      {
        onSync: async (sync) => {
          if (!this.isActiveOwner(owner)) return;
          if (
            !useChatHistoryStore.getState().applyLiveSync(historyOwner, sync)
          ) {
            await this.calibrateLiveHistory(displayId, backendId, owner);
            return;
          }
          if (sync.terminal) return;
          this.publishLiveWindow(displayId, historyOwner, owner);
        },
        onEvents: async (events) => {
          if (!this.isActiveOwner(owner)) return;
          if (
            !useChatHistoryStore
              .getState()
              .applyLiveEvents(historyOwner, events)
          ) {
            await this.calibrateLiveHistory(displayId, backendId, owner);
            return;
          }
          if (events.some((event) => event.kind === "terminal")) return;
          this.publishLiveWindow(displayId, historyOwner, owner);
        },
        onResync: async () => {
          await this.calibrateLiveHistory(displayId, backendId, owner);
        },
        onTerminal: async () => {
          await this.calibrateLiveHistory(displayId, backendId, owner);
          this.settleLiveWaiters(backendId);
        },
      },
    );
    this.liveControllers.set(backendId, controller);
    void controller.start().finally(() => {
      if (this.liveControllers.get(backendId) === controller) {
        this.liveControllers.delete(backendId);
      }
    });
  }

  /**
   * Test-only: restores ownership, in-flight work, caches, and callbacks to a
   * pristine page-load state so tests cannot leak state into each other.
   */
  resetForTests(): void {
    this.stopAllLiveControllers();
    this.visibleSessionId = undefined;
    this.activeOwner = { agentId: "", generation: 0 };
    this.resolvedRouteSession = null;
    this.routeResolutionVersion = 0;
    this.removedRouteIds.clear();
    this.removedCurrentRouteIds.clear();
    this.sessionListRequest = null;
    this.invalidateSessionCreation();
    this.resolvePromise = null;
    this.pendingLocalSessionIds.clear();
    this.sessionRequests.clear();
    this.sessionResultCache.clear();
    this.convertedSessionCache.clear();
    this.historyOwners.clear();
    this.historyWindows.clear();
    this.pendingHistoryPaths.clear();
    this.sessionList = [];
    this._prevReturnedList = null;
    this.lastSelectedIds.clear();
    this.preferredChatId = null;
    this.lastActiveChatId = null;
    this.isSessionSwitching = false;
    this.switchAbortController = null;
    this.onSessionIdResolved = null;
    this.onSessionRemoved = null;
    this.onSessionSelected = null;
    this.onSessionCreated = null;
    this.onLiveMessages = null;
  }

  /**
   * Cache the latest user message for a chat so it can be patched into
   * history during reconnect (the backend only persists it after generation
   * completes). Persisted to localStorage so it survives page refresh and is
   * visible when another tab switches into this running conversation.
   *
   * @param content  Optional full content array (in stored-name format)
   *                 including images/files. When provided, patchLastUserMessage
   *                 will reconstruct the user card with attachments.
   */
  setLastUserMessage(
    sessionIds: string | readonly (string | null | undefined)[],
    text: string,
    content?: Array<{ type: string; [key: string]: unknown }>,
    clientMessageId?: string,
  ): void {
    if (!text && (!content || content.length === 0)) return;
    const ids = normalizePendingUserMessageIds(sessionIds);
    for (const sessionId of ids) {
      // Callers supply only canonical Chat UUIDs and their local draft IDs.
      // A runtime session_id can belong to other users/channels or Agents;
      // it must never be used as a shared pending-message cache key.
      this.invalidateConvertedCache(sessionId);
      if (content && content.length > 0) {
        savePendingUserMessage(sessionId, { text, content, clientMessageId });
      } else if (clientMessageId) {
        savePendingUserMessage(sessionId, { text, clientMessageId });
      } else {
        savePendingUserMessage(sessionId, text);
      }
    }
  }

  /** Remove a pending message only when it still belongs to this request. */
  discardLastUserMessage(
    sessionIds: string | readonly (string | null | undefined)[],
    clientMessageId?: string,
  ): void {
    for (const sessionId of normalizePendingUserMessageIds(sessionIds)) {
      const cached = loadPendingUserMessage(sessionId);
      if (!cached) continue;
      if (
        clientMessageId &&
        cached.clientMessageId &&
        cached.clientMessageId !== clientMessageId
      ) {
        continue;
      }
      clearPendingUserMessage(sessionId);
    }
  }

  /**
   * Deduplicates concurrent getSessionList calls so that two parallel
   * invocations share one network request and write sessionList only once,
   * preserving any realId mappings that were already resolved.
   * The in-flight request carries the owner token it was started under so it
   * is never reused — nor applied — across an agent switch.
   */
  private sessionListRequest: {
    owner: SessionOwnerToken;
    promise: Promise<IAgentScopeRuntimeWebUISession[]>;
  } | null = null;

  /** Pending resolve promise so getSession can await it before returning. */
  private resolvePromise: Promise<void> | null = null;

  // A sidebar poll may replace a local placeholder with the backend UUID
  // before POST headers arrive. Keep its ownership until resolution notifies
  // the host, rather than relying on the placeholder still being in the list.
  private pendingLocalSessionIds = new Set<string>();

  /**
   * Deduplicates concurrent getSession calls for the same sessionId.
   * Each in-flight promise carries the owner epoch it was started under and
   * is never reused across an agent switch.
   */
  private sessionRequests: Map<
    string,
    {
      promise: Promise<IAgentScopeRuntimeWebUISession>;
      owner: SessionOwnerToken;
      kind: "get" | "refresh";
    }
  > = new Map();

  /**
   * Called when a temporary timestamp session id is resolved to a real backend
   * UUID. Consumers (e.g. Chat/index.tsx) can register here to update the URL.
   */
  onSessionIdResolved: ((tempId: string, realId: string) => void) | null = null;

  /**
   * Called after a session is removed. Consumers can register here to clear
   * the session id from the URL.
   */
  onSessionRemoved: ((removedId: string) => void) | null = null;

  /**
   * Called when a session is selected from the session list.
   * Consumers can register here to update the URL when switching sessions.
   */
  onSessionSelected:
    | ((sessionId: string | null | undefined, realId: string | null) => void)
    | null = null;

  /**
   * Called when a new session is created.
   * Consumers can register here to update the URL with the new session id.
   */
  onSessionCreated: ((sessionId: string) => void) | null = null;

  /** Publishes a bounded normalized live window to the visible SDK host. */
  onLiveMessages:
    | ((sessionId: string, messages: IAgentScopeRuntimeWebUIMessage[]) => void)
    | null = null;

  /**
   * When reconnecting to a running conversation, the backend history may not
   * include the latest user message (it's only persisted after generation
   * completes). If generating, look up the cached data from shared storage
   * and patch it into the message list (including any attachments).
   *
   * When not generating the conversation is done — clear the cached entry
   * once the fetched history contains the pending text.
   *
   * Returns true when an unconfirmed pending message was patched in (the
   * history is incomplete and must not be treated as canonical).
   */
  private patchLastUserMessage(
    messages: IAgentScopeRuntimeWebUIMessage[],
    generating: boolean,
    sessionIds: readonly (string | null | undefined)[],
  ): boolean {
    const pendingIds = normalizePendingUserMessageIds(sessionIds);
    let cached: PendingUserMsg | null = null;
    let cachedId: string | null = null;
    for (const sessionId of pendingIds) {
      cached = loadPendingUserMessage(sessionId);
      if (cached) {
        cachedId = sessionId;
        break;
      }
    }
    if (!cached || (!cached.text && !cached.content?.length)) {
      if (!generating) {
        pendingIds.forEach(clearPendingUserMessage);
      }
      return false;
    }

    // Canonicalize a local draft entry onto its resolved backend UUID.
    const canonicalId = pendingIds[0];
    if (canonicalId && cachedId !== canonicalId) {
      savePendingUserMessage(canonicalId, cached);
    }

    // A request is confirmed as soon as its unique client id appears
    // anywhere in history. This also prevents an already-persisted turn from
    // being appended again while the backend still reports "running".
    if (cached.clientMessageId) {
      const persistenceConfirmed = messages.some((message) => {
        if (message.role !== ROLE_USER) return false;
        const input = message?.cards?.[0]?.data?.input?.[0];
        return (
          extractClientMessageId(input?.metadata) === cached.clientMessageId
        );
      });
      if (persistenceConfirmed) {
        pendingIds.forEach(clearPendingUserMessage);
        return false;
      }
    }

    // When the chat is idle, clear the cache only after the fetched
    // history actually contains the pending text. Clearing
    // unconditionally lost the last message in two windows: POST sent
    // but the run not registered yet (status still "idle"), and
    // generation completed but the memory flush not finished.
    if (!generating) {
      let lastUserText = "";
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role !== ROLE_USER) continue;
        const input = messages[i]?.cards?.[0]?.data?.input?.[0];
        lastUserText = extractTextFromContent(input?.content);
        break;
      }
      const persistenceConfirmed =
        !cached.clientMessageId && lastUserText.trim() === cached.text.trim();
      if (persistenceConfirmed) {
        pendingIds.forEach(clearPendingUserMessage);
        return false;
      }
      // History is missing the turn — fall through and patch it in,
      // keeping the cache until a later fetch confirms persistence.
    }

    // Use the full content array (with images/files) when available;
    // fall back to text-only for legacy entries.
    const msgContent: unknown =
      cached.content ??
      (cached.text ? [{ type: "text", text: cached.text }] : []);

    const lastMsg = messages[messages.length - 1];
    if (lastMsg?.role === ROLE_USER) {
      const text = extractTextFromContent(
        lastMsg?.cards?.[0]?.data?.input?.[0]?.content,
      );
      if (!text) {
        lastMsg.cards = buildUserCard({
          content: msgContent,
          role: ROLE_USER,
        } as Message).cards;
      }
    } else {
      messages.push(
        buildUserCard({
          content: msgContent,
          role: ROLE_USER,
        } as Message),
      );
    }
    return true;
  }

  private createEmptySession(
    sessionId: string,
    owner: SessionOwnerToken,
  ): ExtendedSession {
    if (this.isActiveOwner(owner) && this.isVisibleSession(sessionId)) {
      useTurnUsageStore.getState().setSnapshot(null);
    }
    return {
      id: sessionId,
      name: DEFAULT_SESSION_NAME,
      sessionId,
      userId: DEFAULT_USER_ID,
      channel: DEFAULT_CHANNEL,
      messages: [],
      meta: {},
    } as ExtendedSession;
  }

  private findSession(id: string): ExtendedSession | undefined {
    // Canonical IDs, including resolved archived Chats, precede SDK aliases.
    const canonical = this.sessionList.find(
      (x) => ((x as ExtendedSession).realId ?? x.id) === id,
    ) as ExtendedSession | undefined;
    if (canonical) return canonical;

    const resolved = this.resolvedRouteSession;
    if (
      resolved?.id === id ||
      resolved?.realId === id ||
      resolved?.sessionId === id
    )
      return resolved;

    const display = this.sessionList.find((x) => x.id === id) as
      | ExtendedSession
      | undefined;
    if (display) return display;

    // Runtime session_id values are opaque. Most locally-created values use
    // the timestamp-random format, but imported/auto-registered Chats may use
    // UUID-looking runtime IDs as well. Use an unconfirmed list alias only
    // when the match is unique: the backend permits multiple Chats to share
    // a runtime identity, and guessing would cross conversation data.
    const runtimeMatches = new Map<string, ExtendedSession>();
    for (const session of this.sessionList as ExtendedSession[]) {
      if (session.sessionId === id)
        runtimeMatches.set(session.realId ?? session.id, session);
    }
    return runtimeMatches.size === 1
      ? runtimeMatches.values().next().value
      : undefined;
  }

  /** Returns the real backend UUID, or null when not yet resolved. */
  getRealIdForSession(sessionId: string): string | null {
    const session = this.findSession(sessionId);
    if (!session) return null;
    return session.realId ?? (session.id !== sessionId ? session.id : null);
  }

  /** Resolves the effective ID for URL navigation (prefers backend UUID). */
  getEffectiveSessionId(
    sessionId: string,
    resolvedRealId?: string | null,
  ): string {
    return resolvedRealId ?? this.getRealIdForSession(sessionId) ?? sessionId;
  }

  /**
   * Centralizes state tracking after navigating to a session.
   * Reduces repeated `lastActiveChatId + persist` scattered
   * across onSessionIdResolved, onSessionSelected, sidebar, and initializer.
   */
  trackNavigatedSession(
    effectiveId: string,
    persistFn?: (agentId: string, id: string) => void,
    agentId?: string,
  ): void {
    this.lastActiveChatId = effectiveId;
    // Never persist a temporary local timestamp id. These ids only exist in
    // memory for brand-new chats before the first message is sent; persisting
    // them causes an unknown id to be restored on agent switch.
    if (persistFn && agentId && !isLocalTimestamp(effectiveId)) {
      persistFn(agentId, effectiveId);
    }
  }

  /**
   * Returns true if id is a newly-created local-timestamp session that hasn't
   * yet sent its first message (i.e. it's in sessionList but has no realId).
   * Used by onSessionSelected to suppress library auto-selection while the
   * user is on a blank new-chat screen.
   */
  isUnresolvedLocalSession(id: string): boolean {
    if (!isLocalTimestamp(id)) return false;
    const session = this.findSession(id);
    return !!session && !session.realId;
  }

  /** Returns the backend-compatible session_id. Falls back to the id itself. */
  getBackendSessionId(libraryId: string): string {
    return this.findSession(libraryId)?.sessionId || libraryId;
  }

  /** Resolve one immutable identity from an explicit SDK, chat, or runtime id. */
  getSessionIdentity(referenceId?: string | null): SessionIdentity {
    const explicitId = referenceId || "";
    const session = explicitId
      ? this.findSession(explicitId)
      : (this.sessionList.find(
          (item) =>
            isLocalTimestamp(item.id) && !(item as ExtendedSession).realId,
        ) as ExtendedSession | undefined);
    const sdkSessionId = session?.id || explicitId;
    const chatId =
      session?.realId ??
      (session && sdkSessionId && !isLocalTimestamp(sdkSessionId)
        ? sdkSessionId
        : undefined);
    return {
      sdkSessionId,
      chatId,
      sessionId:
        session?.sessionId ||
        (isLocalTimestamp(sdkSessionId) ? sdkSessionId : ""),
      userId: session?.userId || DEFAULT_USER_ID,
      channel: session?.channel || DEFAULT_CHANNEL,
    };
  }

  /** Apply listChats to sessionList; merge realId and generating by session_id. */
  private applyChatsToSessionList(
    chats: ChatSpec[],
  ): IAgentScopeRuntimeWebUISession[] {
    const resolvedId = this.resolvedRouteSession?.id;
    if (
      resolvedId &&
      this.sessionList.some(
        (session) =>
          ((session as ExtendedSession).realId ?? session.id) === resolvedId,
      ) &&
      !chats.some((chat) => chat.id === resolvedId)
    ) {
      // A listed Chat disappeared: let the route distinguish archive from
      // deletion. Never-listed archived Chats retain their resolved identity.
      this.resolvedRouteSession = null;
      this.routeResolutionVersion++;
    }
    // Capture the leading unresolved local session (the one just created via
    // createSession). It won't appear in the backend list until the first
    // message is sent; without this it would be wiped on every getSessionList
    // call — causing the "new chat flashes then disappears" bug.
    // We only track the leading entry: at most one unresolved session should
    // exist at any time (the guard in createSession enforces this invariant).
    const firstItem = this.sessionList[0];
    const leadingUnresolved =
      firstItem &&
      isLocalTimestamp(firstItem.id) &&
      !(firstItem as ExtendedSession).realId
        ? (firstItem as ExtendedSession)
        : null;

    const newList = chats
      .filter((c) => c.id && c.id !== "undefined" && c.id !== "null")
      .map(chatSpecToSession)
      .reverse();

    // Track which existing sessions have already been matched so that
    // sessions sharing the same sessionId (channel:user_id) don't all
    // resolve to the same existing entry — the root cause of #3843.
    const matchedExistingIds = new Set<string>();

    this.sessionList = newList.map((s) => {
      const sExt = s as ExtendedSession;

      // 1) Exact match by backend UUID: s.id matches existing.id or existing.realId
      let existing = this.sessionList.find((e) => {
        if (matchedExistingIds.has(e.id)) return false;
        const eExt = e as ExtendedSession;
        return e.id === s.id || (eExt.realId != null && eExt.realId === s.id);
      }) as ExtendedSession | undefined;

      // 2) Fallback: match by sessionId, but only claim the first unmatched one
      if (!existing) {
        existing = this.sessionList.find((e) => {
          if (matchedExistingIds.has(e.id)) return false;
          const eExt = e as ExtendedSession;
          return (
            eExt.sessionId === sExt.sessionId &&
            eExt.userId === sExt.userId &&
            eExt.channel === sExt.channel
          );
        }) as ExtendedSession | undefined;
      }

      if (!existing) return s;

      matchedExistingIds.add(existing.id);

      const next = { ...s } as ExtendedSession;
      if (existing.realId && existing.realId === s.id) {
        // Keep local aliases only for the same canonical Chat: runtime IDs
        // can be reassigned after deletion. Preserve the SDK ID during SSE.
        next.id = existing.id;
        next.realId = existing.realId;
      }
      // Only carry over generating=true from the old session when the
      // backend hasn't explicitly reported the chat as idle.  Previously
      // the flag was inherited unconditionally, so once set it could never
      // be cleared — causing a permanent spinner in the session list
      // (issue #4903).
      if (existing.generating && sExt.status !== "idle") {
        next.generating = existing.generating;
      }
      return next as IAgentScopeRuntimeWebUISession;
    });

    // Re-prepend the leading unresolved local session if the backend didn't
    // return it yet (no message sent). Once matched, it's already in the new
    // list as {id: localId} via resolveRealId, so no re-prepend is needed.
    if (leadingUnresolved && !matchedExistingIds.has(leadingUnresolved.id)) {
      this.sessionList = [leadingUnresolved, ...this.sessionList];
    }

    if (this.preferredChatId) {
      const preferredId = this.preferredChatId;
      this.preferredChatId = null;
      let idx = this.sessionList.findIndex((s) => s.id === preferredId);
      // Page refresh: URL may contain an opaque runtime session_id while the
      // backend list exposes only Chat UUIDs. UUID-looking runtime IDs are
      // valid too, so match by value rather than by the old timestamp shape.
      if (idx < 0 && this.resolvedRouteSession?.id !== preferredId) {
        const runtimeMatches = this.sessionList
          .map((s, index) => ({ s: s as ExtendedSession, index }))
          .filter(({ s }) => s.sessionId === preferredId);
        if (runtimeMatches.length === 1) {
          idx = runtimeMatches[0].index;
          const s = runtimeMatches[0].s;
          s.realId = s.id;
          s.id = preferredId;
        }
      }
      if (idx > 0) {
        const [preferred] = this.sessionList.splice(idx, 1);
        this.sessionList.unshift(preferred);
      }
    }

    // If the list hasn't changed substantively, return the previous array
    // reference to prevent downstream useMemo / React re-renders.
    if (
      this._prevReturnedList &&
      this.isSessionListEqual(this._prevReturnedList, this.sessionList)
    ) {
      return this._prevReturnedList;
    }
    const result = [...this.sessionList];
    this._prevReturnedList = result;
    return result;
  }

  /**
   * Shallow-compare two session lists by key fields.
   * Returns true if lists are structurally identical (no re-render needed).
   */
  private isSessionListEqual(
    prev: IAgentScopeRuntimeWebUISession[],
    next: IAgentScopeRuntimeWebUISession[],
  ): boolean {
    if (prev.length !== next.length) return false;
    for (let i = 0; i < prev.length; i++) {
      const a = prev[i] as ExtendedSession;
      const b = next[i] as ExtendedSession;
      const bothRunning = a.status === "running" && b.status === "running";
      if (
        a.id !== b.id ||
        a.name !== b.name ||
        a.status !== b.status ||
        (!bothRunning && a.updatedAt !== b.updatedAt) ||
        a.lastFinishedAt !== b.lastFinishedAt ||
        a.createdAt !== b.createdAt ||
        a.pinned !== b.pinned ||
        a.generating !== b.generating ||
        a.realId !== b.realId ||
        a.sessionId !== b.sessionId ||
        a.userId !== b.userId ||
        a.channel !== b.channel ||
        a.archivedAt !== b.archivedAt ||
        a.archived !== b.archived ||
        a.source !== b.source ||
        a.groupId !== b.groupId ||
        a.parentSessionId !== b.parentSessionId ||
        a.rootSessionId !== b.rootSessionId
      ) {
        return false;
      }
    }
    return true;
  }

  async getSessionList() {
    // Reuse an in-flight request only within the same ownership epoch, so a
    // new agent never awaits a list request started by a previous agent.
    if (
      this.sessionListRequest &&
      this.isActiveOwner(this.sessionListRequest.owner)
    ) {
      return this.sessionListRequest.promise;
    }

    const owner = this.getActiveOwner();
    const entry = { owner } as {
      owner: SessionOwnerToken;
      promise: Promise<IAgentScopeRuntimeWebUISession[]>;
    };
    entry.promise = (async () => {
      try {
        const chats = await api.listChats({
          archived: false,
          include_app_owned: false,
        });
        // A result from a stale epoch must not replace the current agent's
        // session list; hand back the current list without mutation.
        if (!this.isActiveOwner(owner)) {
          return this._prevReturnedList ?? [...this.sessionList];
        }
        return this.applyChatsToSessionList(chats);
      } finally {
        if (this.sessionListRequest === entry) this.sessionListRequest = null;
      }
    })();
    this.sessionListRequest = entry;

    return entry.promise;
  }

  /**
   * Track both displayId and realId of the last selected session to avoid
   * duplicate onSessionSelected calls when the same session is loaded via
   * either its displayId or realId (issue #4557).
   */
  private lastSelectedIds: Set<string> = new Set();

  /** Refresh after another sender has owned this Chat, without selecting it. */
  async refreshSession(sessionId: string, signal?: AbortSignal) {
    const owner = this.getActiveOwner();
    let requestKey = this.getEffectiveSessionId(sessionId);
    const inFlight =
      this.sessionRequests.get(sessionId) ??
      this.sessionRequests.get(requestKey);
    if (inFlight && this.isActiveOwner(inFlight.owner)) {
      if (inFlight.kind === "refresh") return inFlight.promise;
      await inFlight.promise.catch(() => undefined);
    }
    if (signal?.aborted || !this.isActiveOwner(owner)) return undefined;
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    requestKey = backendId;
    const requestKeys = new Set([sessionId, requestKey]);
    const joinedRefresh = [...requestKeys]
      .map((key) => this.sessionRequests.get(key))
      .find((entry) => entry?.kind === "refresh");
    if (
      joinedRefresh?.kind === "refresh" &&
      this.isActiveOwner(joinedRefresh.owner)
    ) {
      return joinedRefresh.promise;
    }
    this.sessionResultCache.delete(sessionId);
    this.sessionResultCache.delete(backendId);
    this.invalidateConvertedCache(backendId);
    const requestPromise = this._doGetSession(sessionId, signal, owner);
    const entry = { promise: requestPromise, owner, kind: "refresh" as const };
    requestKeys.forEach((key) => this.sessionRequests.set(key, entry));
    try {
      return await requestPromise;
    } finally {
      requestKeys.forEach((key) => {
        if (this.sessionRequests.get(key) === entry) {
          this.sessionRequests.delete(key);
        }
      });
    }
  }

  async getSession(sessionId: string, signal?: AbortSignal) {
    const owner = this.getActiveOwner();

    // Check short-lived result cache first (populated by preloadSession).
    // Entries must still belong to the same owner and canonical Chat.
    const cached = this.sessionResultCache.get(sessionId);
    if (
      cached &&
      this.isActiveOwner(cached.owner) &&
      this.getEffectiveSessionId(sessionId) ===
        ((cached.session as ExtendedSession).realId ?? cached.session.id)
    ) {
      this.applySessionView(cached.session as ExtendedSession, owner);
      return cached.session;
    }

    // Reuse an in-flight request only within the same ownership epoch, so a
    // new agent never adopts a request (and its captured owner) started by a
    // previous agent.
    const requestKey = this.getEffectiveSessionId(sessionId);
    const requestKeys = new Set([sessionId, requestKey]);
    const existingRequest = [...requestKeys]
      .map((key) => this.sessionRequests.get(key))
      .find((request) => request !== undefined);
    if (existingRequest && this.isActiveOwner(existingRequest.owner)) {
      return existingRequest.promise;
    }

    const requestPromise = this._doGetSession(sessionId, signal, owner);
    const entry = { promise: requestPromise, owner, kind: "get" as const };
    requestKeys.forEach((key) => this.sessionRequests.set(key, entry));

    try {
      const session = await requestPromise;
      const extendedSession = session as ExtendedSession;
      const realId = extendedSession.realId || null;
      if (
        this.isActiveOwner(owner) &&
        this.getEffectiveSessionId(sessionId) !== (realId ?? session.id)
      ) {
        throw new DOMException("Aborted", "AbortError");
      }

      // Only trigger onSessionSelected if the result still belongs to the
      // active ownership epoch and neither the displayId nor the realId has
      // already been selected. The latter prevents the infinite loop where
      // displayId and realId alternate triggering onSessionSelected.
      if (this.isActiveOwner(owner) && !this.lastSelectedIds.has(sessionId)) {
        this.lastSelectedIds.clear();
        this.lastSelectedIds.add(sessionId);
        if (realId) this.lastSelectedIds.add(realId);
        this.onSessionSelected?.(sessionId, realId);
      }
      return session;
    } finally {
      // Delete by entry identity so a stale request cannot remove a newer
      // epoch's in-flight entry stored under the same key.
      requestKeys.forEach((key) => {
        if (this.sessionRequests.get(key) === entry) {
          this.sessionRequests.delete(key);
        }
      });
    }
  }

  /**
   * Fetch chat history from backend and build an ExtendedSession.
   * Centralises the repeated fetch-convert-patch-build pattern used by
   * _doGetSession in multiple branches. Construction is applied to shared
   * state (turn-usage store and converted cache) only while
   * the caller's owner epoch is still active.
   */
  private async fetchAndBuildSession(
    displayId: string,
    backendId: string,
    listEntry: ExtendedSession | undefined,
    signal: AbortSignal | undefined,
    owner: SessionOwnerToken,
  ): Promise<ExtendedSession> {
    const isIdle = !listEntry?.generating;
    if (isIdle) {
      const cached = this.getCachedConvertedSession(
        backendId,
        listEntry?.updatedAt,
      );
      if (cached) {
        let historyReady = true;
        if (cached.historyV2) {
          const historyOwner = useChatHistoryStore
            .getState()
            .beginPath(owner.agentId, backendId);
          const historyPath =
            useChatHistoryStore.getState().paths[historyOwner.key];
          if (!historyPath?.view) {
            this.invalidateConvertedCache(backendId);
            historyReady = false;
          } else {
            this.historyOwners.set(displayId, historyOwner);
            this.historyOwners.set(backendId, historyOwner);
            this.historyWindows.set(historyOwner.key, {
              epoch: historyOwner.epoch,
              window: createTailTimelineWindow(historyPath),
            });
          }
        }
        if (historyReady) {
          cached.id = displayId;
          if (listEntry?.name) cached.name = listEntry.name;
          cached.realId =
            listEntry?.realId ??
            (backendId !== displayId ? backendId : cached.realId);
          this.applySessionView(cached, owner);
          return cached;
        }
      }
    }

    try {
      return await this.fetchAndBuildPaginatedSession(
        displayId,
        backendId,
        listEntry,
        signal,
        owner,
      );
    } catch (error) {
      // A rolling downgrade can remove the V2 application while the Console
      // bundle is still open. Only that explicit capability failure may use
      // the legacy transcript endpoint; authorization and data errors must
      // remain visible instead of being masked by a second read path.
      const capabilityUnavailable =
        error instanceof ApiError &&
        error.status === 503 &&
        ["chat_v2_unavailable", "chat_v2_ui_disabled"].some((code) =>
          error.message.includes(code),
        );
      if (!capabilityUnavailable) throw error;
    }

    return this.fetchAndBuildLegacySession(
      displayId,
      backendId,
      listEntry,
      signal,
      owner,
    );
  }

  private assertHistoryOwner(
    displayId: string,
    backendId: string,
    owner: SessionOwnerToken,
    signal?: AbortSignal,
  ): void {
    if (
      signal?.aborted ||
      !this.isActiveOwner(owner) ||
      this.getEffectiveSessionId(displayId) !== backendId
    ) {
      throw new DOMException("Aborted", "AbortError");
    }
  }

  private async fetchAndBuildPaginatedSession(
    displayId: string,
    backendId: string,
    listEntry: ExtendedSession | undefined,
    signal: AbortSignal | undefined,
    owner: SessionOwnerToken,
  ): Promise<ExtendedSession> {
    const view = await api.getChatView(backendId, {
      signal,
      agentId: owner.agentId,
    });
    this.assertHistoryOwner(displayId, backendId, owner, signal);

    const historyState = useChatHistoryStore.getState();
    const currentHistoryOwner =
      this.historyOwners.get(displayId) ?? this.historyOwners.get(backendId);
    const currentHistoryPath = currentHistoryOwner
      ? historyState.paths[currentHistoryOwner.key]
      : undefined;
    const historyOwner =
      currentHistoryOwner?.agentId === owner.agentId &&
      currentHistoryOwner.conversationId === backendId &&
      currentHistoryPath?.ownerEpoch === currentHistoryOwner.epoch &&
      currentHistoryPath.view?.view_version === view.view_version &&
      historyState.activeKey === currentHistoryOwner.key
        ? currentHistoryOwner
        : historyState.beginPath(owner.agentId, backendId, view.view_version);
    const page = await api.getTurnPage(backendId, {
      headTurnId: view.head_turn_id,
      viewVersion: view.view_version,
      limit: TURN_PAGE_LIMIT,
      signal,
      agentId: owner.agentId,
    });
    this.assertHistoryOwner(displayId, backendId, owner, signal);
    if (!useChatHistoryStore.getState().mergeTurnPage(historyOwner, page)) {
      throw new DOMException("History owner changed", "AbortError");
    }
    await this.prefetchIncompleteChildren(
      backendId,
      historyOwner,
      owner,
      signal,
    );

    this.historyOwners.set(displayId, historyOwner);
    this.historyOwners.set(backendId, historyOwner);
    const historyPath = useChatHistoryStore.getState().paths[historyOwner.key];
    if (!historyPath) {
      throw new DOMException("History owner changed", "AbortError");
    }
    const historyWindow = createTailTimelineWindow(historyPath);
    this.historyWindows.set(historyOwner.key, {
      epoch: historyOwner.epoch,
      window: historyWindow,
    });
    const backendMessages = selectLoadedBackendMessages(historyOwner);
    for (const id of new Set([displayId, backendId])) {
      useMessageQueueStore
        .getState()
        .reconcileHistory(id, owner.agentId, backendMessages);
    }

    const generating = view.active_run !== null;
    const messages = convertMessages(
      selectWindowBackendMessages(
        historyOwner,
        historyWindow.turnIds,
        historyWindow.atTail,
      ),
    );
    const patchedPending = this.patchLastUserMessage(messages, generating, [
      backendId,
      displayId,
      listEntry?.realId,
    ]);
    if (patchedPending) this.pendingHistoryPaths.add(historyOwner.key);
    else this.pendingHistoryPaths.delete(historyOwner.key);
    const session: ExtendedSession = {
      id: displayId,
      name: listEntry?.name || view.display_name || DEFAULT_SESSION_NAME,
      sessionId: listEntry?.sessionId || displayId,
      userId: listEntry?.userId || DEFAULT_USER_ID,
      channel: listEntry?.channel || DEFAULT_CHANNEL,
      messages,
      meta: listEntry?.meta || {},
      realId:
        listEntry?.realId ?? (backendId !== displayId ? backendId : undefined),
      generating,
      historyV2: true,
    };
    this.applySessionView(session, owner);
    this.configureLiveController(
      displayId,
      backendId,
      historyOwner,
      owner,
      Boolean(view.capabilities.live_resync && view.active_run),
    );
    if (!generating && !patchedPending && this.isActiveOwner(owner)) {
      this.setCachedConvertedSession(
        backendId,
        session,
        listEntry?.updatedAt ?? null,
      );
    }
    return session;
  }

  private async prefetchIncompleteChildren(
    backendId: string,
    historyOwner: ChatHistoryOwner,
    owner: SessionOwnerToken,
    signal?: AbortSignal,
  ): Promise<void> {
    let fetchedBytes = 0;
    let requests = 0;
    while (
      fetchedBytes < CHILD_PREFETCH_BYTE_LIMIT &&
      requests < CHILD_PREFETCH_REQUEST_LIMIT
    ) {
      if (signal?.aborted || !this.isActiveOwner(owner)) {
        throw new DOMException("Aborted", "AbortError");
      }
      const path = useChatHistoryStore.getState().paths[historyOwner.key];
      if (!path || path.ownerEpoch !== historyOwner.epoch || !path.view) {
        throw new DOMException("History owner changed", "AbortError");
      }

      let progressed = false;
      for (const turnId of [...path.turnOrder].reverse()) {
        const turn = path.turnsById[turnId];
        if (!turn) continue;
        for (const messageId of turn.messageIds) {
          const message = path.messagesById[messageId];
          if (!message?.child_cursor) continue;
          const blockPage = await api.getBlockPage(backendId, messageId, {
            headTurnId: path.view.head_turn_id,
            viewVersion: path.view.view_version,
            cursor: message.child_cursor,
            signal,
            agentId: owner.agentId,
          });
          if (
            !useChatHistoryStore
              .getState()
              .mergeBlockPage(historyOwner, messageId, blockPage)
          ) {
            throw new DOMException("History view changed", "AbortError");
          }
          fetchedBytes += blockPage.bytes;
          requests += 1;
          progressed = true;
          break;
        }
        if (progressed) break;
        if (!turn.child_cursor) continue;
        const messagePage = await api.getMessagePage(backendId, turnId, {
          headTurnId: path.view.head_turn_id,
          viewVersion: path.view.view_version,
          cursor: turn.child_cursor,
          signal,
          agentId: owner.agentId,
        });
        if (
          !useChatHistoryStore
            .getState()
            .mergeMessagePage(historyOwner, turnId, messagePage)
        ) {
          throw new DOMException("History view changed", "AbortError");
        }
        fetchedBytes += messagePage.bytes;
        requests += 1;
        progressed = true;
        break;
      }
      if (!progressed) return;
    }
  }

  private async fetchAndBuildLegacySession(
    displayId: string,
    backendId: string,
    listEntry: ExtendedSession | undefined,
    signal: AbortSignal | undefined,
    owner: SessionOwnerToken,
  ): Promise<ExtendedSession> {
    const chatHistory = await api.getChat(backendId, {
      signal,
      include_app_owned: false,
    });
    if (
      signal?.aborted ||
      (this.isActiveOwner(owner) &&
        this.getEffectiveSessionId(displayId) !== backendId)
    ) {
      throw new DOMException("Aborted", "AbortError");
    }
    if (this.isActiveOwner(owner)) {
      for (const id of new Set([displayId, backendId])) {
        useMessageQueueStore
          .getState()
          .reconcileHistory(id, owner.agentId, chatHistory.messages || []);
      }
    }
    const generating = isGenerating(chatHistory);
    const messages = convertMessages(chatHistory.messages || []);
    const patchedPending = this.patchLastUserMessage(messages, generating, [
      backendId,
      displayId,
      listEntry?.realId,
    ]);

    const session: ExtendedSession = {
      id: displayId,
      name: listEntry?.name || DEFAULT_SESSION_NAME,
      sessionId: listEntry?.sessionId || displayId,
      userId: listEntry?.userId || DEFAULT_USER_ID,
      channel: listEntry?.channel || DEFAULT_CHANNEL,
      messages,
      meta: listEntry?.meta || {},
      realId:
        listEntry?.realId ?? (backendId !== displayId ? backendId : undefined),
      generating,
    };

    // Cache non-generating sessions — only within the epoch that fetched
    // them, so a stale load cannot write into the new agent's cache.
    // A history patched with an unconfirmed pending message is NOT
    // canonical (the agent reply may still be missing): caching it would
    // keep serving the incomplete turn for the whole cache TTL.
    if (!generating && !patchedPending && this.isActiveOwner(owner)) {
      this.setCachedConvertedSession(
        backendId,
        session,
        listEntry?.updatedAt ?? null,
      );
    }

    this.applySessionView(session, owner);
    return session;
  }

  /** Load the next older normalized window for the active SDK session. */
  async loadOlderHistory(
    sessionId: string,
    options: HistoryLoadOptions = {},
  ): Promise<HistoryLoadResult> {
    const owner = this.getActiveOwner();
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    const historyOwner =
      this.historyOwners.get(sessionId) ?? this.historyOwners.get(backendId);
    if (
      !historyOwner ||
      historyOwner.agentId !== owner.agentId ||
      this.pendingHistoryPaths.has(historyOwner.key)
    ) {
      return {
        messages: [],
        noMore: true,
        replacedWindow: false,
        windowAtLimit: false,
        turnCount: 0,
        atOlderLimit: true,
        atTail: true,
      };
    }
    const path = useChatHistoryStore.getState().paths[historyOwner.key];
    if (!path || path.ownerEpoch !== historyOwner.epoch || !path.view) {
      return {
        messages: [],
        noMore: true,
        replacedWindow: false,
        windowAtLimit: false,
        turnCount: 0,
        atOlderLimit: true,
        atTail: true,
      };
    }
    const entry = this.historyWindows.get(historyOwner.key);
    const currentWindow =
      entry?.epoch === historyOwner.epoch
        ? entry.window
        : createTailTimelineWindow(path);
    const firstIndex = path.turnOrder.indexOf(currentWindow.turnIds[0]);
    if (firstIndex <= 0 && path.olderCursor) {
      const page = await api.getTurnPage(backendId, {
        headTurnId: path.view.head_turn_id,
        viewVersion: path.view.view_version,
        cursor: path.olderCursor,
        limit: TURN_PAGE_LIMIT,
        agentId: owner.agentId,
      });
      if (!this.isActiveOwner(owner)) {
        throw new DOMException("Agent changed", "AbortError");
      }
      const merged = useChatHistoryStore
        .getState()
        .mergeTurnPage(historyOwner, page, "older");
      if (!merged) {
        throw new DOMException("History view changed", "AbortError");
      }
      await this.prefetchIncompleteChildren(backendId, historyOwner, owner);
    }
    const updatedPath = useChatHistoryStore.getState().paths[historyOwner.key];
    if (!updatedPath) {
      throw new DOMException("History owner changed", "AbortError");
    }
    const extendedWindow = extendTimelineWindowOlder(
      updatedPath,
      currentWindow,
      false,
    );
    const windowExtended =
      extendedWindow.turnIds.join("\u0000") !==
      currentWindow.turnIds.join("\u0000");
    const nextWindow =
      !options.extendOnly && !windowExtended
        ? extendTimelineWindowOlder(updatedPath, currentWindow, true)
        : extendedWindow;
    this.historyWindows.set(historyOwner.key, {
      epoch: historyOwner.epoch,
      window: nextWindow,
    });
    const replacedWindow =
      nextWindow.turnIds.join("\u0000") !==
      currentWindow.turnIds.join("\u0000");
    const canExtend = timelineWindowCanExtendOlder(updatedPath, nextWindow);
    const messages = convertMessages(
      selectWindowBackendMessages(
        historyOwner,
        nextWindow.turnIds,
        nextWindow.atTail,
      ),
    );
    return {
      messages,
      noMore: nextWindow.atOlderLimit,
      replacedWindow,
      windowAtLimit: canExtend && !replacedWindow,
      turnCount: nextWindow.turnIds.length,
      atOlderLimit: nextWindow.atOlderLimit,
      atTail: nextWindow.atTail,
    };
  }

  /** Load or reveal the next newer normalized window. */
  async loadNewerHistory(
    sessionId: string,
    options: HistoryLoadOptions = {},
  ): Promise<HistoryLoadResult> {
    const owner = this.getActiveOwner();
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    const historyOwner =
      this.historyOwners.get(sessionId) ?? this.historyOwners.get(backendId);
    if (
      !historyOwner ||
      historyOwner.agentId !== owner.agentId ||
      this.pendingHistoryPaths.has(historyOwner.key)
    ) {
      return {
        messages: [],
        noMore: true,
        replacedWindow: false,
        windowAtLimit: false,
        turnCount: 0,
        atOlderLimit: true,
        atTail: true,
      };
    }
    const path = useChatHistoryStore.getState().paths[historyOwner.key];
    if (!path || path.ownerEpoch !== historyOwner.epoch || !path.view) {
      return {
        messages: [],
        noMore: true,
        replacedWindow: false,
        windowAtLimit: false,
        turnCount: 0,
        atOlderLimit: true,
        atTail: true,
      };
    }
    const entry = this.historyWindows.get(historyOwner.key);
    const currentWindow =
      entry?.epoch === historyOwner.epoch
        ? entry.window
        : createTailTimelineWindow(path);
    const lastIndex = path.turnOrder.indexOf(
      currentWindow.turnIds[currentWindow.turnIds.length - 1],
    );
    if (lastIndex >= path.turnOrder.length - 1 && path.newerCursor) {
      const page = await api.getTurnPage(backendId, {
        headTurnId: path.view.head_turn_id,
        viewVersion: path.view.view_version,
        cursor: path.newerCursor,
        limit: TURN_PAGE_LIMIT,
        agentId: owner.agentId,
      });
      if (!this.isActiveOwner(owner)) {
        throw new DOMException("Agent changed", "AbortError");
      }
      const merged = useChatHistoryStore
        .getState()
        .mergeTurnPage(historyOwner, page, "newer");
      if (!merged) {
        throw new DOMException("History view changed", "AbortError");
      }
      await this.prefetchIncompleteChildren(backendId, historyOwner, owner);
    }
    const updatedPath = useChatHistoryStore.getState().paths[historyOwner.key];
    if (!updatedPath) {
      throw new DOMException("History owner changed", "AbortError");
    }
    const extendedWindow = extendTimelineWindowNewer(
      updatedPath,
      currentWindow,
      false,
    );
    const windowExtended =
      extendedWindow.turnIds.join("\u0000") !==
      currentWindow.turnIds.join("\u0000");
    const nextWindow =
      !options.extendOnly && !windowExtended
        ? extendTimelineWindowNewer(updatedPath, currentWindow, true)
        : extendedWindow;
    this.historyWindows.set(historyOwner.key, {
      epoch: historyOwner.epoch,
      window: nextWindow,
    });
    const replacedWindow =
      nextWindow.turnIds.join("\u0000") !==
      currentWindow.turnIds.join("\u0000");
    const canExtend = timelineWindowCanExtendNewer(updatedPath, nextWindow);
    const messages = convertMessages(
      selectWindowBackendMessages(
        historyOwner,
        nextWindow.turnIds,
        nextWindow.atTail,
      ),
    );
    return {
      messages,
      noMore: nextWindow.atTail,
      replacedWindow,
      windowAtLimit: canExtend && !replacedWindow,
      turnCount: nextWindow.turnIds.length,
      atOlderLimit: nextWindow.atOlderLimit,
      atTail: nextWindow.atTail,
    };
  }

  /** Re-read the fixed view tail and atomically reset the mounted window. */
  async returnToLatestHistory(sessionId: string): Promise<HistoryLoadResult> {
    const owner = this.getActiveOwner();
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    const historyOwner =
      this.historyOwners.get(sessionId) ?? this.historyOwners.get(backendId);
    const path = historyOwner
      ? useChatHistoryStore.getState().paths[historyOwner.key]
      : undefined;
    if (
      !historyOwner ||
      historyOwner.agentId !== owner.agentId ||
      !path ||
      path.ownerEpoch !== historyOwner.epoch ||
      !path.view
    ) {
      return {
        messages: [],
        noMore: true,
        replacedWindow: false,
        windowAtLimit: false,
        turnCount: 0,
        atOlderLimit: true,
        atTail: true,
      };
    }
    const latestView = await api.getChatView(backendId, {
      agentId: owner.agentId,
    });
    if (!this.isActiveOwner(owner)) {
      throw new DOMException("Agent changed", "AbortError");
    }
    if (latestView.view_version !== path.view.view_version) {
      const session = await this.refreshSession(sessionId);
      const refreshedOwner =
        this.historyOwners.get(sessionId) ?? this.historyOwners.get(backendId);
      const refreshedEntry = refreshedOwner
        ? this.historyWindows.get(refreshedOwner.key)
        : undefined;
      if (!session || !refreshedOwner || !refreshedEntry) {
        throw new DOMException("History owner changed", "AbortError");
      }
      return {
        messages: session.messages ?? [],
        noMore: refreshedEntry.window.atTail,
        replacedWindow: true,
        windowAtLimit: false,
        turnCount: refreshedEntry.window.turnIds.length,
        atOlderLimit: refreshedEntry.window.atOlderLimit,
        atTail: refreshedEntry.window.atTail,
      };
    }
    const page = await api.getTurnPage(backendId, {
      headTurnId: path.view.head_turn_id,
      viewVersion: path.view.view_version,
      limit: TURN_PAGE_LIMIT,
      agentId: owner.agentId,
    });
    if (!this.isActiveOwner(owner)) {
      throw new DOMException("Agent changed", "AbortError");
    }
    const merged = useChatHistoryStore
      .getState()
      .mergeTurnPage(historyOwner, page, "initial");
    if (!merged) {
      throw new DOMException("History view changed", "AbortError");
    }
    await this.prefetchIncompleteChildren(backendId, historyOwner, owner);
    const updatedPath = useChatHistoryStore.getState().paths[historyOwner.key];
    if (!updatedPath) {
      throw new DOMException("History owner changed", "AbortError");
    }
    const nextWindow = createTailTimelineWindow(updatedPath);
    this.historyWindows.set(historyOwner.key, {
      epoch: historyOwner.epoch,
      window: nextWindow,
    });
    return {
      messages: convertMessages(
        selectWindowBackendMessages(
          historyOwner,
          nextWindow.turnIds,
          nextWindow.atTail,
        ),
      ),
      noMore: nextWindow.atTail,
      replacedWindow: true,
      windowAtLimit: false,
      turnCount: nextWindow.turnIds.length,
      atOlderLimit: nextWindow.atOlderLimit,
      atTail: nextWindow.atTail,
    };
  }

  /** Hydrate one fixed-view turn window for data-layer navigation. */
  async loadHistoryAtTurn(
    sessionId: string,
    turnId: string,
  ): Promise<HistoryLoadResult> {
    const owner = this.getActiveOwner();
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    const historyOwner =
      this.historyOwners.get(sessionId) ?? this.historyOwners.get(backendId);
    const path = historyOwner
      ? useChatHistoryStore.getState().paths[historyOwner.key]
      : undefined;
    if (
      !historyOwner ||
      historyOwner.agentId !== owner.agentId ||
      !path ||
      path.ownerEpoch !== historyOwner.epoch ||
      !path.view
    ) {
      return {
        messages: [],
        noMore: true,
        replacedWindow: false,
        windowAtLimit: false,
        turnCount: 0,
        atOlderLimit: true,
        atTail: true,
      };
    }
    const page = await api.getTurnPage(backendId, {
      headTurnId: path.view.head_turn_id,
      viewVersion: path.view.view_version,
      targetTurnId: turnId,
      limit: TURN_PAGE_LIMIT,
      agentId: owner.agentId,
    });
    if (!this.isActiveOwner(owner)) {
      throw new DOMException("Agent changed", "AbortError");
    }
    const merged = useChatHistoryStore
      .getState()
      .mergeTurnPage(historyOwner, page, "initial");
    if (!merged) {
      throw new DOMException("History view changed", "AbortError");
    }
    await this.prefetchIncompleteChildren(backendId, historyOwner, owner);
    const updatedPath = useChatHistoryStore.getState().paths[historyOwner.key];
    const targetTurn = updatedPath?.turnsById[turnId];
    if (!updatedPath || !targetTurn) {
      throw new Error("Target Chat turn was not returned by the server");
    }
    const nextWindow = createTimelineWindowAtTurn(updatedPath, turnId);
    this.historyWindows.set(historyOwner.key, {
      epoch: historyOwner.epoch,
      window: nextWindow,
    });
    const targetBubbleId = targetTurn.messageIds.find(
      (messageId) => updatedPath.messagesById[messageId]?.role === "user",
    );
    return {
      messages: convertMessages(
        selectWindowBackendMessages(
          historyOwner,
          nextWindow.turnIds,
          nextWindow.atTail,
        ),
      ),
      noMore: nextWindow.atOlderLimit,
      replacedWindow: true,
      windowAtLimit: false,
      turnCount: nextWindow.turnIds.length,
      atOlderLimit: nextWindow.atOlderLimit,
      atTail: nextWindow.atTail,
      targetBubbleId,
    };
  }

  hasPaginatedHistory(sessionId: string): boolean {
    const backendId = this.getRealIdForSession(sessionId) || sessionId;
    const historyOwner =
      this.historyOwners.get(sessionId) ?? this.historyOwners.get(backendId);
    if (!historyOwner) return false;
    const path = useChatHistoryStore.getState().paths[historyOwner.key];
    return Boolean(
      path &&
        path.ownerEpoch === historyOwner.epoch &&
        path.view &&
        !this.pendingHistoryPaths.has(historyOwner.key),
    );
  }

  /**
   * Read one integrity-checked payload fragment from the active normalized
   * history path. The caller owns buffering and must keep it bounded.
   */
  async loadBlockFragment(
    blockId: string,
    cursor: string | null,
    expectedStart: number,
    signal?: AbortSignal,
  ): Promise<LoadedChatBlockFragment> {
    const owner = this.getActiveOwner();
    const activeKey = useChatHistoryStore.getState().activeKey;
    const historyOwner = [...this.historyOwners.values()].find(
      (candidate) =>
        candidate.key === activeKey && candidate.agentId === owner.agentId,
    );
    if (!historyOwner) {
      throw new Error("Chat history is no longer active");
    }
    const path = useChatHistoryStore.getState().paths[historyOwner.key];
    const block = path?.blocksById[blockId];
    if (
      !path ||
      path.ownerEpoch !== historyOwner.epoch ||
      !path.view ||
      !block ||
      block.content_loaded
    ) {
      throw new Error("Chat block is no longer available for lazy loading");
    }

    const requestCursor = cursor ?? block.content_cursor;
    if (!requestCursor) {
      throw new Error("Chat block does not have a content cursor");
    }
    const page = await api.getBlockFragment(
      historyOwner.conversationId,
      blockId,
      {
        headTurnId: path.view.head_turn_id,
        viewVersion: path.view.view_version,
        cursor: requestCursor,
        signal,
        agentId: owner.agentId,
      },
    );
    if (signal?.aborted || !this.isActiveOwner(owner)) {
      throw new DOMException("Agent changed", "AbortError");
    }
    const currentPath = useChatHistoryStore.getState().paths[historyOwner.key];
    const currentBlock = currentPath?.blocksById[blockId];
    if (
      currentPath?.ownerEpoch !== historyOwner.epoch ||
      currentPath.view?.view_version !== path.view.view_version ||
      !currentBlock ||
      currentBlock.entity_version !== block.entity_version
    ) {
      throw new DOMException("History view changed", "AbortError");
    }
    return validateChatBlockFragment(
      page,
      currentBlock,
      path.view,
      expectedStart,
    );
  }

  private async _doGetSession(
    sessionId: string,
    signal: AbortSignal | undefined,
    owner: SessionOwnerToken,
  ): Promise<IAgentScopeRuntimeWebUISession> {
    // --- No session selected (library bug: createSession sets undefined) ---
    if (!sessionId || sessionId === "undefined" || sessionId === "null") {
      if (this.isActiveOwner(owner)) {
        useTurnUsageStore.getState().setSnapshot(null);
      }
      return {
        id: sessionId || "",
        name: "",
        sessionId: "",
        userId: DEFAULT_USER_ID,
        channel: DEFAULT_CHANNEL,
        messages: [],
        meta: {},
      } as ExtendedSession;
    }

    // --- Local timestamp ID (New Chat before first reply) ---
    if (isLocalTimestamp(sessionId)) {
      const fromList = this.findSession(sessionId);
      if (fromList?.realId) {
        return this.fetchAndBuildSession(
          sessionId,
          fromList.realId,
          fromList,
          signal,
          owner,
        );
      }
      // A triggerResolve may be in-flight (POST succeeded but getSessionList
      // hasn't returned yet). Wait for it so we can return real messages
      // instead of an empty local session — prevents clearing the library's
      // message state after the first SSE stream completes.
      if (fromList && this.resolvePromise) {
        await this.resolvePromise;
        const resolved = this.findSession(sessionId);
        if (resolved?.realId) {
          return this.fetchAndBuildSession(
            sessionId,
            resolved.realId,
            resolved,
            signal,
            owner,
          );
        }
      }
      if (fromList) {
        this.applySessionView(fromList, owner);
        return fromList;
      }
      return this.createEmptySession(sessionId, owner);
    }

    // --- Backend Chat UUID or an opaque runtime session_id alias ---
    const listEntry = this.findSession(sessionId);
    const backendId =
      listEntry?.realId ??
      (listEntry && listEntry.id !== sessionId ? listEntry.id : sessionId);
    return this.fetchAndBuildSession(
      sessionId,
      backendId,
      listEntry,
      signal,
      owner,
    );
  }

  /**
   * After fetching the latest session list, try to resolve a local timestamp
   * session to its real backend UUID and notify listeners. Skipped entirely
   * when the ownership epoch the resolution was started under is no longer
   * active: a late resolution from a previous agent must neither rewrite the
   * current agent's session list nor fire onSessionIdResolved into its view.
   */
  private resolveAndNotify(tempId: string, owner: SessionOwnerToken): void {
    if (!this.isActiveOwner(owner)) return;
    const { list, realId } = resolveRealId(this.sessionList, tempId);
    this.sessionList = list;
    if (realId) {
      this.pendingLocalSessionIds.delete(tempId);
      // Publish the resolved mapping immediately so the SDK can match a URL
      // carrying the backend UUID while the response is still generating.
      syncSessionsGlobal(this.sessionList as ExtendedSession[]);
      // Migrate the pending user message from the local timestamp key to
      // the backend UUID key so patchLastUserMessage can find it after
      // page refresh (where the URL — and therefore the lookup key — is
      // the UUID, not the original timestamp).
      const cached = loadPendingUserMessage(tempId);
      if (cached) {
        savePendingUserMessage(realId, cached);
        clearPendingUserMessage(tempId);
      }
      this.onSessionIdResolved?.(tempId, realId);
    }
  }

  /**
   * Trigger ID resolution for a local timestamp session.
   * Called by customFetch after POST succeeds (the backend has created the
   * chat at that point). Fire-and-forget — runs concurrently with SSE.
   */
  triggerResolve(tempId: string): void {
    if (!isLocalTimestamp(tempId)) return;
    const existing = this.findSession(tempId);
    const owner = this.getActiveOwner();
    if (existing?.realId) {
      if (this.pendingLocalSessionIds.has(tempId)) {
        this.resolveAndNotify(tempId, owner);
      }
      return;
    }
    if (!existing && !this.pendingLocalSessionIds.has(tempId)) return;
    // Force a fresh listChats request: if a stale in-flight getSessionList
    // (started before the POST) is still pending, its response won't contain
    // the new backend session yet. Sharing that stale promise would cause
    // resolveRealId to silently fail and onSessionIdResolved never fires,
    // leaving the URL at /chat instead of /chat/<uuid>.
    this.sessionListRequest = null;
    const promise = this.getSessionList()
      .then(() => this.resolveAndNotify(tempId, owner))
      .finally(() => {
        if (this.resolvePromise === promise) this.resolvePromise = null;
      });
    this.resolvePromise = promise;
  }

  async updateSession(session: Partial<IAgentScopeRuntimeWebUISession>) {
    // Strip messages before merging to avoid storing large data in the
    // session list. Use destructuring instead of mutating the input object
    // — the library may pass its own internal session reference, and
    // mutating session.messages would corrupt its React state.
    const { messages: _msgs, ...metadata } = session;
    void _msgs;
    const index = this.sessionList.findIndex((s) => s.id === metadata.id);

    if (index > -1) {
      this.sessionList[index] = { ...this.sessionList[index], ...metadata };
    } else {
      // Session not found by id — createSession now always unshifts the
      // session before returning, so this branch should not occur in normal
      // flows. Refresh the list to stay in sync but do NOT call resolveAndNotify:
      // triggerResolve (called by customFetch after POST success) is the sole
      // entry point for ID resolution.
      await this.getSessionList();
    }

    return [...this.sessionList];
  }

  async createSession(
    session: Partial<IAgentScopeRuntimeWebUISession>,
  ): Promise<IAgentScopeRuntimeWebUICreateSessionResult> {
    const owner = this.getActiveOwner();
    const visit = this.creationVisit;
    if (
      this.sessionCreationRequest &&
      this.isActiveOwner(this.sessionCreationRequest.owner)
    ) {
      return this.sessionCreationRequest.promise;
    }
    // Idempotency: reuse an unresolved local session. The explicit SDK result
    // identifies the reused session without mutating the caller's draft or
    // relying on list order.
    const existing = this.sessionList.find(
      (s) => isLocalTimestamp(s.id) && !(s as ExtendedSession).realId,
    ) as ExtendedSession | undefined;
    if (existing) {
      this.pendingCreatedSessions.add(existing.id);
      return { sessions: [...this.sessionList], session: existing };
    }

    const entry = {
      owner,
      promise:
        (async (): Promise<IAgentScopeRuntimeWebUICreateSessionResult> => {
          const runtimeSessionId = `${Date.now()}-${randomBase36(7)}`;
          // Resolve the backend UUID before the first turn. Only this shared
          // operation publishes the session and fires the creation callback.
          const placeholderName = session.name?.slice(0, 10) || "Media Message";
          const chat = await api.createChat({
            session_id: runtimeSessionId,
            user_id: DEFAULT_USER_ID,
            channel: DEFAULT_CHANNEL,
            name: placeholderName,
            meta: { console_placeholder_name: placeholderName },
          });
          if (!this.isActiveOwner(owner) || visit !== this.creationVisit) {
            throw new DOMException("Session creation superseded", "AbortError");
          }
          // Publish the same metadata as a list refresh on the first render;
          // missing timestamps would briefly place a new Chat in Earlier.
          const extended = {
            ...this.createEmptySession(chat.id, owner),
            ...chatSpecToSession(chat),
            realId: chat.id,
            sessionId: chat.session_id || runtimeSessionId,
          };
          this.sessionList.unshift(extended);
          this.pendingCreatedSessions.add(chat.id);
          return { sessions: [...this.sessionList], session: extended };
        })(),
    };
    this.sessionCreationRequest = entry;
    try {
      return await entry.promise;
    } finally {
      // Both success and failure permit another creation. An old epoch must
      // not clear the request installed by a newer A -> B -> A cycle.
      if (this.sessionCreationRequest === entry) {
        this.sessionCreationRequest = null;
      }
    }
  }

  async removeSession(session: Partial<IAgentScopeRuntimeWebUISession>) {
    if (!session.id) return [...this.sessionList];

    const { id: sessionId } = session;
    const owner = this.getActiveOwner();

    const existing = this.findSession(sessionId);

    const deleteId =
      existing?.realId ?? (isLocalTimestamp(sessionId) ? null : sessionId);

    if (deleteId) await api.deleteChat(deleteId);

    // Invalidate LRU cache for the deleted session
    if (deleteId) this.invalidateConvertedCache(deleteId);
    if (existing?.realId) this.invalidateConvertedCache(existing.realId);

    // Use the canonical id from the list entry (existing?.id = localId even when
    // the caller passed a UUID), so the filter always removes the right entry.
    const canonicalId = existing?.id ?? sessionId;
    this.sessionList = this.sessionList.filter((s) => s.id !== canonicalId);
    if (
      this.isActiveOwner(owner) &&
      this.resolvedRouteSession?.id === canonicalId
    ) {
      this.resolvedRouteSession = null;
      this.routeResolutionVersion++;
    }

    const resolvedId = existing?.realId ?? sessionId;
    this.onSessionRemoved?.(resolvedId);

    return [...this.sessionList];
  }
}

const sessionApi = new SessionApi();

// Ownership follows the selected agent from the agent store. Claiming here —
// at module load and synchronously inside every store update — guarantees a
// single lifecycle location and that the epoch advances before any React
// re-render or effect can start new-agent session work, regardless of which
// page (chat, settings, sidebar preload) triggers the change.
sessionApi.setActiveAgent(useAgentStore.getState().selectedAgent);
useAgentStore.subscribe((state) => {
  sessionApi.setActiveAgent(state.selectedAgent);
});

export default sessionApi;

// ---------------------------------------------------------------------------
// Test-only exports (used by ./tests/testLargeSession.test.tsx — PR-F3 / #5479)
// These helpers are pure data transforms with no side effects; exposing them
// avoids reaching into internals via private reflection. Keep the surface tiny.
// ---------------------------------------------------------------------------
export const __test__ = {
  convertMessages,
  buildUserCard,
  buildResponseCard,
  toOutputMessage,
  normalizeOutputMessageContent,
  contentToRequestParts,
  extractTextFromContent,
  parseTimestamp,
  parseFinishedAt,
  isLocalTimestamp,
  isGenerating,
  resolveRealId,
};
