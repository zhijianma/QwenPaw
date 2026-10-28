/**
 * Session API creation lifecycle and pure transforms. Regression family:
 * owner-epoch concurrent creation, failure retry and late-result isolation,
 * session id resolution (local timestamp ids must never be used as backend
 * UUIDs — 404 loops) and message-to-card conversion (history replay must
 * preserve roles/timestamps/attachments).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("@agentscope-ai/chat", () => ({}));

import sessionApi, { __test__ as T } from "./index";
import api, {
  type ChatHistory,
  type ChatSpec,
  type ChatTurnItem,
} from "../../../api";
import { ApiError } from "../../../api/request";
import { createSdkSessionAdapter } from "../sdkSessionAdapter";
import { groupChatsByDate } from "../../../utils/chatGroups";
import type { ExtendedSession } from "../../../stores/sessionListStore";
import { sha256Hex } from "../chatBlockFragments";
import { ACTIVE_PAGE_LIMIT, useChatHistoryStore } from "../chatHistoryStore";
import {
  MAX_WINDOW_BUBBLES,
  MAX_WINDOW_TURNS,
  TURN_PAGE_LIMIT,
} from "../chatTimelineWindow";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function createdChat(id: string): ChatSpec {
  return {
    id,
    name: id,
    session_id: `runtime-${id}`,
    user_id: "default",
    channel: "console",
    created_at: "2026-09-09T00:00:00Z",
    updated_at: "2026-09-09T00:00:00Z",
    meta: {},
  };
}

function paginatedTurn(id: number): ChatTurnItem {
  return {
    turn_id: `turn-${id}`,
    parent_turn_id: id > 0 ? `turn-${id - 1}` : null,
    state: "completed",
    lineage_depth: id,
    usage: null,
    usage_complete: true,
    error_digest: null,
    created_at_us: id,
    updated_at_us: id,
    terminal_at_us: id,
    child_cursor: null,
    children_complete: true,
    messages: [
      {
        message_id: `message-${id}`,
        turn_id: `turn-${id}`,
        role: "user",
        ordinal: 0,
        state: "completed",
        metadata: { type: "message" },
        metadata_complete: true,
        created_at_us: id,
        updated_at_us: id,
        child_cursor: null,
        children_complete: true,
        blocks: [
          {
            block_id: `block-${id}`,
            message_id: `message-${id}`,
            kind: "text",
            ordinal: 0,
            state: "completed",
            total_bytes: 8,
            content_sha256: null,
            entity_version: 1,
            metadata: {},
            metadata_complete: true,
            created_at_us: id,
            updated_at_us: id,
            content: { type: "text", text: `text-${id}` },
            content_loaded: true,
            content_cursor: null,
          },
        ],
      },
    ],
  };
}

function toolDenseTurn(id: number): ChatTurnItem {
  const turn = paginatedTurn(id);
  turn.messages.push(
    ...Array.from({ length: 40 }, (_, index) => ({
      ...turn.messages[0],
      message_id: `assistant-${id}-${index}`,
      role: "assistant",
      ordinal: index + 1,
      blocks: [
        {
          ...turn.messages[0].blocks[0],
          block_id: `assistant-block-${id}-${index}`,
          message_id: `assistant-${id}-${index}`,
          content: { type: "text", text: `tool-output-${id}-${index}` },
        },
      ],
    })),
  );
  return turn;
}

describe("terminal turn metadata conversion", () => {
  it("moves normalized turn identity onto the response card", () => {
    const converted = T.convertMessages([
      {
        role: "user",
        content: [{ type: "text", text: "request" }],
        chat_turn_id: "turn-7",
      },
      {
        role: "assistant",
        type: "message",
        status: "completed",
        content: [{ type: "text", text: "response" }],
      },
    ]);
    const response = converted[1] as unknown as {
      cards: Array<{
        data: { chat_turn_id?: string };
      }>;
    };

    expect(response.cards[0].data.chat_turn_id).toBe("turn-7");
  });

  it.each(["canceled", "failed"])(
    "restores a %s response card from history metadata",
    (status) => {
      const converted = T.convertMessages([
        {
          role: "user",
          content: [{ type: "text", text: "request" }],
          metadata: { qwenpaw_turn_state: { status } },
        },
        {
          role: "assistant",
          type: "reasoning",
          status: "completed",
          content: [{ type: "text", text: "step" }],
        },
      ]);
      const response = converted[1] as unknown as {
        cards: Array<{ data: { status: string } }>;
        msgStatus: string;
      };

      expect(response.cards[0].data.status).toBe(status);
      expect(response.msgStatus).toBe(
        status === "canceled" ? "interrupted" : "finished",
      );
    },
  );
});

beforeEach(() => {
  vi.spyOn(api, "getChatView").mockRejectedValue(
    new ApiError(503, "chat_v2_unavailable"),
  );
});

describe("paginated chat history", () => {
  beforeEach(() => {
    sessionApi.resetForTests();
    sessionApi.setActiveAgent("A");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionApi.resetForTests();
  });

  it("shares concurrent refresh and SDK loads for one Chat owner", async () => {
    const turnPageGate = deferred<ReturnType<typeof paginatedTurn>[] | null>();
    vi.mocked(api.getChatView).mockResolvedValue({
      conversation_id: "copied-chat",
      display_name: "Copied chat",
      lifecycle: "active",
      migration_state: "ready_v2",
      catalog_generation: 1,
      head_turn_id: "turn-1",
      view_version: "view-copied",
      context_revision_id: "context-copied",
      active_run: null,
      capabilities: {
        turn_pagination: true,
        message_pagination: true,
        block_pagination: true,
        block_fragments: true,
        chat_copy: true,
        edit_last_user_message: false,
      },
    });
    const turnPage = vi
      .spyOn(api, "getTurnPage")
      .mockImplementation(async () => ({
        items: (await turnPageGate.promise) ?? [],
        older_cursor: null,
        newer_cursor: null,
        view: {
          conversation_id: "copied-chat",
          head_turn_id: "turn-1",
          view_version: "view-copied",
        },
        bytes: 256,
        truncated_entity: false,
      }));

    const first = sessionApi.refreshSession("copied-chat");
    const second = sessionApi.refreshSession("copied-chat");
    const sdkLoad = sessionApi.getSession("copied-chat");
    await vi.waitFor(() => expect(turnPage).toHaveBeenCalled());
    turnPageGate.resolve([paginatedTurn(1)]);

    await expect(Promise.all([first, second, sdkLoad])).resolves.toEqual([
      expect.objectContaining({ id: "copied-chat" }),
      expect.objectContaining({ id: "copied-chat" }),
      expect.objectContaining({ id: "copied-chat" }),
    ]);
    expect(turnPage).toHaveBeenCalledTimes(1);
  });

  it("loads the tail and replaces it with a bounded older window", async () => {
    vi.mocked(api.getChatView).mockResolvedValue({
      conversation_id: "chat-v2",
      display_name: "V2 chat",
      lifecycle: "active",
      migration_state: "ready_v2",
      catalog_generation: 1,
      head_turn_id: "turn-2",
      view_version: "view-v2",
      context_revision_id: "context-v2",
      active_run: null,
      capabilities: {
        turn_pagination: true,
        message_pagination: true,
        block_pagination: true,
        block_fragments: true,
        edit_last_user_message: false,
      },
    });
    const initialTurn = paginatedTurn(2);
    initialTurn.messages[0].child_cursor = "next-block";
    initialTurn.messages[0].children_complete = false;
    const blockPage = vi.spyOn(api, "getBlockPage").mockResolvedValue({
      items: [
        {
          ...initialTurn.messages[0].blocks[0],
          block_id: "block-2-continuation",
          ordinal: 1,
          content: { type: "text", text: "continued" },
        },
      ],
      older_cursor: "previous-block",
      newer_cursor: null,
      view: {
        conversation_id: "chat-v2",
        head_turn_id: "turn-2",
        view_version: "view-v2",
      },
      bytes: 512,
      truncated_entity: false,
    });
    const turnPage = vi
      .spyOn(api, "getTurnPage")
      .mockResolvedValueOnce({
        items: [initialTurn],
        older_cursor: "older-cursor",
        newer_cursor: null,
        view: {
          conversation_id: "chat-v2",
          head_turn_id: "turn-2",
          view_version: "view-v2",
        },
        bytes: 1024,
        truncated_entity: false,
      })
      .mockResolvedValueOnce({
        items: [paginatedTurn(1)],
        older_cursor: null,
        newer_cursor: "newer-cursor",
        view: {
          conversation_id: "chat-v2",
          head_turn_id: "turn-2",
          view_version: "view-v2",
        },
        bytes: 1024,
        truncated_entity: false,
      });
    const legacy = vi.spyOn(api, "getChat");

    const session = await sessionApi.getSession("chat-v2");
    expect(session).toMatchObject({
      id: "chat-v2",
      name: "V2 chat",
      generating: false,
      historyV2: true,
    });
    expect(session.messages).toHaveLength(1);
    expect(sessionApi.hasPaginatedHistory("chat-v2")).toBe(true);
    expect(legacy).not.toHaveBeenCalled();
    expect(turnPage).toHaveBeenNthCalledWith(
      1,
      "chat-v2",
      expect.objectContaining({ limit: 5 }),
    );
    expect(blockPage).toHaveBeenCalledWith(
      "chat-v2",
      "message-2",
      expect.objectContaining({ cursor: "next-block" }),
    );

    const older = await sessionApi.loadOlderHistory("chat-v2");
    expect(older).toMatchObject({ noMore: true, replacedWindow: true });
    expect(older.messages).toHaveLength(2);
    expect(turnPage).toHaveBeenLastCalledWith(
      "chat-v2",
      expect.objectContaining({
        cursor: "older-cursor",
        headTurnId: "turn-2",
        limit: 5,
        viewVersion: "view-v2",
      }),
    );
  });

  it("bounds SDK messages after loading over 1000 tool-dense messages", async () => {
    const pageCount = 30;
    const lastPage = pageCount - 1;
    const headTurn = pageCount * TURN_PAGE_LIMIT - 1;
    vi.mocked(api.getChatView).mockResolvedValue({
      conversation_id: "chat-tool-dense",
      display_name: "Tool dense chat",
      lifecycle: "active",
      migration_state: "ready_v2",
      catalog_generation: 1,
      head_turn_id: `turn-${headTurn}`,
      view_version: "view-tool-dense",
      context_revision_id: "context-tool-dense",
      active_run: null,
      capabilities: {
        turn_pagination: true,
        message_pagination: true,
        block_pagination: true,
        block_fragments: true,
        chat_copy: true,
        edit_last_user_message: false,
      },
    });
    const turnPage = vi
      .spyOn(api, "getTurnPage")
      .mockImplementation(async (_chatId, options) => {
        if (options.targetTurnId) {
          const target = Number(options.targetTurnId.replace("turn-", ""));
          const firstTurn = Math.max(0, target - TURN_PAGE_LIMIT + 1);
          return {
            items: Array.from({ length: target - firstTurn + 1 }, (_, index) =>
              toolDenseTurn(firstTurn + index),
            ),
            older_cursor: firstTurn > 0 ? `cursor-target-${firstTurn}` : null,
            newer_cursor:
              target < headTurn ? `cursor-target-${target + 1}` : null,
            view: {
              conversation_id: "chat-tool-dense",
              head_turn_id: `turn-${headTurn}`,
              view_version: "view-tool-dense",
            },
            bytes: 64 * 1024,
            truncated_entity: false,
          };
        }
        const pageIndex = options.cursor
          ? Number(options.cursor.replace("cursor-", ""))
          : lastPage;
        const firstTurn = pageIndex * TURN_PAGE_LIMIT;
        return {
          items: Array.from({ length: TURN_PAGE_LIMIT }, (_, index) =>
            toolDenseTurn(firstTurn + index),
          ),
          older_cursor: pageIndex > 0 ? `cursor-${pageIndex - 1}` : null,
          newer_cursor: pageIndex < lastPage ? `cursor-${pageIndex + 1}` : null,
          view: {
            conversation_id: "chat-tool-dense",
            head_turn_id: `turn-${headTurn}`,
            view_version: "view-tool-dense",
          },
          bytes: 64 * 1024,
          truncated_entity: false,
        };
      });

    const initial = await sessionApi.getSession("chat-tool-dense");
    let maxSdkMessages = initial.messages.length;
    let maxUserAnchors = initial.messages.filter(
      (message) => message.role === "user",
    ).length;
    for (let attempt = 0; attempt < pageCount * 3; attempt += 1) {
      const result = await sessionApi.loadOlderHistory("chat-tool-dense");
      maxSdkMessages = Math.max(maxSdkMessages, result.messages.length);
      maxUserAnchors = Math.max(
        maxUserAnchors,
        result.messages.filter((message) => message.role === "user").length,
      );
      if (turnPage.mock.calls.length === pageCount) break;
    }

    const activeKey = useChatHistoryStore.getState().activeKey;
    const path = activeKey
      ? useChatHistoryStore.getState().paths[activeKey]
      : undefined;
    expect(turnPage).toHaveBeenCalledTimes(pageCount);
    expect(Object.keys(path?.messagesById ?? {})).toHaveLength(
      ACTIVE_PAGE_LIMIT * TURN_PAGE_LIMIT * 41,
    );
    expect(Object.keys(path?.messagesById ?? {}).length).toBeGreaterThan(1000);
    expect(path?.pages).toHaveLength(ACTIVE_PAGE_LIMIT);
    expect(maxSdkMessages).toBe(MAX_WINDOW_BUBBLES);
    expect(maxUserAnchors).toBe(MAX_WINDOW_TURNS);

    let newer = await sessionApi.loadNewerHistory("chat-tool-dense");
    for (
      let attempt = 1;
      attempt < pageCount * 3 && !newer.atTail;
      attempt += 1
    ) {
      expect(newer.messages.length).toBeLessThanOrEqual(MAX_WINDOW_BUBBLES);
      newer = await sessionApi.loadNewerHistory("chat-tool-dense");
    }
    expect(newer.atTail).toBe(true);
    expect(newer.messages.some((message) => message.id.includes("149"))).toBe(
      true,
    );
    const newerPath = activeKey
      ? useChatHistoryStore.getState().paths[activeKey]
      : undefined;
    expect(newerPath?.pages.length).toBeLessThanOrEqual(ACTIVE_PAGE_LIMIT);

    const target = await sessionApi.loadHistoryAtTurn(
      "chat-tool-dense",
      "turn-32",
    );
    expect(target).toMatchObject({
      atTail: false,
      replacedWindow: true,
      targetBubbleId: "message-32",
      turnCount: TURN_PAGE_LIMIT,
    });
    expect(target.messages).toHaveLength(TURN_PAGE_LIMIT * 2);
    expect(target.messages.some((message) => message.id === "message-32")).toBe(
      true,
    );
    expect(target.messages.some((message) => message.id.includes("149"))).toBe(
      false,
    );
    expect(turnPage).toHaveBeenLastCalledWith(
      "chat-tool-dense",
      expect.objectContaining({
        targetTurnId: "turn-32",
      }),
    );

    const latest = await sessionApi.returnToLatestHistory("chat-tool-dense");
    expect(latest).toMatchObject({
      atTail: true,
      replacedWindow: true,
      turnCount: TURN_PAGE_LIMIT,
    });
    expect(latest.messages).toHaveLength(TURN_PAGE_LIMIT * 2);
    expect(turnPage).toHaveBeenLastCalledWith(
      "chat-tool-dense",
      expect.not.objectContaining({ cursor: expect.anything() }),
    );
  });

  it.each(["chat_v2_unavailable", "chat_v2_ui_disabled"])(
    "uses compatible history only for %s",
    async (code) => {
      vi.mocked(api.getChatView).mockRejectedValue(new ApiError(503, code));
      const legacy = vi.spyOn(api, "getChat").mockResolvedValue({
        messages: [],
        status: "idle",
      } as ChatHistory);

      await sessionApi.getSession("chat-fallback");

      expect(legacy).toHaveBeenCalledTimes(1);
    },
  );

  it("does not turn an unrelated 503 into a full transcript read", async () => {
    const failure = new ApiError(503, "backend overloaded");
    vi.mocked(api.getChatView).mockRejectedValue(failure);
    const legacy = vi.spyOn(api, "getChat");

    await expect(sessionApi.getSession("chat-overloaded")).rejects.toBe(
      failure,
    );
    expect(legacy).not.toHaveBeenCalled();
  });

  it("loads a scoped lazy block fragment and verifies its payload", async () => {
    const bytes = new TextEncoder().encode(
      JSON.stringify({ type: "text", text: "lazy content" }),
    );
    const digest = await sha256Hex(bytes);
    const turn = paginatedTurn(1);
    turn.messages[0].blocks[0] = {
      ...turn.messages[0].blocks[0],
      total_bytes: bytes.byteLength,
      content_sha256: digest,
      content: null,
      content_loaded: false,
      content_cursor: "fragment-cursor",
      preview: "lazy preview",
    };
    vi.mocked(api.getChatView).mockResolvedValue({
      conversation_id: "chat-v2",
      display_name: "V2 chat",
      lifecycle: "active",
      migration_state: "ready_v2",
      catalog_generation: 1,
      head_turn_id: "turn-1",
      view_version: "view-v2",
      context_revision_id: "context-v2",
      active_run: null,
      capabilities: {
        turn_pagination: true,
        message_pagination: true,
        block_pagination: true,
        block_fragments: true,
        edit_last_user_message: false,
      },
    });
    vi.spyOn(api, "getTurnPage").mockResolvedValue({
      items: [turn],
      older_cursor: null,
      newer_cursor: null,
      view: {
        conversation_id: "chat-v2",
        head_turn_id: "turn-1",
        view_version: "view-v2",
      },
      bytes: 256,
      truncated_entity: true,
    });
    const fragment = vi.spyOn(api, "getBlockFragment").mockResolvedValue({
      items: [
        {
          block_id: "block-1",
          entity_version: 1,
          start_byte: 0,
          end_byte: bytes.byteLength,
          total_bytes: bytes.byteLength,
          payload_base64: btoa(String.fromCharCode(...bytes)),
          payload_encoding: "base64",
          fragment_sha256: digest,
          content_sha256: digest,
        },
      ],
      older_cursor: null,
      newer_cursor: null,
      view: {
        conversation_id: "chat-v2",
        head_turn_id: "turn-1",
        view_version: "view-v2",
      },
      bytes: 256,
      truncated_entity: false,
    });

    await sessionApi.getSession("chat-v2");
    const loaded = await sessionApi.loadBlockFragment("block-1", null, 0);

    expect(fragment).toHaveBeenCalledWith(
      "chat-v2",
      "block-1",
      expect.objectContaining({
        cursor: "fragment-cursor",
        headTurnId: "turn-1",
        viewVersion: "view-v2",
        agentId: "A",
      }),
    );
    expect(new TextDecoder().decode(loaded.bytes)).toContain("lazy content");
    expect(loaded.nextCursor).toBeNull();
  });

  it("streams a threshold tail into the visible normalized session", async () => {
    sessionApi.setVisibleSession("chat-v2");
    vi.mocked(api.getChatView).mockResolvedValue({
      conversation_id: "chat-v2",
      display_name: "V2 chat",
      lifecycle: "active",
      migration_state: "ready_v2",
      catalog_generation: 1,
      head_turn_id: "turn-1",
      view_version: "view-v2",
      context_revision_id: "context-v2",
      active_run: { run_id: "run-v2", state: "running" },
      capabilities: {
        turn_pagination: true,
        message_pagination: true,
        block_pagination: true,
        block_fragments: true,
        live_resync: true,
        edit_last_user_message: false,
      },
    });
    vi.spyOn(api, "getTurnPage").mockResolvedValue({
      items: [paginatedTurn(1)],
      older_cursor: null,
      newer_cursor: null,
      view: {
        conversation_id: "chat-v2",
        head_turn_id: "turn-1",
        view_version: "view-v2",
      },
      bytes: 256,
      truncated_entity: false,
    });
    const wire =
      "event: sync\ndata: " +
      JSON.stringify({
        kind: "sync",
        path: {
          conversation_id: "chat-v2",
          run_id: "run-v2",
          turn_id: "turn-1",
        },
        cursor: { run_epoch: "epoch-v2", event_sequence: 1 },
        view: {
          run_state: "running",
          blocks: [
            {
              message_id: "live-message",
              block_id: "live-block",
              index: 0,
              kind: "text",
              state: "partial",
              durable_bytes: 0,
              total_bytes: 7,
              entity_version: 1,
              pending: [{ start_byte: 0, entity_version: 1, text: "pending" }],
            },
          ],
        },
        resync_reason: null,
        terminal: false,
      }) +
      "\n\n";
    const encoded = new TextEncoder().encode(wire);
    let canceled = false;
    vi.spyOn(api, "openChatLive").mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(encoded);
          },
          cancel() {
            canceled = true;
          },
        }),
        { status: 200 },
      ),
    );
    const published = vi.fn();
    sessionApi.onLiveMessages = published;

    await sessionApi.getSession("chat-v2");
    await vi.waitFor(() => expect(published).toHaveBeenCalled());

    expect(api.openChatLive).toHaveBeenCalledWith(
      "chat-v2",
      expect.objectContaining({
        cursor: null,
        agentId: "A",
      }),
    );
    const lastCall = published.mock.calls[published.mock.calls.length - 1];
    expect(lastCall?.[1]).toEqual(
      expect.arrayContaining([expect.objectContaining({ role: "assistant" })]),
    );
    expect(sessionApi.hasNormalizedLive("chat-v2")).toBe(true);
    const terminal = sessionApi.waitForNormalizedLiveTerminal("chat-v2");
    sessionApi.setVisibleSession(null);
    await terminal;
    await vi.waitFor(() => expect(canceled).toBe(true));
  });

  it("re-reads durable history after a terminal sync", async () => {
    sessionApi.setVisibleSession("chat-v2");
    const activeView = {
      conversation_id: "chat-v2",
      display_name: "V2 chat",
      lifecycle: "active",
      migration_state: "ready_v2",
      catalog_generation: 1,
      head_turn_id: "turn-1",
      view_version: "view-v2",
      context_revision_id: "context-v2",
      active_run: { run_id: "run-v2", state: "running" },
      capabilities: {
        turn_pagination: true,
        message_pagination: true,
        block_pagination: true,
        block_fragments: true,
        live_resync: true,
        edit_last_user_message: false,
      },
    };
    vi.mocked(api.getChatView)
      .mockResolvedValueOnce(activeView)
      .mockResolvedValueOnce({ ...activeView, active_run: null });
    const completed = paginatedTurn(1);
    completed.messages.push({
      ...completed.messages[0],
      message_id: "assistant-message",
      role: "assistant",
      ordinal: 1,
      blocks: [
        {
          ...completed.messages[0].blocks[0],
          block_id: "assistant-block",
          message_id: "assistant-message",
          content: { type: "text", text: "completed" },
        },
      ],
    });
    vi.spyOn(api, "getTurnPage")
      .mockResolvedValueOnce({
        items: [paginatedTurn(1)],
        older_cursor: null,
        newer_cursor: null,
        view: {
          conversation_id: "chat-v2",
          head_turn_id: "turn-1",
          view_version: "view-v2",
        },
        bytes: 256,
        truncated_entity: false,
      })
      .mockResolvedValueOnce({
        items: [completed],
        older_cursor: null,
        newer_cursor: null,
        view: {
          conversation_id: "chat-v2",
          head_turn_id: "turn-1",
          view_version: "view-v2",
        },
        bytes: 512,
        truncated_entity: false,
      });
    const terminalSync =
      "event: sync\ndata: " +
      JSON.stringify({
        kind: "sync",
        path: {
          conversation_id: "chat-v2",
          run_id: "run-v2",
          turn_id: "turn-1",
        },
        cursor: { run_epoch: "epoch-v2", event_sequence: 4 },
        view: { run_state: "completed", blocks: [] },
        resync_reason: null,
        terminal: true,
      }) +
      "\n\n";
    vi.spyOn(api, "openChatLive").mockResolvedValue(
      new Response(terminalSync, { status: 200 }),
    );
    const published = vi.fn();
    sessionApi.onLiveMessages = published;

    await sessionApi.getSession("chat-v2");
    await vi.waitFor(() => {
      expect(api.getChatView).toHaveBeenCalledTimes(2);
      const calls = published.mock.calls;
      expect(calls[calls.length - 1]?.[1]).toHaveLength(2);
    });
  });
});

describe("createSession owner-epoch singleflight", () => {
  beforeEach(() => {
    sessionApi.resetForTests();
    sessionApi.setActiveAgent("A");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionApi.resetForTests();
  });

  it("keeps a live usage turn through first-send URL resolution and history loading", async () => {
    const { useTurnUsageStore } = await import("../turnUsageStore");
    sessionApi.setVisibleSession(null);
    vi.spyOn(sessionApi, "getBackendSessionId").mockReturnValue("runtime-one");
    const turn = useTurnUsageStore.getState().beginTurn("A", "runtime-one");
    const live = { usage: { total_tokens: 125 }, context_usage: null };
    useTurnUsageStore.getState().setSnapshotForTurn(live, turn);
    sessionApi.setVisibleSession("one");
    vi.spyOn(api, "getChat").mockResolvedValue({
      messages: [],
      status: "idle",
    } as ChatHistory);
    await sessionApi.getSession("one");
    expect(useTurnUsageStore.getState().activeTurn).toEqual(turn);
    expect(useTurnUsageStore.getState().snapshot).toEqual(live);
    sessionApi.setVisibleSession(null);
    expect(useTurnUsageStore.getState().activeTurn).toBeNull();
  });

  it("shares concurrent first sends and publishes the created Chat once", async () => {
    const pending = deferred<ChatSpec>();
    const create = vi.spyOn(api, "createChat").mockReturnValue(pending.promise);
    const selected = vi.fn();
    sessionApi.onSessionCreated = selected;
    const bound = sessionApi.bindToOwner();
    const results = Promise.all([
      sessionApi.createSession({ name: "first" }),
      bound.createSession({ name: "second" }),
      sessionApi.createSession({ name: "third" }),
    ]);
    pending.resolve(createdChat("chat-one"));
    const sessions = await results;

    expect(create).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({ name: "first" }),
    );
    expect(sessions[1]).toBe(sessions[0]);
    expect(sessions[2]).toBe(sessions[0]);
    sessionApi.activateCreatedSession("chat-one");
    expect(selected).toHaveBeenCalledExactlyOnceWith("chat-one");
    expect(sessions[0]).toMatchObject({
      session: { id: "chat-one", sessionId: "runtime-chat-one" },
      sessions: [{ id: "chat-one" }],
    });
  });

  it("creates another Chat after the previous creation has completed", async () => {
    const create = vi
      .spyOn(api, "createChat")
      .mockResolvedValueOnce(createdChat("chat-one"))
      .mockResolvedValueOnce(createdChat("chat-two"));
    await sessionApi.createSession({ name: "first" });
    const next = await sessionApi.createSession({ name: "second" });

    expect(create).toHaveBeenCalledTimes(2);
    expect(next.session.id).toBe("chat-two");
    expect(next.sessions.map((session) => session.id)).toEqual([
      "chat-two",
      "chat-one",
    ]);
  });

  it("publishes a newly created Chat in Today before any list refresh", async () => {
    const now = new Date().toISOString();
    const chat = {
      ...createdChat("chat-today"),
      created_at: now,
      updated_at: now,
    };
    vi.spyOn(api, "createChat").mockResolvedValue(chat);
    const result = await sessionApi.createSession({ name: "first message" });

    const groups = groupChatsByDate(result.sessions as ExtendedSession[]);
    expect(groups.map((group) => group.key)).toEqual(["today"]);
    expect(result.session).toMatchObject({
      createdAt: chat.created_at,
      updatedAt: chat.updated_at,
    });
  });

  it("rejects all waiters on failure and allows a fresh concurrent retry", async () => {
    const pending = deferred<ChatSpec>();
    const failure = new Error("create failed");
    const create = vi.spyOn(api, "createChat").mockReturnValue(pending.promise);
    const selected = vi.fn();
    sessionApi.onSessionCreated = selected;
    const failed = Promise.allSettled([
      sessionApi.createSession({ name: "first" }),
      sessionApi.createSession({ name: "second" }),
    ]);
    pending.reject(failure);
    expect(await failed).toEqual([
      { status: "rejected", reason: failure },
      { status: "rejected", reason: failure },
    ]);
    expect(selected).not.toHaveBeenCalled();
    const attemptsBeforeRetry = create.mock.calls.length;
    const retry = deferred<ChatSpec>();
    create.mockReturnValue(retry.promise);
    const retried = Promise.all([
      sessionApi.createSession({ name: "retry" }),
      sessionApi.createSession({ name: "retry again" }),
    ]);
    retry.resolve(createdChat("retry-chat"));
    const results = await retried;

    expect(attemptsBeforeRetry).toBe(1);
    expect(create).toHaveBeenCalledTimes(2);
    expect(results[0]).toBe(results[1]);
    sessionApi.activateCreatedSession("retry-chat");
    expect(selected).toHaveBeenCalledExactlyOnceWith("retry-chat");
  });

  it.each(["resolve", "reject"] as const)(
    "does not let an old A %s clear the new A epoch's pending creation",
    async (settle) => {
      const old = deferred<ChatSpec>();
      const current = deferred<ChatSpec>();
      const create = vi.spyOn(api, "createChat").mockReturnValue(old.promise);
      const selected = vi.fn();
      sessionApi.onSessionCreated = selected;
      const oldResult = Promise.allSettled([
        sessionApi.createSession({ name: "old A" }),
      ]);
      const oldBound = sessionApi.bindToOwner();
      sessionApi.setActiveAgent("B");
      sessionApi.setActiveAgent("A");
      create.mockReturnValue(current.promise);
      const newResult = sessionApi.createSession({ name: "new A" });
      const identityBefore =
        sessionApi.getSessionIdentity("fresh-chat").sessionId;
      if (settle === "resolve") old.resolve(createdChat("stale-chat"));
      else old.reject(new Error("old failure"));
      const [stale] = await oldResult;
      expect(stale.status).toBe("rejected");
      if (settle === "resolve" && stale.status === "rejected") {
        expect(stale.reason).toMatchObject({ name: "AbortError" });
      }
      expect(selected).not.toHaveBeenCalled();
      expect(sessionApi.getSessionIdentity("fresh-chat").sessionId).toBe(
        identityBefore,
      );
      await expect(oldBound.createSession({})).rejects.toMatchObject({
        name: "AbortError",
      });
      const joined = sessionApi.createSession({ name: "join new A" });
      current.resolve(createdChat("fresh-chat"));
      const [result, joinedResult] = await Promise.all([newResult, joined]);

      expect(create).toHaveBeenCalledTimes(2);
      expect(joinedResult).toBe(result);
      expect(result.sessions.map((session) => session.id)).toEqual([
        "fresh-chat",
      ]);
      expect(sessionApi.getSessionIdentity("fresh-chat").sessionId).toBe(
        "runtime-fresh-chat",
      );
      sessionApi.activateCreatedSession("fresh-chat");
      expect(selected).toHaveBeenCalledExactlyOnceWith("fresh-chat");
    },
  );

  it("rejects late old-A success after new-A success without changing identity or list", async () => {
    const old = deferred<ChatSpec>();
    const create = vi.spyOn(api, "createChat").mockReturnValue(old.promise);
    const selected = vi.fn();
    sessionApi.onSessionCreated = selected;
    const stale = Promise.allSettled([sessionApi.createSession({})]);
    sessionApi.setActiveAgent("B");
    sessionApi.setActiveAgent("A");
    create.mockResolvedValue(createdChat("fresh-chat"));
    const fresh = await sessionApi.createSession({});
    old.resolve(createdChat("stale-chat"));
    expect(await stale).toEqual([
      {
        status: "rejected",
        reason: expect.objectContaining({ name: "AbortError" }),
      },
    ]);
    expect(sessionApi.getSessionIdentity("fresh-chat").sessionId).toBe(
      "runtime-fresh-chat",
    );
    sessionApi.activateCreatedSession("fresh-chat");
    expect(selected).toHaveBeenCalledExactlyOnceWith("fresh-chat");
    create.mockResolvedValue(createdChat("next-chat"));
    const next = await sessionApi.createSession({});
    expect(fresh.session.id).toBe("fresh-chat");
    expect(next.sessions.map((session) => session.id)).toEqual([
      "next-chat",
      "fresh-chat",
    ]);
  });
});

describe("bound session history owner epochs", () => {
  beforeEach(() => {
    sessionApi.resetForTests();
    sessionApi.setActiveAgent("A");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionApi.resetForTests();
  });

  it("suppresses A1's late idle observer after A -> B -> A2 while preserving direct getSession results", async () => {
    const chatId = "11111111-1111-4111-8111-111111111111";
    const pending = deferred<ChatHistory>();
    const history = vi.spyOn(api, "getChat").mockReturnValue(pending.promise);
    const clearLoading = vi.fn();
    const oldObserver = vi.fn((_id, session) => {
      if (session && !session.generating) clearLoading();
    });
    const oldBound = sessionApi.bindToOwner();
    const oldAdapter = createSdkSessionAdapter(oldBound, oldObserver);
    // Use the real SessionApi and adapter; defer only the backend history.
    const boundResult = oldAdapter.api.getSession(chatId);
    const directResult = sessionApi.getSession(chatId);
    await vi.waitFor(() => expect(history).toHaveBeenCalledTimes(1));

    sessionApi.setActiveAgent("B");
    sessionApi.setActiveAgent("A");
    history.mockResolvedValue({ messages: [], status: "running" });
    const currentObserver = vi.fn();
    const currentAdapter = createSdkSessionAdapter(
      sessionApi.bindToOwner(),
      currentObserver,
    );
    const current = await currentAdapter.api.getSession(chatId);
    expect(current).toMatchObject({ id: chatId, generating: true });
    expect(currentAdapter.isReady(chatId)).toBe(true);
    expect(currentObserver).toHaveBeenCalledExactlyOnceWith(chatId, current);

    pending.resolve({ messages: [], status: "idle" });
    const [staleBound, staleDirect] = await Promise.all([
      boundResult,
      directResult,
    ]);
    expect(oldObserver).not.toHaveBeenCalled();
    expect(clearLoading).not.toHaveBeenCalled();
    expect(staleBound).toBeUndefined();
    expect(oldAdapter.isReady(chatId)).toBe(false);
    expect(staleDirect).toMatchObject({ id: chatId, generating: false });
    expect(currentAdapter.isReady(chatId)).toBe(true);
    expect(currentObserver).toHaveBeenCalledTimes(1);

    // The original pre-call guard must also reject newly invoked stale APIs.
    await expect(oldBound.getSession(chatId)).resolves.toBeUndefined();
    expect(history).toHaveBeenCalledTimes(2);
  });

  it("still delivers current-owner idle history to the observer", async () => {
    const chatId = "22222222-2222-4222-8222-222222222222";
    vi.spyOn(api, "getChat").mockResolvedValue({
      messages: [],
      status: "idle",
    });
    const observer = vi.fn();
    const adapter = createSdkSessionAdapter(sessionApi.bindToOwner(), observer);
    const session = await adapter.api.getSession(chatId);

    expect(session).toMatchObject({ id: chatId, generating: false });
    expect(observer).toHaveBeenCalledExactlyOnceWith(chatId, session);
    expect(adapter.isReady(chatId)).toBe(true);
  });
});

type SessionLike = {
  id: string;
  name?: string;
  sessionId?: string;
  realId?: string;
};

const msg = (over: Record<string, unknown> = {}) => ({
  id: "m1",
  role: "user",
  type: "message",
  content: "hello",
  metadata: null,
  ...over,
});

describe("parseTimestamp / parseFinishedAt", () => {
  it("parses a metadata timestamp to unix seconds", () => {
    const m = msg({ metadata: { timestamp: "2026-05-27 10:44:53.362" } });
    const sec = T.parseTimestamp(m as never);
    expect(sec).toBe(
      Math.floor(new Date("2026-05-27T10:44:53.362").getTime() / 1000),
    );
  });

  it("returns 0 for missing metadata", () => {
    expect(T.parseTimestamp(msg() as never)).toBe(0);
    expect(T.parseFinishedAt(msg() as never)).toBe(0);
  });

  it("returns 0 for unparseable strings", () => {
    const m = msg({ metadata: { timestamp: "not a date", finished_at: 42 } });
    expect(T.parseTimestamp(m as never)).toBe(0);
    expect(T.parseFinishedAt(m as never)).toBe(0);
  });

  it("reads finished_at independently of timestamp", () => {
    const m = msg({
      metadata: {
        timestamp: "2026-01-01 00:00:00",
        finished_at: "2026-01-02 00:00:00",
      },
    });
    const ts = T.parseTimestamp(m as never);
    const fa = T.parseFinishedAt(m as never);
    expect(fa).toBeGreaterThan(ts);
  });
});

describe("extractTextFromContent", () => {
  it("returns strings as-is", () => {
    expect(T.extractTextFromContent("plain")).toBe("plain");
  });

  it("joins text items and ignores other types", () => {
    expect(
      T.extractTextFromContent([
        { type: "text", text: "a" },
        { type: "image" },
        { type: "text", text: "b" },
        { type: "text" },
      ]),
    ).toBe("a\nb");
  });

  it("stringifies non-array non-string content", () => {
    expect(T.extractTextFromContent(null)).toBe("");
    expect(T.extractTextFromContent(5)).toBe("5");
  });
});

describe("contentToRequestParts", () => {
  it("wraps plain strings into a text part", () => {
    expect(T.contentToRequestParts("hi")).toEqual([
      { type: "text", text: "hi", status: "created" },
    ]);
  });

  it("returns a single empty text part for empty arrays", () => {
    expect(T.contentToRequestParts([])).toEqual([
      { type: "text", text: "", status: "created" },
    ]);
  });

  it("tags every part with created status", () => {
    const parts = T.contentToRequestParts([{ type: "text", text: "x" }]);
    expect(parts[0].status).toBe("created");
  });
});

describe("toOutputMessage", () => {
  it("maps system plugin_call_output to the tool role", () => {
    const out = T.toOutputMessage(
      msg({ role: "system", type: "plugin_call_output" }) as never,
    );
    expect(out.role).toBe("tool");
  });

  it("keeps other roles untouched and nulls missing metadata", () => {
    const out = T.toOutputMessage(
      msg({ role: "assistant", metadata: undefined }) as never,
    );
    expect(out.role).toBe("assistant");
    expect(out.metadata).toBeNull();
  });
});

describe("buildUserCard", () => {
  it("uses the message id and parses the created timestamp", () => {
    const card = T.buildUserCard(
      msg({
        id: "fixed-id",
        metadata: { timestamp: "2026-01-01 00:00:00" },
      }) as never,
    );
    expect(card.id).toBe("fixed-id");
    expect(card.role).toBe("user");
    expect(card.cards![0].code).toBe("AgentScopeRuntimeRequestCard");
    expect(card.cards![0].data.created_at).toBeGreaterThan(0);
    expect(card.cards![0].data.input[0].content).toEqual([
      { type: "text", text: "hello", status: "created" },
    ]);
  });

  it("generates an id when the message has none", () => {
    const card = T.buildUserCard(msg({ id: "" }) as never);
    expect(card.id).toBeTruthy();
  });

  it("preserves normalized turn identity for request actions", () => {
    const card = T.buildUserCard(
      msg({
        chat_turn_id: "turn-7",
      }) as never,
    );
    const data = card.cards![0].data as {
      chat_turn_id?: string;
    };

    expect(data.chat_turn_id).toBe("turn-7");
  });
});

describe("isLocalTimestamp", () => {
  it("recognizes local timestamp-random ids", () => {
    expect(T.isLocalTimestamp("1735689600000-abc12")).toBe(true);
  });

  it("rejects backend UUIDs", () => {
    expect(T.isLocalTimestamp("550e8400-e29b-41d4-a716-446655440000")).toBe(
      false,
    );
    expect(T.isLocalTimestamp("")).toBe(false);
  });
});

describe("isGenerating", () => {
  it("is true only for an explicit running status", () => {
    expect(T.isGenerating({ status: "running" } as never)).toBe(true);
  });

  it("treats missing status as idle (no false reconnects)", () => {
    expect(T.isGenerating({} as never)).toBe(false);
    expect(T.isGenerating({ status: "idle" } as never)).toBe(false);
    expect(T.isGenerating({ status: undefined } as never)).toBe(false);
  });
});

describe("resolveRealId", () => {
  const local = (id: string, over: Partial<SessionLike> = {}) =>
    ({
      id,
      name: id,
      sessionId: undefined,
      realId: undefined,
      ...over,
    }) as SessionLike;

  const LOCAL_ID = "1735689600000-abc12"; // local timestamp-random id

  it("returns an already-resolved realId without mutating the list", () => {
    const list = [local(LOCAL_ID, { realId: "uuid-1" })];
    const { list: out, realId } = T.resolveRealId(list as never, LOCAL_ID);
    expect(realId).toBe("uuid-1");
    expect(out).toBe(list);
  });

  it("links a backend chat whose session_id matches the temp id", () => {
    const placeholder = local(LOCAL_ID);
    const backend = { id: "uuid-2", name: "b", sessionId: LOCAL_ID };
    const { list, realId } = T.resolveRealId(
      [placeholder, backend] as never,
      LOCAL_ID,
    );
    expect(realId).toBe("uuid-2");
    // resolved entry moves to the front and adopts the local id
    expect(list[0].id).toBe(LOCAL_ID);
    expect((list[0] as SessionLike).realId).toBe("uuid-2");
  });

  it("never returns a local timestamp id as the real id", () => {
    const placeholder = local(LOCAL_ID);
    const { realId } = T.resolveRealId([placeholder] as never, LOCAL_ID);
    expect(realId).toBeNull();
  });

  it("returns null when nothing matches", () => {
    const { realId } = T.resolveRealId(
      [local("other-uuid")] as never,
      LOCAL_ID,
    );
    expect(realId).toBeNull();
  });

  it("does not use a placeholder sharing the temp id as the backend uuid", () => {
    const placeholder = local(LOCAL_ID);
    const backendSameId = { id: LOCAL_ID, sessionId: LOCAL_ID };
    const { realId } = T.resolveRealId(
      [placeholder, backendSameId] as never,
      LOCAL_ID,
    );
    // branch 2 skips id===tempSessionId; branch 3 finds the placeholder,
    // whose local-timestamp id must never be treated as a backend UUID.
    expect(realId).toBeNull();
  });
});

describe("normalizeOutputMessageContent", () => {
  it("passes strings through", () => {
    expect(T.normalizeOutputMessageContent("x")).toBe("x");
  });

  it("keeps non-array content unchanged", () => {
    expect(T.normalizeOutputMessageContent(42)).toBe(42);
  });
});

describe("contentToRequestParts", () => {
  it("wraps string content into a created text part", () => {
    expect(T.contentToRequestParts("hi")).toEqual([
      { type: "text", text: "hi", status: "created" },
    ]);
  });

  it("stringifies non-string, non-array content", () => {
    expect(T.contentToRequestParts(null)).toEqual([
      { type: "text", text: "", status: "created" },
    ]);
    expect(T.contentToRequestParts(7)).toEqual([
      { type: "text", text: "7", status: "created" },
    ]);
  });

  it("returns a single empty text part for an empty array", () => {
    expect(T.contentToRequestParts([])).toEqual([
      { type: "text", text: "", status: "created" },
    ]);
  });

  it("resolves image content urls and marks parts created", () => {
    const parts = T.contentToRequestParts([
      { type: "image", image_url: "/files/a.png" },
    ]);
    expect(parts).toHaveLength(1);
    expect(parts[0].status).toBe("created");
    expect(String(parts[0].image_url)).toContain("/files/a.png");
  });

  it("resolves audio data urls", () => {
    const parts = T.contentToRequestParts([
      { type: "audio", data: "/files/b.mp3" },
    ]);
    expect(String(parts[0].data)).toContain("/files/b.mp3");
  });

  it("resolves video urls", () => {
    const parts = T.contentToRequestParts([
      { type: "video", video_url: "/files/c.mp4" },
    ]);
    expect(String(parts[0].video_url)).toContain("/files/c.mp4");
  });

  it("resolves file urls from file_url or file_id with a fallback name", () => {
    const byUrl = T.contentToRequestParts([
      { type: "file", file_url: "/files/d.pdf", filename: "d.pdf" },
    ]);
    expect(String(byUrl[0].file_url)).toContain("/files/d.pdf");
    expect(byUrl[0].file_name).toBe("d.pdf");

    const byId = T.contentToRequestParts([
      { type: "file", file_id: "fid-1", file_name: "named.bin" },
    ]);
    expect(String(byId[0].file_url)).toContain("fid-1");
    expect(byId[0].file_name).toBe("named.bin");

    const unnamed = T.contentToRequestParts([
      { type: "file", file_id: "fid-2" },
    ]);
    expect(unnamed[0].file_name).toBe("file");
  });

  it("passes through content items with no url fields", () => {
    const parts = T.contentToRequestParts([{ type: "text", text: "plain" }]);
    expect(parts[0].text).toBe("plain");
    expect(parts[0].status).toBe("created");
  });
});

describe("creation visit isolation", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    sessionApi.resetForTests();
  });
  it("does not join a prior blank visit or activate a late response", async () => {
    sessionApi.resetForTests();
    const old = deferred<ChatSpec>();
    const fresh = deferred<ChatSpec>();
    const create = vi
      .spyOn(api, "createChat")
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(fresh.promise);
    const selected = vi.fn();
    sessionApi.onSessionCreated = selected;
    const stale = sessionApi.createSession({ name: "old draft" });
    const rejected = expect(stale).rejects.toMatchObject({
      name: "AbortError",
    });
    sessionApi.invalidateSessionCreation();
    const current = sessionApi.createSession({ name: "fresh draft" });
    old.resolve(createdChat("old-chat"));
    await rejected;
    const joined = sessionApi.createSession({ name: "fresh retry" });
    fresh.resolve(createdChat("fresh-chat"));
    expect(await joined).toBe(await current);
    expect(create).toHaveBeenCalledTimes(2);
    expect(selected).not.toHaveBeenCalled();
    sessionApi.activateCreatedSession("old-chat");
    expect(selected).not.toHaveBeenCalled();
    sessionApi.activateCreatedSession("fresh-chat");
    sessionApi.activateCreatedSession("fresh-chat");
    expect(selected).toHaveBeenCalledExactlyOnceWith("fresh-chat");
  });
});

describe("visible session usage ownership", () => {
  beforeEach(() => {
    sessionApi.resetForTests();
    sessionApi.setActiveAgent("A");
  });
  afterEach(() => {
    vi.restoreAllMocks();
    sessionApi.resetForTests();
  });
  it("does not project a late history response over the selected session", async () => {
    const { useTurnUsageStore } = await import("../turnUsageStore");
    const history = (tokens: number): ChatHistory =>
      ({
        messages: [
          {
            role: "assistant",
            content: [{ type: "text", text: "done" }],
            metadata: {
              qwenpaw_turn_usage: {
                usage: { total_tokens: tokens },
                context_usage: {
                  estimated_tokens: tokens,
                  max_input_length: 100000,
                  context_usage_ratio: tokens / 1000,
                },
              },
            },
          },
        ],
        status: "idle",
      }) as ChatHistory;
    const slow = deferred<ChatHistory>();
    vi.spyOn(api, "getChat").mockImplementation((id) =>
      id === "one" ? slow.promise : Promise.resolve(history(200)),
    );
    sessionApi.setVisibleSession("one");
    const first = sessionApi.getSession("one");
    sessionApi.setVisibleSession("two");
    await sessionApi.getSession("two");
    expect(
      useTurnUsageStore.getState().snapshot?.context_usage?.estimated_tokens,
    ).toBe(200);
    slow.resolve(history(100));
    await first;
    expect(
      useTurnUsageStore.getState().snapshot?.context_usage?.estimated_tokens,
    ).toBe(200);
    sessionApi.setVisibleSession(null);
    expect(useTurnUsageStore.getState().snapshot).toBeNull();
  });
  it("restores usage when selection hits the preload result cache", async () => {
    const { useTurnUsageStore } = await import("../turnUsageStore");
    vi.spyOn(api, "getChat").mockResolvedValue({
      messages: [
        {
          role: "assistant",
          content: [{ type: "text", text: "done" }],
          metadata: {
            qwenpaw_turn_usage: {
              context_usage: {
                estimated_tokens: 500,
                max_input_length: 100000,
                context_usage_ratio: 0.5,
              },
            },
          },
        },
      ],
      status: "idle",
    } as ChatHistory);
    sessionApi.setVisibleSession(null);
    await sessionApi.preloadSession("cached");
    expect(useTurnUsageStore.getState().snapshot).toBeNull();
    sessionApi.setVisibleSession("cached");
    await sessionApi.getSession("cached");
    expect(
      useTurnUsageStore.getState().snapshot?.context_usage?.estimated_tokens,
    ).toBe(500);
  });
});
