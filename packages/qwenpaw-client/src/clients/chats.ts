import type {
  ChatForkRequest,
  ChatHistory,
  ChatSpec,
  ConversationRuntimeProjection,
} from "../contracts/chats.js";
import { QwenPawHttpError } from "../transport.js";

export interface ChatClientTransport {
  request<T>(path: string, init?: RequestInit): Promise<T>;
  openStream(path: string, init?: RequestInit): Promise<Response>;
}

export interface ChatRequestOptions {
  agentId?: string;
  signal?: AbortSignal;
}

export interface ListChatsOptions extends ChatRequestOptions {
  userId?: string;
  channel?: string;
  archived?: boolean;
  includeAppOwned?: boolean;
}

export interface FollowRuntimeOptions extends ChatRequestOptions {
  afterCursor?: string;
}

interface ServerSentEvent {
  id?: string;
  event?: string;
  data: string;
}

function requestInit(
  options: ChatRequestOptions,
  init: RequestInit = {},
  includeSignal = false,
): RequestInit | undefined {
  const result: RequestInit = { ...init };
  if (options.agentId) {
    if (init.headers) {
      const headers = new Headers(init.headers);
      headers.set("X-Agent-Id", options.agentId);
      result.headers = headers;
    } else {
      result.headers = { "X-Agent-Id": options.agentId };
    }
  }
  if (includeSignal || options.signal) result.signal = options.signal;
  return Object.keys(result).length > 0 ? result : undefined;
}

function parseEvent(block: string): ServerSentEvent | undefined {
  const data: string[] = [];
  let id: string | undefined;
  let event: string | undefined;
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const raw = separator < 0 ? "" : line.slice(separator + 1);
    const value = raw.startsWith(" ") ? raw.slice(1) : raw;
    if (field === "id") id = value;
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }
  if (!id && !event && data.length === 0) return undefined;
  return { id, event, data: data.join("\n") };
}

function eventBlocks(buffer: string): [ServerSentEvent[], string] {
  const events: ServerSentEvent[] = [];
  let boundary = buffer.indexOf("\n\n");
  while (boundary >= 0) {
    const event = parseEvent(buffer.slice(0, boundary));
    if (event) events.push(event);
    buffer = buffer.slice(boundary + 2);
    boundary = buffer.indexOf("\n\n");
  }
  return [events, buffer];
}

async function responseError(response: Response): Promise<QwenPawHttpError> {
  const body = (await response.text().catch(() => "")).slice(0, 4096);
  return new QwenPawHttpError(response.status, body);
}

export function createChatClient(transport: ChatClientTransport) {
  const encoded = encodeURIComponent;
  return {
    list(options: ListChatsOptions = {}) {
      const query = new URLSearchParams();
      if (options.userId) query.set("user_id", options.userId);
      if (options.channel) query.set("channel", options.channel);
      if (options.archived !== undefined) {
        query.set("archived", String(options.archived));
      }
      if (options.includeAppOwned !== undefined) {
        query.set("include_app_owned", String(options.includeAppOwned));
      }
      const suffix = query.size > 0 ? `?${query.toString()}` : "";
      const init = requestInit(options);
      return init
        ? transport.request<ChatSpec[]>(`/chats${suffix}`, init)
        : transport.request<ChatSpec[]>(`/chats${suffix}`);
    },
    create(body: Partial<ChatSpec>, options: ChatRequestOptions = {}) {
      return transport.request<ChatSpec>(
        "/chats",
        requestInit(options, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      );
    },
    history(
      chatId: string,
      options: ChatRequestOptions & { includeAppOwned?: boolean } = {},
    ) {
      const query =
        options.includeAppOwned === undefined
          ? ""
          : `?include_app_owned=${String(options.includeAppOwned)}`;
      return transport.request<ChatHistory>(
        `/chats/${encoded(chatId)}${query}`,
        requestInit(options, {}, true),
      );
    },
    fork(
      chatId: string,
      body: ChatForkRequest,
      options: ChatRequestOptions = {},
    ) {
      return transport.request<ChatSpec>(
        `/chats/${encoded(chatId)}/fork`,
        requestInit(options, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      );
    },
    runtime(chatId: string, options: ChatRequestOptions = {}) {
      return transport.request<ConversationRuntimeProjection>(
        `/chats/${encoded(chatId)}/runtime`,
        requestInit(options, {}, true),
      );
    },
    async *followRuntime(
      chatId: string,
      options: FollowRuntimeOptions = {},
    ): AsyncGenerator<ConversationRuntimeProjection, string | undefined> {
      const headers = new Headers({ Accept: "text/event-stream" });
      if (options.agentId) headers.set("X-Agent-Id", options.agentId);
      if (options.afterCursor) {
        headers.set("Last-Event-ID", options.afterCursor);
      }
      const response = await transport.openStream(
        `/chats/${encoded(chatId)}/runtime/stream`,
        requestInit(options, { headers }),
      );
      if (!response.ok) throw await responseError(response);
      if (!response.body) {
        throw new Error("QwenPaw runtime stream has no response body");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let cursor = options.afterCursor;
      try {
        while (true) {
          const result = await reader.read();
          buffer += decoder.decode(result.value, { stream: !result.done });
          buffer = buffer.split("\r\n").join("\n");
          const [events, remainder] = eventBlocks(buffer);
          buffer = remainder;
          for (const event of events) {
            if (event.id) cursor = event.id;
            if (event.event !== "snapshot" || !event.data) continue;
            const projection = JSON.parse(
              event.data,
            ) as ConversationRuntimeProjection;
            cursor = projection.cursor || cursor;
            yield projection;
          }
          if (result.done) {
            const event = parseEvent(buffer);
            if (event?.id) cursor = event.id;
            if (event?.event === "snapshot" && event.data) {
              const projection = JSON.parse(
                event.data,
              ) as ConversationRuntimeProjection;
              cursor = projection.cursor || cursor;
              yield projection;
            }
            return cursor;
          }
        }
      } finally {
        reader.releaseLock();
      }
    },
  };
}

export type ChatClient = ReturnType<typeof createChatClient>;
