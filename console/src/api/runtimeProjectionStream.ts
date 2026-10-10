import { buildAuthHeaders } from "./authHeaders";
import { getApiUrl } from "./config";
import { request } from "./request";
import { createChatClient } from "../clients/chatClient";
import type { ConversationRuntimeProjection } from "./types";

interface RuntimeProjectionStreamOptions {
  chatId: string;
  agentId?: string;
  afterCursor?: string;
  signal?: AbortSignal;
  onSnapshot: (projection: ConversationRuntimeProjection) => void;
}

const chatClient = createChatClient({
  request,
  openStream: (path, init) => {
    const headers = new Headers(init?.headers);
    return fetch(getApiUrl(path), {
      ...init,
      headers: {
        ...buildAuthHeaders(),
        Accept: headers.get("Accept") ?? "text/event-stream",
        ...(headers.get("X-Agent-Id")
          ? { "X-Agent-Id": headers.get("X-Agent-Id") as string }
          : {}),
        ...(headers.get("Last-Event-ID")
          ? { "Last-Event-ID": headers.get("Last-Event-ID") as string }
          : {}),
      },
    });
  },
});

export async function streamRuntimeProjection(
  options: RuntimeProjectionStreamOptions,
): Promise<string | undefined> {
  try {
    const stream = chatClient.followRuntime(options.chatId, {
      agentId: options.agentId,
      afterCursor: options.afterCursor,
      signal: options.signal,
    });
    while (true) {
      const result = await stream.next();
      if (result.done) return result.value;
      options.onSnapshot(result.value as ConversationRuntimeProjection);
    }
  } catch (error) {
    const failure = error as { status?: unknown; body?: unknown };
    if (typeof failure.status === "number") {
      const detail = typeof failure.body === "string" ? failure.body : "";
      throw new Error(
        detail
          ? `Runtime stream failed: ${failure.status} - ${detail}`
          : `Runtime stream failed: ${failure.status}`,
        { cause: error },
      );
    }
    throw error;
  }
}

export type { RuntimeProjectionStreamOptions };
