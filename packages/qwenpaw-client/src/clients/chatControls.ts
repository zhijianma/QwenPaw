import type {
  ChatControlRequest,
  ChatQueueReorderRequest,
  ChatSteerRequest,
  ChatSubmissionRequest,
  ControlReceipt,
  QueueProjection,
} from "../contracts/chatControls.js";

export interface ChatControlClientTransport {
  request<T>(path: string, init?: RequestInit): Promise<T>;
}

export interface ChatControlRequestOptions {
  agentId?: string;
  signal?: AbortSignal;
}

function requestInit(
  method: "GET" | "POST",
  body: unknown,
  options: ChatControlRequestOptions,
): RequestInit {
  return {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    ...(options.agentId ? { headers: { "X-Agent-Id": options.agentId } } : {}),
    ...(options.signal ? { signal: options.signal } : {}),
  };
}

export function createChatControlClient(transport: ChatControlClientTransport) {
  const encoded = encodeURIComponent;
  return {
    submit: (
      chatId: string,
      body: ChatSubmissionRequest,
      options: ChatControlRequestOptions = {},
    ) =>
      transport.request<ControlReceipt>(
        `/chats/${encoded(chatId)}/submissions`,
        requestInit("POST", body, options),
      ),
    queue: (chatId: string, options: ChatControlRequestOptions = {}) =>
      transport.request<QueueProjection>(
        `/chats/${encoded(chatId)}/queue`,
        requestInit("GET", undefined, options),
      ),
    steer: (
      chatId: string,
      body: ChatSteerRequest,
      options: ChatControlRequestOptions = {},
    ) =>
      transport.request<ControlReceipt>(
        `/chats/${encoded(chatId)}/control/steer`,
        requestInit("POST", body, options),
      ),
    interrupt: (
      chatId: string,
      body: ChatControlRequest,
      options: ChatControlRequestOptions = {},
    ) =>
      transport.request<ControlReceipt>(
        `/chats/${encoded(chatId)}/control/interrupt`,
        requestInit("POST", body, options),
      ),
    stopAndClear: (
      chatId: string,
      body: ChatControlRequest,
      options: ChatControlRequestOptions = {},
    ) =>
      transport.request<ControlReceipt>(
        `/chats/${encoded(chatId)}/control/stop-and-clear`,
        requestInit("POST", body, options),
      ),
    cancelQueued: (
      chatId: string,
      submissionId: string,
      body: ChatControlRequest,
      options: ChatControlRequestOptions = {},
    ) =>
      transport.request<ControlReceipt>(
        `/chats/${encoded(chatId)}/queue/${encoded(submissionId)}/cancel`,
        requestInit("POST", body, options),
      ),
    reorder: (
      chatId: string,
      body: ChatQueueReorderRequest,
      options: ChatControlRequestOptions = {},
    ) =>
      transport.request<ControlReceipt>(
        `/chats/${encoded(chatId)}/queue/reorder`,
        requestInit("POST", body, options),
      ),
  };
}

export type ChatControlClient = ReturnType<typeof createChatControlClient>;
