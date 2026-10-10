import type { ChatControlClientTransport } from "./clients/chatControls.js";
import type { ChatClientTransport } from "./clients/chats.js";
import type { InteractionClientTransport } from "./clients/interactions.js";
import type { RuntimeClientTransport } from "./clients/runtime.js";
import type { TaskClientTransport } from "./clients/tasks.js";

export interface FetchTransportOptions {
  /** QwenPaw API root, for example http://localhost:8004/api. */
  baseUrl: string;
  token?: string;
  agentId?: string;
  headers?: HeadersInit;
  fetch?: typeof globalThis.fetch;
}

export class QwenPawHttpError extends Error {
  readonly status: number;
  readonly body: string;

  constructor(status: number, body: string) {
    super(`QwenPaw Host request failed with status ${status}`);
    this.name = "QwenPawHttpError";
    this.status = status;
    this.body = body;
  }
}

export type QwenPawTransport = ChatClientTransport &
  ChatControlClientTransport &
  RuntimeClientTransport &
  InteractionClientTransport &
  TaskClientTransport;

function responseValue(response: Response): Promise<unknown> {
  if (response.status === 204) return Promise.resolve(undefined);
  const contentType = response.headers.get("Content-Type") ?? "";
  return contentType.includes("application/json")
    ? response.json()
    : response.text();
}

export function createFetchTransport(
  options: FetchTransportOptions,
): QwenPawTransport {
  const baseUrl = options.baseUrl.replace(/\/+$/, "");
  if (!baseUrl) throw new TypeError("baseUrl must not be empty");
  const fetchImpl = options.fetch ?? globalThis.fetch;
  if (typeof fetchImpl !== "function") {
    throw new TypeError("A Fetch implementation is required");
  }

  const toUrl = (path: string) =>
    `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  const send = (path: string, init: RequestInit = {}) => {
    const headers = new Headers(options.headers);
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    if (options.token && !headers.has("Authorization")) {
      headers.set("Authorization", `Bearer ${options.token}`);
    }
    if (options.agentId && !headers.has("X-Agent-Id")) {
      headers.set("X-Agent-Id", options.agentId);
    }
    if (init.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetchImpl(toUrl(path), { ...init, headers });
  };

  return {
    toUrl,
    openStream: send,
    async request<T>(path: string, init?: RequestInit): Promise<T> {
      const response = await send(path, init);
      if (!response.ok) {
        const body = (await response.text().catch(() => "")).slice(0, 4096);
        throw new QwenPawHttpError(response.status, body);
      }
      return (await responseValue(response)) as T;
    },
  };
}
