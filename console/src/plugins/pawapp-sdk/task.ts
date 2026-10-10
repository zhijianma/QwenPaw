/**
 * pawapp-sdk/task.ts — PawTask: long-running task with SSE event stream.
 *
 * Usage:
 *   const task = paw.api.task('/generate', { script });
 *   task.on('progress', (data) => setProgress(data.step));
 *   task.on('image_ready', (data) => addImage(data.url));
 *   const result = await task.result;
 */
import { hostFetch } from "../hostSdk/fetch";
import type {
  PawTaskEventHandler,
  PawTaskHandle,
  PawTaskInteractionAnswer,
  PawTaskInteractionRequest,
  PawTaskOptions,
} from "./types";
import { normalizeAppId, normalizeAppRelativePath } from "./scope";

export class PawTaskTransportError extends Error {
  readonly code = "PAW_TASK_STREAM_INTERRUPTED";

  constructor(taskId: string) {
    super(
      `PawTask ${taskId} stream ended before a durable terminal result. ` +
        "The task outcome is unknown.",
    );
    this.name = "PawTaskTransportError";
  }
}

/**
 * Create a PawTask — posts to backend to start task, then connects
 * to SSE stream for realtime events.
 */
function createPawTaskWithScope(
  appId: string,
  path: string,
  params?: unknown,
  strictScope = false,
  options: PawTaskOptions = {},
): PawTaskHandle {
  const listeners = new Map<string, Set<PawTaskEventHandler>>();
  const chatId =
    options.chatId ?? window.QwenPaw?.host?.getCurrentChatId?.() ?? undefined;
  let taskId = "";
  let abortController: AbortController | null = new AbortController();

  // Promise that resolves with the final result
  let resolveResult!: (value: unknown) => void;
  let rejectResult!: (reason: unknown) => void;
  const resultPromise = new Promise<unknown>((resolve, reject) => {
    resolveResult = resolve;
    rejectResult = reject;
  });

  function emit(event: string, data: unknown) {
    const handlers = listeners.get(event);
    if (handlers) {
      for (const handler of handlers) {
        try {
          handler(data);
        } catch (e) {
          console.error(`[PawTask] Error in ${event} handler:`, e);
        }
      }
    }
  }

  async function respond(
    request: PawTaskInteractionRequest,
    answer: PawTaskInteractionAnswer,
  ): Promise<unknown> {
    const response = await hostFetch(
      `/chats/${encodeURIComponent(request.chat_id)}` +
        `/interactions/${encodeURIComponent(request.interaction_id)}` +
        "/response",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Agent-Id": request.agent_id,
        },
        body: JSON.stringify({
          idempotency_key: answer.idempotencyKey ?? crypto.randomUUID(),
          expected_revision: request.revision,
          selected_option_ids: [answer.optionId],
          text: answer.text ?? "",
          values: answer.values ?? {},
        }),
      },
    );
    if (!response.ok) {
      throw new Error(
        `Interaction response failed: ${response.status} ` +
          response.statusText,
      );
    }
    return response.json();
  }

  // Start the task asynchronously
  (async () => {
    try {
      // POST to create task
      const normalizedAppId = strictScope ? normalizeAppId(appId) : appId;
      const normalized = strictScope
        ? normalizeAppRelativePath(path)
        : path.startsWith("/")
        ? path
        : `/${path}`;
      // Use unified route: /{appId}/... -> /api/{appId}/... via getApiUrl
      const createRes = await hostFetch(`/${normalizedAppId}${normalized}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(chatId ? { "X-QwenPaw-Chat-Id": chatId } : {}),
        },
        body: params != null ? JSON.stringify(params) : undefined,
        signal: abortController?.signal,
      });

      if (!createRes.ok) {
        throw new Error(
          `Task creation failed: ${createRes.status} ${createRes.statusText}`,
        );
      }

      const createData = await createRes.json();
      taskId = createData.task_id ?? createData.taskId ?? "";

      if (!taskId) {
        throw new Error("No task_id returned from backend");
      }

      // Connect to SSE stream using fetch-based approach
      // (EventSource doesn't support custom auth headers)
      const sseRes = await hostFetch(
        `/${normalizedAppId}/task/${encodeURIComponent(taskId)}/stream`,
        {
          headers: {
            Accept: "text/event-stream",
          },
          signal: abortController?.signal,
        },
      );

      if (!sseRes.ok || !sseRes.body) {
        throw new Error(`SSE connection failed: ${sseRes.status}`);
      }

      const reader = sseRes.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const eventData = JSON.parse(line.slice(6));
              const eventType = eventData.type ?? eventData.event ?? "message";

              if (eventType === "done") {
                emit("done", eventData.data ?? eventData);
                resolveResult(eventData.data ?? eventData.result ?? null);
                return;
              } else if (eventType === "error") {
                const err = new Error(eventData.message ?? "Task failed");
                emit("error", eventData);
                rejectResult(err);
                return;
              } else if (eventType === "pawapp:confirm_request") {
                emit(eventType, eventData);
              } else {
                emit(eventType, eventData.data ?? eventData);
              }
            } catch {
              // Non-JSON line, emit as raw
              emit("message", line.slice(6));
            }
          }
        }
      }

      // The legacy stream has no durable cursor. Transport EOF is not proof
      // that the task completed, so never turn it into a successful null.
      const interrupted = new PawTaskTransportError(taskId);
      emit("error", {
        type: "error",
        code: interrupted.code,
        message: interrupted.message,
      });
      throw interrupted;
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        rejectResult(new Error("Task cancelled"));
      } else {
        rejectResult(err);
      }
    }
  })();

  const handle: PawTaskHandle = {
    on(event, handler) {
      if (!listeners.has(event)) {
        listeners.set(event, new Set());
      }
      listeners.get(event)!.add(handler);
      return handle;
    },
    off(event, handler) {
      listeners.get(event)?.delete(handler);
      return handle;
    },
    respond,
    cancel() {
      if (taskId) {
        const normalizedAppId = strictScope ? normalizeAppId(appId) : appId;
        void hostFetch(
          `/${normalizedAppId}/task/${encodeURIComponent(taskId)}/cancel`,
          { method: "POST" },
        ).catch(() => undefined);
      }
      abortController?.abort();
      abortController = null;
    },
    get result() {
      return resultPromise;
    },
    get taskId() {
      return taskId;
    },
  };

  return handle;
}

/** Legacy path-compatible task factory. */
export function createPawTask(
  appId: string,
  path: string,
  params?: unknown,
  options?: PawTaskOptions,
): PawTaskHandle {
  return createPawTaskWithScope(appId, path, params, false, options);
}

/** @internal Strict task factory used by permanent app-scoped handles. */
export function createScopedPawTask(
  appId: string,
  path: string,
  params?: unknown,
  options?: PawTaskOptions,
): PawTaskHandle {
  return createPawTaskWithScope(appId, path, params, true, options);
}
