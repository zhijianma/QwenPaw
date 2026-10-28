import { beforeEach, describe, expect, it } from "vitest";
import type { ChatHistoryPage, ChatTurnItem } from "../../api/types/chat";
import {
  ACTIVE_BYTE_LIMIT,
  BACKGROUND_PAGE_LIMIT,
  selectLoadedBackendMessages,
  useChatHistoryStore,
} from "./chatHistoryStore";
import type { ChatLiveEvent, ChatLiveSync } from "./chatLiveController";

function turnPage(
  id: number,
  options?: {
    conversation?: string;
    bytes?: number;
    loaded?: boolean;
    state?: string;
    version?: string;
  },
): ChatHistoryPage<ChatTurnItem> {
  const loaded = options?.loaded ?? true;
  return {
    items: [
      {
        turn_id: `turn-${id}`,
        parent_turn_id: id > 0 ? `turn-${id - 1}` : null,
        state: options?.state ?? "completed",
        lineage_depth: id,
        usage: null,
        usage_complete: true,
        error_digest: null,
        created_at_us: id,
        updated_at_us: id,
        terminal_at_us: id,
        child_cursor: null,
        children_complete: loaded,
        messages: [
          {
            message_id: `message-${id}`,
            turn_id: `turn-${id}`,
            role: "user",
            ordinal: 0,
            state: "completed",
            metadata: { type: "message", status: "completed" },
            metadata_complete: true,
            created_at_us: id,
            updated_at_us: id,
            child_cursor: null,
            children_complete: loaded,
            blocks: [
              {
                block_id: `block-${id}`,
                message_id: `message-${id}`,
                kind: "text",
                ordinal: 0,
                state: "completed",
                total_bytes: loaded ? 16 : 1024,
                content_sha256: "hash",
                entity_version: 1,
                metadata: {},
                metadata_complete: true,
                created_at_us: id,
                updated_at_us: id,
                content: loaded ? { type: "text", text: `text-${id}` } : null,
                content_loaded: loaded,
                content_cursor: loaded ? null : `fragment-${id}`,
                preview: loaded ? undefined : `preview-${id}`,
              },
            ],
          },
        ],
      },
    ],
    older_cursor: id > 0 ? `older-${id}` : null,
    newer_cursor: `newer-${id}`,
    view: {
      conversation_id: options?.conversation ?? "chat",
      head_turn_id: "head",
      view_version: options?.version ?? "version",
    },
    bytes: options?.bytes ?? 1024,
    truncated_entity: !loaded,
  };
}

describe("chatHistoryStore", () => {
  beforeEach(() => {
    useChatHistoryStore.setState({ activeKey: null, paths: {} });
  });

  it("normalizes a page and reconstructs only the retained SDK window", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");

    expect(
      useChatHistoryStore.getState().mergeTurnPage(owner, turnPage(1)),
    ).toBe(true);

    const path = useChatHistoryStore.getState().paths[owner.key];
    expect(path.turnOrder).toEqual(["turn-1"]);
    expect(path.turnsById["turn-1"].messageIds).toEqual(["message-1"]);
    expect(path.messagesById["message-1"].blockIds).toEqual(["block-1"]);
    expect(selectLoadedBackendMessages(owner)).toEqual([
      expect.objectContaining({
        id: "message-1",
        role: "user",
        chat_turn_id: "turn-1",
        content: [{ type: "text", text: "text-1" }],
      }),
    ]);
  });

  it.each(["canceled", "interrupted"])(
    "restores %s turn state on the user envelope",
    (state) => {
      const owner = useChatHistoryStore
        .getState()
        .beginPath("agent", "chat");
      const page = turnPage(1, { state });
      page.items[0].messages[0].metadata = {
        type: "message",
        metadata: { qwenpaw_client_message_id: "client-1" },
      };

      expect(useChatHistoryStore.getState().mergeTurnPage(owner, page)).toBe(
        true,
      );

      expect(selectLoadedBackendMessages(owner)[0].metadata).toEqual({
        qwenpaw_client_message_id: "client-1",
        qwenpaw_turn_state: { status: "canceled" },
      });
    },
  );

  it("restores failed turn state on the user envelope", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");

    expect(
      useChatHistoryStore
        .getState()
        .mergeTurnPage(owner, turnPage(1, { state: "failed" })),
    ).toBe(true);

    expect(selectLoadedBackendMessages(owner)[0].metadata).toEqual({
      qwenpaw_turn_state: { status: "failed" },
    });
  });

  it("rejects stale owner epochs and changed history views", () => {
    const first = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    const current = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");

    expect(
      useChatHistoryStore.getState().mergeTurnPage(first, turnPage(1)),
    ).toBe(false);
    expect(
      useChatHistoryStore.getState().mergeTurnPage(current, turnPage(1)),
    ).toBe(true);
    expect(
      useChatHistoryStore
        .getState()
        .mergeTurnPage(current, turnPage(2, { version: "restored" }), "newer"),
    ).toBe(false);
  });

  it("keeps rapid Chat windows isolated by owner key", () => {
    const main = useChatHistoryStore
      .getState()
      .beginPath("agent", "main-chat");
    expect(
      useChatHistoryStore
        .getState()
        .mergeTurnPage(main, turnPage(1, { conversation: "main-chat" })),
    ).toBe(true);
    const child = useChatHistoryStore
      .getState()
      .beginPath("agent", "copied-chat");
    expect(
      useChatHistoryStore
        .getState()
        .mergeTurnPage(
          child,
          turnPage(2, { conversation: "copied-chat" }),
        ),
    ).toBe(true);

    expect(selectLoadedBackendMessages(main)[0].id).toBe("message-1");
    expect(selectLoadedBackendMessages(child)[0].id).toBe("message-2");
  });

  it("enforces page and byte budgets without retaining evicted entities", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    for (let id = 0; id < 7; id += 1) {
      useChatHistoryStore
        .getState()
        .mergeTurnPage(
          owner,
          turnPage(id, { bytes: ACTIVE_BYTE_LIMIT / 4 }),
          id === 0 ? "initial" : "newer",
        );
    }

    const active = useChatHistoryStore.getState().paths[owner.key];
    expect(active.pages.length).toBeLessThanOrEqual(4);
    expect(active.decodedBytes).toBeLessThanOrEqual(ACTIVE_BYTE_LIMIT);
    expect(Object.keys(active.turnsById)).toEqual(active.turnOrder);

    useChatHistoryStore.getState().beginPath("agent", "other");
    const background = useChatHistoryStore.getState().paths[owner.key];
    expect(background.pages.length).toBeLessThanOrEqual(BACKGROUND_PAGE_LIMIT);
    expect(Object.keys(background.turnsById)).toEqual(background.turnOrder);
  });

  it("uses a bounded preview instead of decoding an unloaded block", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    useChatHistoryStore
      .getState()
      .mergeTurnPage(owner, turnPage(1, { loaded: false }));

    const [message] = selectLoadedBackendMessages(owner);
    expect(message.content).toEqual([
      expect.objectContaining({
        type: "text",
        text: "preview-1",
        metadata: expect.objectContaining({
          qwenpaw_lazy_block_id: "block-1",
        }),
      }),
    ]);
    const [sameMessage] = selectLoadedBackendMessages(owner);
    expect((sameMessage.content as unknown[])[0]).toBe(
      (message.content as unknown[])[0],
    );
  });

  it("normalizes scalar assistant blocks and hides Scroll headlines", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    const page = turnPage(1);
    const message = page.items[0].messages[0];
    message.role = "assistant";
    message.blocks[0].content = "reply\n\n⟦ hidden headline ⟧";
    useChatHistoryStore.getState().mergeTurnPage(owner, page);

    expect(selectLoadedBackendMessages(owner)[0].content).toEqual([
      {
        type: "text",
        text: "reply",
      },
    ]);
  });

  it("preserves headline-like text written by a user", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    const page = turnPage(1);
    page.items[0].messages[0].blocks[0].content =
      "compare\n⟦ user-provided text ⟧";
    useChatHistoryStore.getState().mergeTurnPage(owner, page);

    expect(selectLoadedBackendMessages(owner)[0].content).toEqual([
      {
        type: "text",
        text: "compare\n⟦ user-provided text ⟧",
      },
    ]);
  });

  it("merges message and block continuations into normalized parents", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    const initial = turnPage(1);
    initial.items[0].child_cursor = "next-message";
    initial.items[0].children_complete = false;
    initial.items[0].messages[0].child_cursor = "next-block";
    initial.items[0].messages[0].children_complete = false;
    useChatHistoryStore.getState().mergeTurnPage(owner, initial);

    expect(
      useChatHistoryStore.getState().mergeBlockPage(owner, "message-1", {
        items: [
          {
            ...initial.items[0].messages[0].blocks[0],
            block_id: "block-continued",
            ordinal: 1,
            content: { type: "text", text: "continued" },
          },
        ],
        older_cursor: "previous-block",
        newer_cursor: null,
        view: initial.view,
        bytes: 512,
        truncated_entity: false,
      }),
    ).toBe(true);
    expect(
      useChatHistoryStore.getState().mergeMessagePage(owner, "turn-1", {
        items: [
          {
            ...initial.items[0].messages[0],
            message_id: "message-continued",
            ordinal: 1,
            blocks: [],
            child_cursor: null,
            children_complete: true,
          },
        ],
        older_cursor: "previous-message",
        newer_cursor: null,
        view: initial.view,
        bytes: 512,
        truncated_entity: false,
      }),
    ).toBe(true);

    const path = useChatHistoryStore.getState().paths[owner.key];
    expect(path.turnsById["turn-1"].messageIds).toEqual([
      "message-1",
      "message-continued",
    ]);
    expect(path.messagesById["message-1"].blockIds).toEqual([
      "block-1",
      "block-continued",
    ]);
    expect(path.decodedBytes).toBe(initial.bytes + 1024);
  });

  it("merges a live sync tail onto its durable byte boundary", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    const page = turnPage(1);
    page.items[0].messages[0].blocks[0].total_bytes = 6;
    useChatHistoryStore.getState().mergeTurnPage(owner, page);
    const sync: ChatLiveSync = {
      kind: "sync",
      path: {
        conversation_id: "chat",
        run_id: "run",
        turn_id: "turn-1",
      },
      cursor: { run_epoch: "epoch", event_sequence: 3 },
      view: {
        run_state: "running",
        blocks: [
          {
            message_id: "message-1",
            block_id: "block-1",
            index: 0,
            kind: "text",
            state: "partial",
            durable_bytes: 6,
            total_bytes: 11,
            entity_version: 2,
            pending: [{ start_byte: 6, entity_version: 2, text: " tail" }],
          },
        ],
      },
      resync_reason: null,
      terminal: false,
    };

    expect(useChatHistoryStore.getState().applyLiveSync(owner, sync)).toBe(
      true,
    );
    const [message] = selectLoadedBackendMessages(owner);
    expect(message.content).toEqual([{ type: "text", text: "text-1 tail" }]);
  });

  it("applies live offsets once and rejects a forward gap", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    const page = turnPage(1);
    page.items[0].messages[0].blocks[0].total_bytes = 6;
    useChatHistoryStore.getState().mergeTurnPage(owner, page);
    const sync: ChatLiveSync = {
      kind: "sync",
      path: {
        conversation_id: "chat",
        run_id: "run",
        turn_id: "turn-1",
      },
      cursor: { run_epoch: "epoch", event_sequence: 0 },
      view: { run_state: "running", blocks: [] },
      resync_reason: null,
      terminal: false,
    };
    useChatHistoryStore.getState().applyLiveSync(owner, sync);
    const append: ChatLiveEvent = {
      path: sync.path!,
      cursor: { run_epoch: "epoch", event_sequence: 1 },
      kind: "block_append",
      payload: { text: " tail", index: 0, message_id: "message-1" },
      entity_id: "block-1",
      entity_version: 2,
      expected_offset: 6,
    };

    expect(
      useChatHistoryStore.getState().applyLiveEvents(owner, [append, append]),
    ).toBe(true);
    expect(
      useChatHistoryStore.getState().applyLiveEvents(owner, [
        {
          ...append,
          cursor: { run_epoch: "epoch", event_sequence: 2 },
          expected_offset: 99,
        },
      ]),
    ).toBe(false);
    const [message] = selectLoadedBackendMessages(owner);
    expect(message.content).toEqual([{ type: "text", text: "text-1 tail" }]);
  });

  it("renders a zero-durable live message absent from the history page", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    useChatHistoryStore.getState().mergeTurnPage(owner, turnPage(1));

    expect(
      useChatHistoryStore.getState().applyLiveSync(owner, {
        kind: "sync",
        path: {
          conversation_id: "chat",
          run_id: "run",
          turn_id: "turn-1",
        },
        cursor: { run_epoch: "epoch", event_sequence: 1 },
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
      }),
    ).toBe(true);

    const messages = selectLoadedBackendMessages(owner);
    expect(messages[messages.length - 1]).toEqual(
      expect.objectContaining({
        id: "live-message",
        role: "assistant",
        content: [{ type: "text", text: "pending" }],
      }),
    );
  });

  it("restores completed active messages from a reconnect sync", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    useChatHistoryStore.getState().mergeTurnPage(owner, turnPage(1));

    expect(
      useChatHistoryStore.getState().applyLiveSync(owner, {
        kind: "sync",
        path: {
          conversation_id: "chat",
          run_id: "run",
          turn_id: "active-turn",
        },
        cursor: { run_epoch: "epoch", event_sequence: 3 },
        view: {
          run_state: "running",
          blocks: [],
          messages: [
            {
              message_id: "tool-message",
              deferred: false,
              encoded_bytes: 128,
              message: {
                id: "source-id",
                role: "tool",
                type: "plugin_call_output",
                status: "completed",
                content: [{ type: "text", text: "tool result" }],
              },
            },
          ],
        },
        resync_reason: null,
        terminal: false,
      }),
    ).toBe(true);

    const messages = selectLoadedBackendMessages(owner);
    expect(messages[messages.length - 1]).toEqual(
      expect.objectContaining({
        id: "tool-message",
        role: "tool",
        content: [{ type: "text", text: "tool result" }],
      }),
    );
  });

  it("keeps a reconnecting turn separate from a canceled predecessor", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    useChatHistoryStore
      .getState()
      .mergeTurnPage(owner, turnPage(1, { state: "canceled" }));

    expect(
      useChatHistoryStore.getState().applyLiveSync(owner, {
        kind: "sync",
        path: {
          conversation_id: "chat",
          run_id: "run",
          turn_id: "active-turn",
        },
        cursor: { run_epoch: "epoch", event_sequence: 3 },
        view: {
          run_state: "running",
          blocks: [],
          messages: [
            {
              message_id: "active-user",
              deferred: false,
              encoded_bytes: 64,
              message: {
                id: "source-user",
                role: "user",
                type: "message",
                status: "completed",
                content: [{ type: "text", text: "next prompt" }],
              },
            },
            {
              message_id: "active-assistant",
              deferred: false,
              encoded_bytes: 64,
              message: {
                id: "source-assistant",
                role: "assistant",
                type: "message",
                status: "completed",
                content: [{ type: "text", text: "next response" }],
              },
            },
          ],
        },
        resync_reason: null,
        terminal: false,
      }),
    ).toBe(true);

    const messages = selectLoadedBackendMessages(owner);
    expect(messages.map((message) => message.id)).toEqual([
      "message-1",
      "active-user",
      "active-assistant",
    ]);
    expect(messages[0].metadata).toEqual({
      qwenpaw_turn_state: { status: "canceled" },
    });
    expect(messages[1].metadata).toBeUndefined();
  });

  it("replaces a partial overlay when its completed message arrives", () => {
    const owner = useChatHistoryStore
      .getState()
      .beginPath("agent", "chat");
    useChatHistoryStore.getState().mergeTurnPage(owner, turnPage(1));
    const path = {
      conversation_id: "chat",
      run_id: "run",
      turn_id: "active-turn",
    };
    useChatHistoryStore.getState().applyLiveSync(owner, {
      kind: "sync",
      path,
      cursor: { run_epoch: "epoch", event_sequence: 1 },
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
    });

    expect(
      useChatHistoryStore.getState().applyLiveEvents(owner, [
        {
          path,
          cursor: { run_epoch: "epoch", event_sequence: 2 },
          kind: "entity_state",
          payload: {
            state: "completed",
            live_message: {
              message_id: "live-message",
              deferred: false,
              encoded_bytes: 96,
              message: {
                role: "assistant",
                type: "message",
                status: "completed",
                content: [{ type: "text", text: "pending" }],
              },
            },
          },
          entity_id: "live-message",
          entity_version: null,
          expected_offset: null,
        },
      ]),
    ).toBe(true);

    const messages = selectLoadedBackendMessages(owner);
    expect(messages.filter((message) => message.id === "live-message")).toEqual(
      [
        expect.objectContaining({
          status: "completed",
          content: [{ type: "text", text: "pending" }],
        }),
      ],
    );
  });
});
