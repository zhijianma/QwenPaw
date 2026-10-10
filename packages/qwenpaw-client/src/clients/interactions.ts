import type {
  ChatInteraction,
  ChatInteractionDecisionRequest,
  ChatInteractionRecord,
  ChatInteractionResolution,
} from "../contracts/interactions.js";

export interface InteractionClientTransport {
  request<T>(path: string, init?: RequestInit): Promise<T>;
}

export interface InteractionRequestOptions {
  agentId?: string;
  signal?: AbortSignal;
}

export interface InteractionHistoryOptions extends InteractionRequestOptions {
  limit?: number;
}

function isAbortSignal(value: unknown): value is AbortSignal {
  return (
    typeof value === "object" &&
    value !== null &&
    "aborted" in value &&
    "addEventListener" in value
  );
}

function normalizeOptions(
  value: InteractionRequestOptions | AbortSignal | undefined,
): InteractionRequestOptions {
  return isAbortSignal(value) ? { signal: value } : value ?? {};
}

function requestInit(
  value: InteractionRequestOptions | AbortSignal | undefined,
  init: RequestInit = {},
): RequestInit | undefined {
  const options = normalizeOptions(value);
  const result: RequestInit = { ...init };
  if (options.agentId) {
    const headers = new Headers(init.headers);
    headers.set("X-Agent-Id", options.agentId);
    result.headers = headers;
  }
  if (options.signal) result.signal = options.signal;
  return Object.keys(result).length > 0 ? result : undefined;
}

function historyLimit(value: number | undefined): number {
  const limit = value ?? 100;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 1000) {
    throw new TypeError("limit must be a safe integer between 1 and 1000");
  }
  return limit;
}

export function createInteractionClient(transport: InteractionClientTransport) {
  return {
    list: (
      chatId: string,
      options?: InteractionRequestOptions | AbortSignal,
    ) => {
      const path = `/chats/${encodeURIComponent(chatId)}/interactions`;
      const init = requestInit(options);
      return init
        ? transport.request<ChatInteraction[]>(path, init)
        : transport.request<ChatInteraction[]>(path);
    },
    history: (chatId: string, options: InteractionHistoryOptions = {}) => {
      const limit = historyLimit(options.limit);
      const path =
        `/chats/${encodeURIComponent(chatId)}/interactions/history` +
        `?limit=${limit}`;
      const init = requestInit(options);
      return init
        ? transport.request<ChatInteractionRecord[]>(path, init)
        : transport.request<ChatInteractionRecord[]>(path);
    },
    respond: (
      chatId: string,
      interactionId: string,
      body: ChatInteractionDecisionRequest,
      options: InteractionRequestOptions = {},
    ) =>
      transport.request<ChatInteractionResolution>(
        `/chats/${encodeURIComponent(chatId)}/interactions/` +
          `${encodeURIComponent(interactionId)}/response`,
        requestInit(options, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      ),
  };
}

export type InteractionClient = ReturnType<typeof createInteractionClient>;
