export interface ChatLivePath {
  conversation_id: string;
  run_id: string;
  turn_id: string;
}

export interface ChatLiveCursor {
  run_epoch: string;
  event_sequence: number;
}

export interface ChatLivePendingSegment {
  start_byte: number;
  entity_version: number;
  text: string;
}

export interface ChatLiveBlockView {
  message_id: string;
  block_id: string;
  index: number;
  kind: string;
  state: string;
  durable_bytes: number;
  total_bytes: number;
  entity_version: number;
  snapshot?: {
    start_byte: number;
    text: string;
  };
  pending: ChatLivePendingSegment[];
}

export interface ChatLiveView {
  run_state: string;
  blocks: ChatLiveBlockView[];
  messages?: ChatLiveCompletedMessage[];
}

export interface ChatLiveCompletedMessage {
  message_id: string;
  deferred: boolean;
  message: Record<string, unknown> | null;
  encoded_bytes: number;
}

export interface ChatLiveSync {
  kind: "sync";
  path: ChatLivePath | null;
  cursor: ChatLiveCursor | null;
  view: ChatLiveView | null;
  resync_reason: string | null;
  terminal: boolean;
}

export interface ChatLiveEvent {
  path: ChatLivePath;
  cursor: ChatLiveCursor;
  kind: string;
  payload: Record<string, unknown>;
  entity_id: string | null;
  entity_version: number | null;
  expected_offset: number | null;
}

export interface ChatLiveCallbacks {
  onSync: (sync: ChatLiveSync) => void | Promise<void>;
  onEvents: (events: ChatLiveEvent[]) => void | Promise<void>;
  onResync: (reason: string) => void | Promise<void>;
  onTerminal: (
    event: ChatLiveEvent | null,
    sync?: ChatLiveSync,
  ) => void | Promise<void>;
  onError?: (error: unknown) => void;
}

export type ChatLiveConnector = (
  cursor: ChatLiveCursor | null,
  signal: AbortSignal,
) => Promise<Response>;

interface SseFrame {
  event: string;
  data: unknown;
}

const encoder = new TextEncoder();

function parseFrame(frame: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const value =
      separator < 0 ? "" : line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }
  if (data.length === 0) return null;
  return { event, data: JSON.parse(data.join("\n")) };
}

export async function* readChatLiveSse(
  response: Response,
  signal: AbortSignal,
): AsyncGenerator<SseFrame> {
  if (!response.ok) {
    throw new Error(`Chat live request failed with ${response.status}`);
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("Chat live response has no body");
  const cancelReader = () => void reader.cancel().catch(() => undefined);
  signal.addEventListener("abort", cancelReader, { once: true });
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = parseFrame(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        if (frame) yield frame;
        boundary = buffer.indexOf("\n\n");
      }
      if (done) return;
    }
  } finally {
    signal.removeEventListener("abort", cancelReader);
    await reader.cancel().catch(() => undefined);
  }
}

class LiveEventBatcher {
  private events: ChatLiveEvent[] = [];
  private bytes = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private tail = Promise.resolve();
  private readonly deliver: ChatLiveCallbacks["onEvents"];
  private readonly maxDelayMs: number;
  private readonly maxBytes: number;

  constructor(
    deliver: ChatLiveCallbacks["onEvents"],
    maxDelayMs = 50,
    maxBytes = 32 * 1024,
  ) {
    this.deliver = deliver;
    this.maxDelayMs = maxDelayMs;
    this.maxBytes = maxBytes;
  }

  async enqueue(event: ChatLiveEvent): Promise<void> {
    if (event.kind !== "block_append") {
      await this.flush();
      await this.dispatch([event]);
      return;
    }
    this.events.push(event);
    this.bytes += encoder.encode(JSON.stringify(event)).byteLength;
    if (this.bytes >= this.maxBytes) {
      await this.flush();
      return;
    }
    if (this.timer === null) {
      this.timer = setTimeout(() => void this.flush(), this.maxDelayMs);
    }
  }

  async flush(): Promise<void> {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    if (this.events.length === 0) {
      await this.tail;
      return;
    }
    const events = this.events;
    this.events = [];
    this.bytes = 0;
    await this.dispatch(events);
  }

  private async dispatch(events: ChatLiveEvent[]): Promise<void> {
    this.tail = this.tail.then(() => this.deliver(events));
    await this.tail;
  }
}

function samePath(left: ChatLivePath, right: ChatLivePath): boolean {
  return (
    left.conversation_id === right.conversation_id &&
    left.run_id === right.run_id &&
    left.turn_id === right.turn_id
  );
}

function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

export class ChatLiveController {
  private controller: AbortController | null = null;
  private readonly conversationId: string;
  private readonly connect: ChatLiveConnector;
  private readonly callbacks: ChatLiveCallbacks;

  constructor(
    conversationId: string,
    connect: ChatLiveConnector,
    callbacks: ChatLiveCallbacks,
  ) {
    this.conversationId = conversationId;
    this.connect = connect;
    this.callbacks = callbacks;
  }

  start(): Promise<void> {
    this.stop();
    this.controller = new AbortController();
    return this.run(this.controller.signal);
  }

  stop(): void {
    this.controller?.abort();
    this.controller = null;
  }

  private async run(signal: AbortSignal): Promise<void> {
    let cursor: ChatLiveCursor | null = null;
    let path: ChatLivePath | null = null;
    let retryMs = 250;
    const batcher = new LiveEventBatcher(this.callbacks.onEvents);
    while (!signal.aborted) {
      try {
        const response = await this.connect(cursor, signal);
        let reconnectWithoutCursor = false;
        for await (const frame of readChatLiveSse(response, signal)) {
          if (frame.event === "sync") {
            const sync = frame.data as ChatLiveSync;
            if (
              sync.path &&
              sync.path.conversation_id !== this.conversationId
            ) {
              throw new Error("Chat live sync path does not match owner");
            }
            await batcher.flush();
            await this.callbacks.onSync(sync);
            if (sync.resync_reason) {
              await this.callbacks.onResync(sync.resync_reason);
            }
            cursor = sync.cursor;
            path = sync.path;
            retryMs = 250;
            if (sync.terminal) {
              await this.callbacks.onTerminal(null, sync);
              return;
            }
            continue;
          }
          const event = frame.data as ChatLiveEvent;
          if (!path || !samePath(path, event.path)) {
            throw new Error("Chat live event path does not match sync");
          }
          if (
            cursor &&
            event.cursor.run_epoch === cursor.run_epoch &&
            event.cursor.event_sequence <= cursor.event_sequence
          ) {
            continue;
          }
          if (
            !cursor ||
            event.cursor.run_epoch !== cursor.run_epoch ||
            event.cursor.event_sequence !== cursor.event_sequence + 1
          ) {
            await batcher.flush();
            await this.callbacks.onResync("event_gap");
            cursor = null;
            path = null;
            reconnectWithoutCursor = true;
            break;
          }
          cursor = event.cursor;
          await batcher.enqueue(event);
          if (event.kind === "resync_required") {
            await batcher.flush();
            await this.callbacks.onResync(
              String(event.payload.reason ?? "resync_required"),
            );
          }
          if (event.kind === "terminal") {
            await batcher.flush();
            await this.callbacks.onTerminal(event);
            return;
          }
        }
        await batcher.flush();
        if (reconnectWithoutCursor) continue;
      } catch (error) {
        await batcher.flush();
        if (signal.aborted) return;
        this.callbacks.onError?.(error);
      }
      await abortableDelay(retryMs, signal).catch(() => undefined);
      retryMs = Math.min(retryMs * 2, 4_000);
    }
  }
}
