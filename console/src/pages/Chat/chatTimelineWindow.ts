import type { ChatHistoryPath } from "./chatHistoryStore";

export const TURN_PAGE_LIMIT = 5;
export const MAX_WINDOW_TURNS = 24;
export const MAX_WINDOW_BUBBLES = 48;
export const MAX_WINDOW_BYTES = 2 * 1024 * 1024;

export interface ChatTimelineWindow {
  turnIds: string[];
  bubbleCount: number;
  payloadBytes: number;
  atTail: boolean;
  atOlderLimit: boolean;
}

const textEncoder = new TextEncoder();

function jsonBytes(value: unknown): number {
  if (value === null || value === undefined) return 0;
  try {
    return textEncoder.encode(JSON.stringify(value)).byteLength;
  } catch {
    return 0;
  }
}

function turnCost(
  path: ChatHistoryPath,
  turnId: string,
): { bubbles: number; bytes: number } {
  const turn = path.turnsById[turnId];
  if (!turn) return { bubbles: 0, bytes: 0 };
  let hasUser = false;
  let hasResponse = false;
  let bytes = jsonBytes(turn.usage) + 256;
  for (const messageId of turn.messageIds) {
    const message = path.messagesById[messageId];
    if (!message) continue;
    if (message.role === "user") hasUser = true;
    else hasResponse = true;
    bytes += jsonBytes(message.metadata) + 128;
    for (const blockId of message.blockIds) {
      const block = path.blocksById[blockId];
      if (!block) continue;
      const renderedBytes = block.content_loaded
        ? Math.max(block.total_bytes, 0)
        : jsonBytes(block.preview);
      bytes += renderedBytes + jsonBytes(block.metadata) + 96;
    }
  }
  return {
    bubbles: Number(hasUser) + Number(hasResponse),
    bytes,
  };
}

function buildWindow(
  path: ChatHistoryPath,
  start: number,
  end: number,
): ChatTimelineWindow {
  const turnIds: string[] = [];
  let bubbleCount = 0;
  let payloadBytes = 0;
  for (let index = end; index >= start; index -= 1) {
    const turnId = path.turnOrder[index];
    if (!turnId) continue;
    const cost = turnCost(path, turnId);
    const wouldExceed =
      turnIds.length >= MAX_WINDOW_TURNS ||
      bubbleCount + cost.bubbles > MAX_WINDOW_BUBBLES ||
      payloadBytes + cost.bytes > MAX_WINDOW_BYTES;
    if (wouldExceed && turnIds.length > 0) break;
    turnIds.unshift(turnId);
    bubbleCount += cost.bubbles;
    payloadBytes += cost.bytes;
  }
  const firstIndex = path.turnOrder.indexOf(turnIds[0]);
  const lastIndex = path.turnOrder.indexOf(turnIds[turnIds.length - 1]);
  return {
    turnIds,
    bubbleCount,
    payloadBytes,
    atTail: lastIndex >= path.turnOrder.length - 1 && path.newerCursor === null,
    atOlderLimit: firstIndex === 0 && path.olderCursor === null,
  };
}

function buildWindowForward(
  path: ChatHistoryPath,
  start: number,
  end: number,
): ChatTimelineWindow {
  const turnIds: string[] = [];
  let bubbleCount = 0;
  let payloadBytes = 0;
  for (let index = start; index <= end; index += 1) {
    const turnId = path.turnOrder[index];
    if (!turnId) continue;
    const cost = turnCost(path, turnId);
    const wouldExceed =
      turnIds.length >= MAX_WINDOW_TURNS ||
      bubbleCount + cost.bubbles > MAX_WINDOW_BUBBLES ||
      payloadBytes + cost.bytes > MAX_WINDOW_BYTES;
    if (wouldExceed && turnIds.length > 0) break;
    turnIds.push(turnId);
    bubbleCount += cost.bubbles;
    payloadBytes += cost.bytes;
  }
  const firstIndex = path.turnOrder.indexOf(turnIds[0]);
  const lastIndex = path.turnOrder.indexOf(turnIds[turnIds.length - 1]);
  return {
    turnIds,
    bubbleCount,
    payloadBytes,
    atTail: lastIndex >= path.turnOrder.length - 1 && path.newerCursor === null,
    atOlderLimit: firstIndex === 0 && path.olderCursor === null,
  };
}

export function createTailTimelineWindow(
  path: ChatHistoryPath,
): ChatTimelineWindow {
  const end = path.turnOrder.length - 1;
  const start = Math.max(0, end - TURN_PAGE_LIMIT + 1);
  return buildWindow(path, start, end);
}

export function createTimelineWindowAtTurn(
  path: ChatHistoryPath,
  targetTurnId: string,
): ChatTimelineWindow {
  const targetIndex = path.turnOrder.indexOf(targetTurnId);
  if (targetIndex < 0) {
    throw new Error("Target Chat turn is not loaded");
  }
  const start = Math.max(0, targetIndex - TURN_PAGE_LIMIT + 1);
  return buildWindow(path, start, targetIndex);
}

export function extendTimelineWindowOlder(
  path: ChatHistoryPath,
  current: ChatTimelineWindow,
  move: boolean,
): ChatTimelineWindow {
  if (path.turnOrder.length === 0) return createTailTimelineWindow(path);
  const currentStart = path.turnOrder.indexOf(current.turnIds[0]);
  const currentEnd = path.turnOrder.indexOf(
    current.turnIds[current.turnIds.length - 1],
  );
  if (currentStart < 0 || currentEnd < 0) {
    return createTailTimelineWindow(path);
  }
  const end = move ? Math.max(0, currentEnd - TURN_PAGE_LIMIT) : currentEnd;
  const start = Math.max(0, currentStart - TURN_PAGE_LIMIT);
  return buildWindow(path, start, end);
}

export function extendTimelineWindowNewer(
  path: ChatHistoryPath,
  current: ChatTimelineWindow,
  move: boolean,
): ChatTimelineWindow {
  if (path.turnOrder.length === 0) return createTailTimelineWindow(path);
  const currentStart = path.turnOrder.indexOf(current.turnIds[0]);
  const currentEnd = path.turnOrder.indexOf(
    current.turnIds[current.turnIds.length - 1],
  );
  if (currentStart < 0 || currentEnd < 0) {
    return createTailTimelineWindow(path);
  }
  const lastIndex = path.turnOrder.length - 1;
  const start = move
    ? Math.min(lastIndex, currentStart + TURN_PAGE_LIMIT)
    : currentStart;
  const end = Math.min(lastIndex, currentEnd + TURN_PAGE_LIMIT);
  return buildWindowForward(path, start, end);
}

export function timelineWindowCanExtendOlder(
  path: ChatHistoryPath,
  window: ChatTimelineWindow,
): boolean {
  const firstIndex = path.turnOrder.indexOf(window.turnIds[0]);
  return firstIndex > 0 || path.olderCursor !== null;
}

export function timelineWindowCanExtendNewer(
  path: ChatHistoryPath,
  window: ChatTimelineWindow,
): boolean {
  const lastIndex = path.turnOrder.indexOf(
    window.turnIds[window.turnIds.length - 1],
  );
  return lastIndex < path.turnOrder.length - 1 || path.newerCursor !== null;
}
