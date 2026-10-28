import { create } from "zustand";
import type {
  ChatBlockItem,
  ChatHistoryPage,
  ChatMessageItem,
  ChatTurnItem,
  ChatViewIdentity,
  Message,
} from "../../api/types/chat";
import type {
  ChatLiveBlockView,
  ChatLiveCursor,
  ChatLiveEvent,
  ChatLivePath,
  ChatLiveSync,
} from "./chatLiveController";
import { stripScrollHeadlines } from "./headlineFilter";

// Six turn pages plus one bounded eight-request message/block continuation.
export const ACTIVE_PAGE_LIMIT = 14;
export const ACTIVE_BYTE_LIMIT = 12 * 1024 * 1024;
export const BACKGROUND_PAGE_LIMIT = 2;
export const BACKGROUND_BYTE_LIMIT = 4 * 1024 * 1024;

export interface ChatHistoryOwner {
  agentId: string;
  conversationId: string;
  key: string;
  epoch: number;
}

interface NormalizedTurn extends Omit<ChatTurnItem, "messages"> {
  messageIds: string[];
}

interface NormalizedMessage extends Omit<ChatMessageItem, "blocks"> {
  blockIds: string[];
}

interface HistoryPageRef {
  id: string;
  level: "turn" | "message" | "block";
  turnIds: string[];
  messageIds: string[];
  blockIds: string[];
  bytes: number;
  olderCursor: string | null;
  newerCursor: string | null;
}

export interface ChatLiveOverlayBlock {
  messageId: string;
  blockId: string;
  index: number;
  kind: string;
  state: string;
  baseOffset: number;
  text: string;
  textBytes: number;
  entityVersion: number;
}

export interface ChatLiveOverlay {
  path: ChatLivePath;
  cursor: ChatLiveCursor;
  runState: string;
  blocksById: Record<string, ChatLiveOverlayBlock>;
  messagesById: Record<string, Message>;
  messageOrder: string[];
}

export interface ChatHistoryPath {
  ownerEpoch: number;
  view: ChatViewIdentity | null;
  turnsById: Record<string, NormalizedTurn>;
  messagesById: Record<string, NormalizedMessage>;
  blocksById: Record<string, ChatBlockItem>;
  turnOrder: string[];
  pages: HistoryPageRef[];
  decodedBytes: number;
  olderCursor: string | null;
  newerCursor: string | null;
  liveOverlay: ChatLiveOverlay | null;
}

type MergeDirection = "initial" | "older" | "newer";

interface ChatHistoryState {
  activeKey: string | null;
  paths: Record<string, ChatHistoryPath>;
  beginPath: (
    agentId: string,
    conversationId: string,
    viewVersion?: string,
  ) => ChatHistoryOwner;
  mergeTurnPage: (
    owner: ChatHistoryOwner,
    page: ChatHistoryPage<ChatTurnItem>,
    direction?: MergeDirection,
  ) => boolean;
  mergeMessagePage: (
    owner: ChatHistoryOwner,
    turnId: string,
    page: ChatHistoryPage<ChatMessageItem>,
  ) => boolean;
  mergeBlockPage: (
    owner: ChatHistoryOwner,
    messageId: string,
    page: ChatHistoryPage<ChatBlockItem>,
  ) => boolean;
  applyLiveSync: (owner: ChatHistoryOwner, sync: ChatLiveSync) => boolean;
  applyLiveEvents: (
    owner: ChatHistoryOwner,
    events: ChatLiveEvent[],
  ) => boolean;
  clearLive: (owner: ChatHistoryOwner) => void;
  invalidatePath: (owner: ChatHistoryOwner) => void;
}

let nextOwnerEpoch = 0;

export function chatHistoryPathKey(
  agentId: string,
  conversationId: string,
): string {
  return [agentId, conversationId]
    .map((part) => encodeURIComponent(part))
    .join(":");
}

function emptyPath(ownerEpoch: number): ChatHistoryPath {
  return {
    ownerEpoch,
    view: null,
    turnsById: {},
    messagesById: {},
    blocksById: {},
    turnOrder: [],
    pages: [],
    decodedBytes: 0,
    olderCursor: null,
    newerCursor: null,
    liveOverlay: null,
  };
}

const textEncoder = new TextEncoder();

function textBytes(value: string): number {
  return textEncoder.encode(value).byteLength;
}

function normalizeLiveBlock(
  path: ChatHistoryPath,
  block: ChatLiveBlockView,
): ChatLiveOverlayBlock | null {
  const durableBlock = path.blocksById[block.block_id];
  const durable = durableBlock?.total_bytes ?? 0;
  const snapshot = block.snapshot;
  let baseOffset = durable;
  let offset = durable;
  let text = "";
  if (!durableBlock && block.durable_bytes > 0) {
    if (!snapshot || snapshot.start_byte > block.durable_bytes) return null;
    baseOffset = snapshot.start_byte;
    text = snapshot.text;
    offset = baseOffset + textBytes(text);
    if (offset < block.durable_bytes) return null;
  }
  for (const segment of block.pending) {
    const length = textBytes(segment.text);
    const end = segment.start_byte + length;
    if (end <= offset) continue;
    if (segment.start_byte !== offset) return null;
    text += segment.text;
    offset = end;
  }
  if (
    text &&
    durableBlock &&
    durableBlock.entity_version > block.entity_version
  ) {
    return null;
  }
  return {
    messageId: block.message_id,
    blockId: block.block_id,
    index: block.index,
    kind: block.kind,
    state: block.state,
    baseOffset,
    text,
    textBytes: textBytes(text),
    entityVersion: block.entity_version,
  };
}

function eventMatchesOverlay(
  overlay: ChatLiveOverlay,
  event: ChatLiveEvent,
): boolean {
  return (
    overlay.path.conversation_id === event.path.conversation_id &&
    overlay.path.run_id === event.path.run_id &&
    overlay.path.turn_id === event.path.turn_id &&
    overlay.cursor.run_epoch === event.cursor.run_epoch
  );
}

function normalizeLiveMessage(
  value: unknown,
  messageId: string,
  turnId?: string,
): Message | null {
  if (!value || typeof value !== "object") return null;
  return {
    ...(value as Message),
    id: messageId,
    ...(turnId ? { chat_turn_id: turnId } : {}),
  };
}

function pageId(
  level: HistoryPageRef["level"],
  ownerId: string,
  viewVersion: string,
  first: string,
  last: string,
): string {
  return [level, ownerId, viewVersion, first, last].join(":");
}

function normalizePage(page: ChatHistoryPage<ChatTurnItem>): {
  reference: HistoryPageRef;
  turns: Record<string, NormalizedTurn>;
  messages: Record<string, NormalizedMessage>;
  blocks: Record<string, ChatBlockItem>;
} {
  const turns: Record<string, NormalizedTurn> = {};
  const messages: Record<string, NormalizedMessage> = {};
  const blocks: Record<string, ChatBlockItem> = {};
  const turnIds: string[] = [];
  const messageIds: string[] = [];
  const blockIds: string[] = [];

  for (const turn of page.items) {
    const normalizedMessages = turn.messages.map((message) => {
      const normalizedBlocks = message.blocks.map((block) => {
        blocks[block.block_id] = block;
        blockIds.push(block.block_id);
        return block.block_id;
      });
      const { blocks: _blocks, ...messageShell } = message;
      void _blocks;
      messages[message.message_id] = {
        ...messageShell,
        blockIds: normalizedBlocks,
      };
      messageIds.push(message.message_id);
      return message.message_id;
    });
    const { messages: _messages, ...turnShell } = turn;
    void _messages;
    turns[turn.turn_id] = {
      ...turnShell,
      messageIds: normalizedMessages,
    };
    turnIds.push(turn.turn_id);
  }

  return {
    reference: {
      id: pageId(
        "turn",
        page.view.conversation_id,
        page.view.view_version,
        turnIds[0] ?? "empty",
        turnIds[turnIds.length - 1] ?? "empty",
      ),
      level: "turn",
      turnIds,
      messageIds,
      blockIds,
      bytes: page.bytes,
      olderCursor: page.older_cursor,
      newerCursor: page.newer_cursor,
    },
    turns,
    messages,
    blocks,
  };
}

function normalizeMessages(
  turnId: string,
  page: ChatHistoryPage<ChatMessageItem>,
) {
  const messages: Record<string, NormalizedMessage> = {};
  const blocks: Record<string, ChatBlockItem> = {};
  const messageIds: string[] = [];
  const blockIds: string[] = [];
  for (const message of page.items) {
    const normalizedBlocks = message.blocks.map((block) => {
      blocks[block.block_id] = block;
      blockIds.push(block.block_id);
      return block.block_id;
    });
    const { blocks: discardedBlocks, ...messageShell } = message;
    void discardedBlocks;
    messages[message.message_id] = {
      ...messageShell,
      blockIds: normalizedBlocks,
    };
    messageIds.push(message.message_id);
  }
  return {
    reference: {
      id: pageId(
        "message",
        turnId,
        page.view.view_version,
        messageIds[0] ?? "empty",
        messageIds[messageIds.length - 1] ?? "empty",
      ),
      level: "message" as const,
      turnIds: [turnId],
      messageIds,
      blockIds,
      bytes: page.bytes,
      olderCursor: page.older_cursor,
      newerCursor: page.newer_cursor,
    },
    messages,
    blocks,
  };
}

function normalizeBlocks(
  messageId: string,
  turnId: string,
  page: ChatHistoryPage<ChatBlockItem>,
) {
  const blocks = Object.fromEntries(
    page.items.map((block) => [block.block_id, block]),
  );
  const blockIds = page.items.map((block) => block.block_id);
  return {
    reference: {
      id: pageId(
        "block",
        messageId,
        page.view.view_version,
        blockIds[0] ?? "empty",
        blockIds[blockIds.length - 1] ?? "empty",
      ),
      level: "block" as const,
      turnIds: [turnId],
      messageIds: [messageId],
      blockIds,
      bytes: page.bytes,
      olderCursor: page.older_cursor,
      newerCursor: page.newer_cursor,
    },
    blocks,
  };
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

function prunePath(
  path: ChatHistoryPath,
  active: boolean,
  direction: MergeDirection,
): ChatHistoryPath {
  const pageLimit = active ? ACTIVE_PAGE_LIMIT : BACKGROUND_PAGE_LIMIT;
  const byteLimit = active ? ACTIVE_BYTE_LIMIT : BACKGROUND_BYTE_LIMIT;
  const pages = [...path.pages];
  let decodedBytes = pages.reduce((total, page) => total + page.bytes, 0);
  while (pages.length > pageLimit || decodedBytes > byteLimit) {
    const removed = direction === "older" ? pages.pop() : pages.shift();
    if (!removed) break;
    decodedBytes -= removed.bytes;
  }

  const retainedTurns = new Set(pages.flatMap((page) => page.turnIds));
  const retainedMessages = new Set(pages.flatMap((page) => page.messageIds));
  const retainedBlocks = new Set(pages.flatMap((page) => page.blockIds));
  const turnsById = Object.fromEntries(
    Object.entries(path.turnsById).filter(([id]) => retainedTurns.has(id)),
  );
  const messagesById = Object.fromEntries(
    Object.entries(path.messagesById).filter(([id]) =>
      retainedMessages.has(id),
    ),
  );
  const blocksById = Object.fromEntries(
    Object.entries(path.blocksById).filter(([id]) => retainedBlocks.has(id)),
  );
  const turnOrder = unique(pages.flatMap((page) => page.turnIds));
  return {
    ...path,
    pages,
    decodedBytes,
    turnsById,
    messagesById,
    blocksById,
    turnOrder,
    olderCursor:
      pages.find((page) => page.level === "turn")?.olderCursor ?? null,
    newerCursor:
      [...pages].reverse().find((page) => page.level === "turn")?.newerCursor ??
      null,
  };
}

function trimBackgroundPaths(
  paths: Record<string, ChatHistoryPath>,
  activeKey: string,
): Record<string, ChatHistoryPath> {
  return Object.fromEntries(
    Object.entries(paths).map(([key, path]) => [
      key,
      prunePath(path, key === activeKey, "newer"),
    ]),
  );
}

export const useChatHistoryStore = create<ChatHistoryState>((set, get) => ({
  activeKey: null,
  paths: {},
  beginPath: (agentId, conversationId, viewVersion) => {
    const key = chatHistoryPathKey(agentId, conversationId);
    const epoch = ++nextOwnerEpoch;
    const current = get().paths[key];
    const viewChanged = Boolean(
      current?.view && viewVersion && current.view.view_version !== viewVersion,
    );
    const path =
      current && !viewChanged
        ? { ...current, ownerEpoch: epoch }
        : emptyPath(epoch);
    const paths = trimBackgroundPaths({ ...get().paths, [key]: path }, key);
    set({ activeKey: key, paths });
    return { agentId, conversationId, key, epoch };
  },
  mergeTurnPage: (owner, page, direction = "initial") => {
    const state = get();
    const path = state.paths[owner.key];
    if (!path || path.ownerEpoch !== owner.epoch) return false;
    if (page.view.conversation_id !== owner.conversationId) {
      return false;
    }
    if (path.view && path.view.view_version !== page.view.view_version) {
      return false;
    }
    const normalized = normalizePage(page);
    const withoutDuplicate = path.pages.filter(
      (candidate) => candidate.id !== normalized.reference.id,
    );
    const pages =
      direction === "older"
        ? [normalized.reference, ...withoutDuplicate]
        : direction === "newer"
        ? [...withoutDuplicate, normalized.reference]
        : [normalized.reference];
    const merged = prunePath(
      {
        ...path,
        view: page.view,
        pages,
        turnsById: { ...path.turnsById, ...normalized.turns },
        messagesById: { ...path.messagesById, ...normalized.messages },
        blocksById: { ...path.blocksById, ...normalized.blocks },
      },
      state.activeKey === owner.key,
      direction,
    );
    set({ paths: { ...state.paths, [owner.key]: merged } });
    return true;
  },
  mergeMessagePage: (owner, turnId, page) => {
    const state = get();
    const path = state.paths[owner.key];
    const turn = path?.turnsById[turnId];
    if (
      !path ||
      !turn ||
      path.ownerEpoch !== owner.epoch ||
      page.view.conversation_id !== owner.conversationId ||
      path.view?.view_version !== page.view.view_version
    ) {
      return false;
    }
    const normalized = normalizeMessages(turnId, page);
    const messagesById = {
      ...path.messagesById,
      ...normalized.messages,
    };
    const messageIds = unique([
      ...turn.messageIds,
      ...normalized.reference.messageIds,
    ]).sort(
      (left, right) => messagesById[left].ordinal - messagesById[right].ordinal,
    );
    const pages = [
      ...path.pages.filter(
        (candidate) => candidate.id !== normalized.reference.id,
      ),
      normalized.reference,
    ];
    const merged = prunePath(
      {
        ...path,
        pages,
        turnsById: {
          ...path.turnsById,
          [turnId]: {
            ...turn,
            messageIds,
            child_cursor: page.newer_cursor,
            children_complete: page.newer_cursor === null,
          },
        },
        messagesById,
        blocksById: { ...path.blocksById, ...normalized.blocks },
      },
      state.activeKey === owner.key,
      "newer",
    );
    set({ paths: { ...state.paths, [owner.key]: merged } });
    return true;
  },
  mergeBlockPage: (owner, messageId, page) => {
    const state = get();
    const path = state.paths[owner.key];
    const message = path?.messagesById[messageId];
    if (
      !path ||
      !message ||
      path.ownerEpoch !== owner.epoch ||
      page.view.conversation_id !== owner.conversationId ||
      path.view?.view_version !== page.view.view_version
    ) {
      return false;
    }
    const normalized = normalizeBlocks(messageId, message.turn_id, page);
    const blocksById = { ...path.blocksById, ...normalized.blocks };
    const blockIds = unique([
      ...message.blockIds,
      ...normalized.reference.blockIds,
    ]).sort(
      (left, right) => blocksById[left].ordinal - blocksById[right].ordinal,
    );
    const pages = [
      ...path.pages.filter(
        (candidate) => candidate.id !== normalized.reference.id,
      ),
      normalized.reference,
    ];
    const merged = prunePath(
      {
        ...path,
        pages,
        messagesById: {
          ...path.messagesById,
          [messageId]: {
            ...message,
            blockIds,
            child_cursor: page.newer_cursor,
            children_complete: page.newer_cursor === null,
          },
        },
        blocksById,
      },
      state.activeKey === owner.key,
      "newer",
    );
    set({ paths: { ...state.paths, [owner.key]: merged } });
    return true;
  },
  applyLiveSync: (owner, sync) => {
    const state = get();
    const path = state.paths[owner.key];
    if (!path || path.ownerEpoch !== owner.epoch) return false;
    if (!sync.path || !sync.cursor || !sync.view) {
      set({
        paths: {
          ...state.paths,
          [owner.key]: { ...path, liveOverlay: null },
        },
      });
      return true;
    }
    if (sync.path.conversation_id !== owner.conversationId) {
      return false;
    }
    const blocksById: Record<string, ChatLiveOverlayBlock> = {};
    for (const block of sync.view.blocks) {
      const normalized = normalizeLiveBlock(path, block);
      if (!normalized) return false;
      blocksById[normalized.blockId] = normalized;
    }
    const messagesById: Record<string, Message> = {};
    const messageOrder: string[] = [];
    for (const item of sync.view.messages ?? []) {
      const message = normalizeLiveMessage(
        item.message,
        item.message_id,
        sync.path.turn_id,
      );
      if (!message) continue;
      messagesById[item.message_id] = message;
      messageOrder.push(item.message_id);
    }
    set({
      paths: {
        ...state.paths,
        [owner.key]: {
          ...path,
          liveOverlay: {
            path: sync.path,
            cursor: sync.cursor,
            runState: sync.view.run_state,
            blocksById,
            messagesById,
            messageOrder,
          },
        },
      },
    });
    return true;
  },
  applyLiveEvents: (owner, events) => {
    const state = get();
    const path = state.paths[owner.key];
    if (!path || path.ownerEpoch !== owner.epoch || !path.liveOverlay) {
      return false;
    }
    let overlay = path.liveOverlay;
    for (const event of events) {
      if (!eventMatchesOverlay(overlay, event)) return false;
      if (event.cursor.event_sequence <= overlay.cursor.event_sequence) {
        continue;
      }
      if (event.cursor.event_sequence !== overlay.cursor.event_sequence + 1) {
        return false;
      }
      const blocksById = { ...overlay.blocksById };
      const messagesById = { ...overlay.messagesById };
      let messageOrder = overlay.messageOrder;
      if (event.kind === "block_append") {
        const blockId = event.entity_id;
        const expected = event.expected_offset;
        const text = event.payload.text;
        const messageId = event.payload.message_id;
        if (
          !blockId ||
          expected === null ||
          typeof text !== "string" ||
          typeof messageId !== "string"
        ) {
          return false;
        }
        const existing = blocksById[blockId];
        const baseOffset =
          existing?.baseOffset ?? path.blocksById[blockId]?.total_bytes ?? 0;
        const currentOffset = baseOffset + (existing?.textBytes ?? 0);
        const payloadBytes = textBytes(text);
        if (expected > currentOffset) return false;
        if (
          existing &&
          event.entity_version !== null &&
          event.entity_version < existing.entityVersion &&
          expected >= currentOffset
        ) {
          return false;
        }
        if (expected < currentOffset) {
          if (expected + payloadBytes <= currentOffset) {
            overlay = { ...overlay, cursor: event.cursor };
            continue;
          }
          return false;
        }
        blocksById[blockId] = {
          messageId,
          blockId,
          index:
            typeof event.payload.index === "number"
              ? event.payload.index
              : existing?.index ?? 0,
          kind: existing?.kind ?? "text",
          state: "partial",
          baseOffset,
          text: `${existing?.text ?? ""}${text}`,
          textBytes: (existing?.textBytes ?? 0) + payloadBytes,
          entityVersion: event.entity_version ?? existing?.entityVersion ?? 0,
        };
      } else if (event.kind === "entity_state") {
        const messageId = event.entity_id;
        if (messageId && event.payload.state === "completed") {
          const liveMessage = event.payload.live_message;
          if (liveMessage && typeof liveMessage === "object") {
            const message = normalizeLiveMessage(
              (liveMessage as { message?: unknown }).message,
              messageId,
              overlay.path.turn_id,
            );
            if (message) {
              messagesById[messageId] = message;
              if (!messageOrder.includes(messageId)) {
                messageOrder = [...messageOrder, messageId];
              }
            }
          }
          for (const [blockId, block] of Object.entries(blocksById)) {
            if (block.messageId === messageId) {
              if (messagesById[messageId]) delete blocksById[blockId];
              else blocksById[blockId] = { ...block, state: "completed" };
            }
          }
        }
      } else if (event.kind === "terminal") {
        for (const blockId of Object.keys(blocksById))
          delete blocksById[blockId];
      }
      overlay = {
        ...overlay,
        cursor: event.cursor,
        runState:
          event.kind === "terminal"
            ? String(event.payload.state ?? "completed")
            : overlay.runState,
        blocksById,
        messagesById,
        messageOrder,
      };
    }
    set({
      paths: {
        ...state.paths,
        [owner.key]: { ...path, liveOverlay: overlay },
      },
    });
    return true;
  },
  clearLive: (owner) => {
    const state = get();
    const path = state.paths[owner.key];
    if (!path || path.ownerEpoch !== owner.epoch) return;
    set({
      paths: {
        ...state.paths,
        [owner.key]: { ...path, liveOverlay: null },
      },
    });
  },
  invalidatePath: (owner) => {
    const path = get().paths[owner.key];
    if (!path || path.ownerEpoch !== owner.epoch) return;
    set({
      paths: {
        ...get().paths,
        [owner.key]: emptyPath(owner.epoch),
      },
    });
  },
}));

const placeholderBlocks = new WeakMap<ChatBlockItem, unknown>();

function placeholderBlock(block: ChatBlockItem): unknown {
  if (block.content_loaded) {
    // A Message content array must contain structured content items. The V2
    // storage model intentionally permits scalar text blocks, so normalize
    // those before handing the retained window to the SDK. Leaving a string
    // inside the array makes the response card render its JSON literal form
    // (quotes and escaped newlines) after a page reload.
    if (typeof block.content === "string") {
      return { type: "text", text: block.content };
    }
    return block.content;
  }
  const existing = placeholderBlocks.get(block);
  if (existing) return existing;
  const placeholder = {
    type: "text",
    text: block.preview || "[Content available on demand]",
    metadata: {
      qwenpaw_lazy_block_id: block.block_id,
      qwenpaw_total_bytes: block.total_bytes,
    },
  };
  placeholderBlocks.set(block, placeholder);
  return placeholder;
}

function blockWithLiveOverlay(
  block: ChatBlockItem,
  overlay: ChatLiveOverlayBlock | undefined,
): unknown {
  const content = placeholderBlock(block);
  if (!overlay || !overlay.text) return content;
  if (block.total_bytes >= overlay.baseOffset + overlay.textBytes) {
    return content;
  }
  if (block.total_bytes !== overlay.baseOffset) return content;
  if (typeof block.content === "string") {
    return { type: "text", text: `${block.content}${overlay.text}` };
  }
  if (
    block.content &&
    typeof block.content === "object" &&
    "text" in block.content &&
    typeof block.content.text === "string"
  ) {
    return {
      ...block.content,
      text: `${block.content.text}${overlay.text}`,
    };
  }
  if (overlay.baseOffset === 0) {
    return { type: "text", text: overlay.text };
  }
  return content;
}

function blockForMessageRole(content: unknown, role: string): unknown {
  if (
    role !== "assistant" ||
    !content ||
    typeof content !== "object" ||
    !("type" in content) ||
    content.type !== "text" ||
    !("text" in content) ||
    typeof content.text !== "string"
  ) {
    return content;
  }
  const text = stripScrollHeadlines(content.text);
  return text === content.text ? content : { ...content, text };
}

function terminalStateForTurn(
  turn: NormalizedTurn,
): { status: "canceled" | "failed" } | null {
  if (turn.state === "canceled" || turn.state === "interrupted") {
    return { status: "canceled" };
  }
  if (turn.state === "failed") return { status: "failed" };
  return null;
}

function selectBackendMessages(
  owner: ChatHistoryOwner,
  turnIds: string[],
  includeLive: boolean,
): Message[] {
  const path = useChatHistoryStore.getState().paths[owner.key];
  if (!path || path.ownerEpoch !== owner.epoch) return [];
  const seenMessages = new Set<string>();
  const messages: Message[] = turnIds.flatMap((turnId) => {
    const turn = path.turnsById[turnId];
    if (!turn) return [];
    const terminalState = terminalStateForTurn(turn);
    return turn.messageIds.flatMap((messageId) => {
      const message = path.messagesById[messageId];
      if (!message) return [];
      seenMessages.add(messageId);
      const envelope =
        message.metadata_complete &&
        message.metadata &&
        typeof message.metadata === "object"
          ? message.metadata
          : {};
      const sourceMetadata = envelope.metadata;
      const metadata =
        message.role === "user" && terminalState
          ? {
              ...(sourceMetadata && typeof sourceMetadata === "object"
                ? sourceMetadata
                : {}),
              qwenpaw_turn_state: terminalState,
            }
          : sourceMetadata;
      return [
        {
          ...envelope,
          metadata,
          id: message.message_id,
          chat_turn_id: turnId,
          role: message.role,
          content: message.blockIds
            .map((blockId) => path.blocksById[blockId])
            .filter((block): block is ChatBlockItem => Boolean(block))
            .map((block) =>
              blockForMessageRole(
                blockWithLiveOverlay(
                  block,
                  path.liveOverlay?.blocksById[block.block_id],
                ),
                message.role,
              ),
            ),
        },
      ];
    });
  });
  if (!includeLive || !path.liveOverlay) return messages;
  for (const messageId of path.liveOverlay.messageOrder) {
    if (seenMessages.has(messageId)) continue;
    const message = path.liveOverlay.messagesById[messageId];
    if (!message) continue;
    messages.push(message);
    seenMessages.add(messageId);
  }
  const liveMessages = new Map<string, ChatLiveOverlayBlock[]>();
  for (const block of Object.values(path.liveOverlay.blocksById)) {
    if (seenMessages.has(block.messageId)) continue;
    const blocks = liveMessages.get(block.messageId) ?? [];
    blocks.push(block);
    liveMessages.set(block.messageId, blocks);
  }
  for (const [messageId, blocks] of liveMessages) {
    messages.push({
      id: messageId,
      chat_turn_id: path.liveOverlay.path.turn_id,
      role: "assistant",
      type: "message",
      status: "in_progress",
      content: blocks
        .sort((left, right) => left.index - right.index)
        .map((block) => ({ type: "text", text: block.text })),
    });
  }
  return messages;
}

/** Build all decoded cache messages. Evicted pages never re-enter memory. */
export function selectLoadedBackendMessages(
  owner: ChatHistoryOwner,
): Message[] {
  const path = useChatHistoryStore.getState().paths[owner.key];
  if (!path || path.ownerEpoch !== owner.epoch) return [];
  return selectBackendMessages(owner, path.turnOrder, true);
}

/** Build only one complete-turn MessageList window. */
export function selectWindowBackendMessages(
  owner: ChatHistoryOwner,
  turnIds: string[],
  includeLive: boolean,
): Message[] {
  return selectBackendMessages(owner, turnIds, includeLive);
}
