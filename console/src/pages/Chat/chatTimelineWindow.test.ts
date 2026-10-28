import { describe, expect, it } from "vitest";
import type { ChatHistoryPath } from "./chatHistoryStore";
import {
  createTailTimelineWindow,
  createTimelineWindowAtTurn,
  extendTimelineWindowNewer,
  extendTimelineWindowOlder,
  MAX_WINDOW_BUBBLES,
  MAX_WINDOW_BYTES,
  MAX_WINDOW_TURNS,
  timelineWindowCanExtendNewer,
  TURN_PAGE_LIMIT,
} from "./chatTimelineWindow";

function historyPath(turns: number, bytesPerTurn = 1024): ChatHistoryPath {
  const turnOrder = Array.from(
    { length: turns },
    (_, index) => `turn-${index}`,
  );
  return {
    ownerEpoch: 1,
    view: null,
    turnsById: Object.fromEntries(
      turnOrder.map((turnId, index) => [
        turnId,
        {
          turn_id: turnId,
          parent_turn_id: index > 0 ? `turn-${index - 1}` : null,
          state: "completed",
          lineage_depth: index,
          usage: null,
          usage_complete: true,
          error_digest: null,
          created_at_us: index,
          updated_at_us: index,
          terminal_at_us: index,
          child_cursor: null,
          children_complete: true,
          messageIds: [`user-${index}`, `assistant-${index}`],
        },
      ]),
    ),
    messagesById: Object.fromEntries(
      turnOrder.flatMap((_turnId, index) =>
        (["user", "assistant"] as const).map((role) => [
          `${role}-${index}`,
          {
            message_id: `${role}-${index}`,
            turn_id: `turn-${index}`,
            role,
            ordinal: role === "user" ? 0 : 1,
            state: "completed",
            metadata: {},
            metadata_complete: true,
            created_at_us: index,
            updated_at_us: index,
            child_cursor: null,
            children_complete: true,
            blockIds: [`${role}-block-${index}`],
          },
        ]),
      ),
    ),
    blocksById: Object.fromEntries(
      turnOrder.flatMap((_turnId, index) =>
        (["user", "assistant"] as const).map((role) => [
          `${role}-block-${index}`,
          {
            block_id: `${role}-block-${index}`,
            message_id: `${role}-${index}`,
            kind: "text",
            ordinal: 0,
            state: "completed",
            total_bytes: bytesPerTurn / 2,
            content_sha256: null,
            entity_version: 1,
            metadata: {},
            metadata_complete: true,
            created_at_us: index,
            updated_at_us: index,
            content: { type: "text", text: role },
            content_loaded: true,
            content_cursor: null,
          },
        ]),
      ),
    ),
    turnOrder,
    pages: [],
    decodedBytes: 0,
    olderCursor: turns > 0 ? "older" : null,
    newerCursor: null,
    liveOverlay: null,
  };
}

describe("chatTimelineWindow", () => {
  it("starts with five complete tail turns", () => {
    const window = createTailTimelineWindow(historyPath(1000));

    expect(window.turnIds).toEqual([
      "turn-995",
      "turn-996",
      "turn-997",
      "turn-998",
      "turn-999",
    ]);
  });

  it("mounts an off-window target instead of the cached tail", () => {
    const window = createTimelineWindowAtTurn(historyPath(1000), "turn-32");

    expect(window.turnIds).toEqual([
      "turn-28",
      "turn-29",
      "turn-30",
      "turn-31",
      "turn-32",
    ]);
    expect(window.atTail).toBe(false);
  });

  it("extends without exceeding turn, bubble, or payload budgets", () => {
    const path = historyPath(1000);
    let window = createTailTimelineWindow(path);
    for (let index = 0; index < 20; index += 1) {
      window = extendTimelineWindowOlder(path, window, false);
    }

    expect(window.turnIds.length).toBeLessThanOrEqual(MAX_WINDOW_TURNS);
    expect(window.bubbleCount).toBeLessThanOrEqual(MAX_WINDOW_BUBBLES);
    expect(window.payloadBytes).toBeLessThanOrEqual(MAX_WINDOW_BYTES);
    expect(window.turnIds).toHaveLength(24);
  });

  it("allows one oversized complete turn to occupy the window", () => {
    const window = createTailTimelineWindow(
      historyPath(1, MAX_WINDOW_BYTES + 1024),
    );

    expect(window.turnIds).toEqual(["turn-0"]);
    expect(window.payloadBytes).toBeGreaterThan(MAX_WINDOW_BYTES);
  });

  it("moves back toward the tail without growing the mounted window", () => {
    const path = historyPath(100);
    let window = createTailTimelineWindow(path);
    for (let index = 0; index < 8; index += 1) {
      window = extendTimelineWindowOlder(path, window, false);
      if (window.turnIds.length === MAX_WINDOW_TURNS) {
        window = extendTimelineWindowOlder(path, window, true);
      }
    }
    expect(window.atTail).toBe(false);
    expect(timelineWindowCanExtendNewer(path, window)).toBe(true);

    const newer = extendTimelineWindowNewer(path, window, true);

    expect(newer.turnIds.length).toBeLessThanOrEqual(MAX_WINDOW_TURNS);
    expect(newer.turnIds[0]).not.toBe(window.turnIds[0]);
    expect(
      Number(newer.turnIds[newer.turnIds.length - 1]?.replace("turn-", "")),
    ).toBe(
      Number(window.turnIds[window.turnIds.length - 1]?.replace("turn-", "")) +
        TURN_PAGE_LIMIT,
    );
  });

  it("does not treat the newest cached turn as the durable tail", () => {
    const path = historyPath(5);
    path.newerCursor = "next-page";

    const window = createTailTimelineWindow(path);

    expect(window.atTail).toBe(false);
    expect(timelineWindowCanExtendNewer(path, window)).toBe(true);
  });
});
