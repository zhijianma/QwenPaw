import { buildAuthHeaders } from "./authHeaders";
import { getApiUrl } from "./config";
import type { ConversationRuntimeProjection } from "./types";

interface RuntimeProjectionStreamOptions {
  chatId: string;
  agentId?: string;
  afterCursor?: string;
  signal?: AbortSignal;
  onSnapshot: (projection: ConversationRuntimeProjection) => void;
}

interface ServerSentEvent {
  id?: string;
  event?: string;
  data: string;
}

function parseEvent(block: string): ServerSentEvent | null {
  const data: string[] = [];
  let id: string | undefined;
  let event: string | undefined;

  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const rawValue = separator < 0 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "id") id = value;
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }

  if (!id && !event && data.length === 0) return null;
  return { id, event, data: data.join("\n") };
}

function consumeEventBlocks(
  buffer: string,
  onEvent: (event: ServerSentEvent) => void,
): string {
  let boundary = buffer.indexOf("\n\n");
  while (boundary >= 0) {
    const block = buffer.slice(0, boundary);
    buffer = buffer.slice(boundary + 2);
    const event = parseEvent(block);
    if (event) onEvent(event);
    boundary = buffer.indexOf("\n\n");
  }
  return buffer;
}

export async function streamRuntimeProjection(
  options: RuntimeProjectionStreamOptions,
): Promise<string | undefined> {
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    ...buildAuthHeaders(),
  };
  if (options.agentId) headers["X-Agent-Id"] = options.agentId;
  if (options.afterCursor) headers["Last-Event-ID"] = options.afterCursor;

  const response = await fetch(
    getApiUrl(`/chats/${encodeURIComponent(options.chatId)}/runtime/stream`),
    { headers, signal: options.signal },
  );
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(
      detail
        ? `Runtime stream failed: ${response.status} - ${detail}`
        : `Runtime stream failed: ${response.status}`,
    );
  }
  if (!response.body) throw new Error("Runtime stream has no response body");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let cursor = options.afterCursor;

  const onEvent = (event: ServerSentEvent) => {
    if (event.id) cursor = event.id;
    if (event.event !== "snapshot" || !event.data) return;
    const projection = JSON.parse(event.data) as ConversationRuntimeProjection;
    cursor = projection.cursor || cursor;
    options.onSnapshot(projection);
  };

  try {
    while (true) {
      const result = await reader.read();
      buffer += decoder.decode(result.value, { stream: !result.done });
      buffer = buffer.replaceAll("\r\n", "\n");
      buffer = consumeEventBlocks(buffer, onEvent);
      if (result.done) {
        const finalEvent = parseEvent(buffer);
        if (finalEvent) onEvent(finalEvent);
        return cursor;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export type { RuntimeProjectionStreamOptions };
