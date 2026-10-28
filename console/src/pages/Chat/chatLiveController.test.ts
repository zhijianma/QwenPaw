import { describe, expect, it, vi } from "vitest";

import {
  ChatLiveController,
  type ChatLiveCursor,
  type ChatLiveEvent,
  type ChatLiveSync,
} from "./chatLiveController";

const path = {
  conversation_id: "chat-1",
  run_id: "run-1",
  turn_id: "turn-1",
};

function cursor(sequence: number): ChatLiveCursor {
  return { run_epoch: "epoch-1", event_sequence: sequence };
}

function sse(event: string, payload: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
}

function response(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200 },
  );
}

function sync(sequence: number, terminal = false): ChatLiveSync {
  return {
    kind: "sync",
    path,
    cursor: cursor(sequence),
    view: { run_state: terminal ? "completed" : "running", blocks: [] },
    resync_reason: null,
    terminal,
  };
}

function event(sequence: number, kind = "block_append"): ChatLiveEvent {
  return {
    path,
    cursor: cursor(sequence),
    kind,
    payload: kind === "block_append" ? { text: String(sequence) } : {},
    entity_id: "block-1",
    entity_version: 1,
    expected_offset: sequence - 1,
  };
}

describe("ChatLiveController", () => {
  it("reconnects from the applied cursor and finishes on terminal", async () => {
    const connections: Array<ChatLiveCursor | null> = [];
    const responses = [
      response(sse("sync", sync(0)), sse("block_append", event(1))),
      response(sse("sync", sync(2)), sse("terminal", event(3, "terminal"))),
    ];
    const events: ChatLiveEvent[] = [];
    const onTerminal = vi.fn();
    const controller = new ChatLiveController(
      "chat-1",
      async (value) => {
        connections.push(value);
        const next = responses.shift();
        if (!next) throw new Error("unexpected reconnect");
        return next;
      },
      {
        onSync: vi.fn(),
        onEvents: (batch) => {
          events.push(...batch);
        },
        onResync: vi.fn(),
        onTerminal,
      },
    );

    await controller.start();

    expect(connections).toEqual([null, cursor(1)]);
    expect(events.map((item) => item.cursor.event_sequence)).toEqual([1, 3]);
    expect(onTerminal).toHaveBeenCalledOnce();
  });

  it("drops its cursor and requests calibration on an event gap", async () => {
    const connections: Array<ChatLiveCursor | null> = [];
    const responses = [
      response(sse("sync", sync(0)), sse("block_append", event(2))),
      response(sse("sync", sync(2, true))),
    ];
    const onResync = vi.fn();
    const controller = new ChatLiveController(
      "chat-1",
      async (value) => {
        connections.push(value);
        const next = responses.shift();
        if (!next) throw new Error("unexpected reconnect");
        return next;
      },
      {
        onSync: vi.fn(),
        onEvents: vi.fn(),
        onResync,
        onTerminal: vi.fn(),
      },
    );

    await controller.start();

    expect(connections).toEqual([null, null]);
    expect(onResync).toHaveBeenCalledWith("event_gap");
  });

  it("parses frames split across transport chunks", async () => {
    const terminal = sse("sync", sync(0, true));
    const midpoint = Math.floor(terminal.length / 2);
    const onSync = vi.fn();
    const controller = new ChatLiveController(
      "chat-1",
      async () =>
        response(terminal.slice(0, midpoint), terminal.slice(midpoint)),
      {
        onSync,
        onEvents: vi.fn(),
        onResync: vi.fn(),
        onTerminal: vi.fn(),
      },
    );

    await controller.start();

    expect(onSync).toHaveBeenCalledWith(sync(0, true));
  });
});
