import type {
  ConversationExecutionChain,
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

export interface FollowSubmissionOptions extends ChatRequestOptions {
  reconnectDelayMs?: number;
  maxReconnectAttempts?: number;
}

const SETTLED_EXECUTION_STATES = new Set([
  "inactive",
  "failed",
  "interrupted",
  "cancelled",
  "achieved",
  "partial",
  "not_achieved",
  "abandoned",
]);

export class ChatExecutionError extends Error {
  readonly code: string;
  readonly chatId: string;
  readonly submissionId: string;
  readonly cursor?: string;

  constructor(
    code: string,
    message: string,
    context: {
      chatId: string;
      submissionId: string;
      cursor?: string;
      cause?: unknown;
    },
  ) {
    super(message);
    this.name = "ChatExecutionError";
    this.code = code;
    this.chatId = context.chatId;
    this.submissionId = context.submissionId;
    this.cursor = context.cursor;
    if (context.cause !== undefined) {
      (this as Error & { cause?: unknown }).cause = context.cause;
    }
  }
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

function nonNegativeInteger(name: string, value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${name} must be a non-negative safe integer`);
  }
  return value;
}

function wait(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const timer = setTimeout(finish, milliseconds);
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(signal?.reason ?? new DOMException("Aborted", "AbortError"));
    };
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
  });
}

function submissionChain(
  projection: ConversationRuntimeProjection,
  submissionId: string,
): ConversationExecutionChain | undefined {
  return projection.execution_chains?.find((chain) =>
    chain.submission_ids.includes(submissionId),
  );
}

export function isExecutionSettled(chain: ConversationExecutionChain): boolean {
  return SETTLED_EXECUTION_STATES.has(chain.state);
}

export function createChatClient(transport: ChatClientTransport) {
  const encoded = encodeURIComponent;
  const runtime = (
    chatId: string,
    options: ChatRequestOptions = {},
  ): Promise<ConversationRuntimeProjection> =>
    transport.request<ConversationRuntimeProjection>(
      `/chats/${encoded(chatId)}/runtime`,
      requestInit(options, {}, true),
    );
  const followRuntime = async function* (
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
  };
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
    runtime,
    followRuntime,
    async *followSubmission(
      chatId: string,
      submissionId: string,
      options: FollowSubmissionOptions = {},
    ): AsyncGenerator<ConversationExecutionChain, ConversationExecutionChain> {
      if (!submissionId.trim()) {
        throw new TypeError("submissionId must not be empty");
      }
      const maxReconnectAttempts = nonNegativeInteger(
        "maxReconnectAttempts",
        options.maxReconnectAttempts ?? 3,
      );
      const reconnectDelayMs = nonNegativeInteger(
        "reconnectDelayMs",
        options.reconnectDelayMs ?? 250,
      );
      let cursor: string | undefined;
      let lastValue = "";
      let reconnectAttempts = 0;

      const inspect = (
        projection: ConversationRuntimeProjection,
      ): { chain?: ConversationExecutionChain; changed: boolean } => {
        cursor = projection.cursor || cursor;
        const chain = submissionChain(projection, submissionId);
        if (!chain && projection.execution_window_truncated) {
          throw new ChatExecutionError(
            "SUBMISSION_OUTSIDE_EXECUTION_WINDOW",
            "Submission is outside the Host execution projection window",
            { chatId, submissionId, cursor },
          );
        }
        if (!chain) return { changed: false };
        const value = JSON.stringify(chain);
        const changed = value !== lastValue;
        lastValue = value;
        return { chain, changed };
      };

      let current = await runtime(chatId, options);
      let observed = inspect(current);
      if (observed.chain && observed.changed) yield observed.chain;
      if (observed.chain && isExecutionSettled(observed.chain)) {
        return observed.chain;
      }

      while (true) {
        try {
          const stream = followRuntime(chatId, {
            agentId: options.agentId,
            signal: options.signal,
            afterCursor: cursor,
          });
          while (true) {
            const result = await stream.next();
            if (result.done) {
              cursor = result.value || cursor;
              break;
            }
            reconnectAttempts = 0;
            observed = inspect(result.value);
            if (observed.chain && observed.changed) yield observed.chain;
            if (observed.chain && isExecutionSettled(observed.chain)) {
              return observed.chain;
            }
          }
        } catch (error) {
          if (error instanceof ChatExecutionError) throw error;
          if (options.signal?.aborted) throw error;
        }

        try {
          current = await runtime(chatId, options);
          observed = inspect(current);
          if (observed.chain && observed.changed) yield observed.chain;
          if (observed.chain && isExecutionSettled(observed.chain)) {
            return observed.chain;
          }
        } catch (error) {
          if (error instanceof ChatExecutionError) throw error;
          if (options.signal?.aborted) throw error;
          reconnectAttempts += 1;
          if (reconnectAttempts > maxReconnectAttempts) {
            throw new ChatExecutionError(
              "RUNTIME_RECONNECT_EXHAUSTED",
              "Unable to reconcile the submission with the QwenPaw Host",
              { chatId, submissionId, cursor, cause: error },
            );
          }
          await wait(reconnectDelayMs, options.signal);
          continue;
        }

        reconnectAttempts += 1;
        if (reconnectAttempts > maxReconnectAttempts) {
          throw new ChatExecutionError(
            "RUNTIME_STREAM_ENDED",
            "Runtime stream ended without a settled execution state",
            { chatId, submissionId, cursor },
          );
        }
        await wait(reconnectDelayMs, options.signal);
      }
    },
  };
}

export type ChatClient = ReturnType<typeof createChatClient>;
