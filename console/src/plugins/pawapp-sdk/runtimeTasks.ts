import { hostFetch } from "../hostSdk/fetch";
import { createTaskClient } from "../../clients/taskClient";
import { createHostRuntimeNamespace } from "./runtime";
import type {
  PawRuntimeTaskApprovalDecision,
  PawRuntimeTaskCancelReceipt,
  PawRuntimeTaskEvent,
  PawRuntimeTaskHandle,
  PawRuntimeTaskProjection,
  PawRuntimeTaskRequest,
  PawRuntimeTaskRunOptions,
  PawRuntimeTasksNamespace,
  PawTaskEventHandler,
} from "./types";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

interface PendingApprovalDecision {
  readonly signature: string;
  readonly idempotencyKey: string;
  promise?: Promise<PawRuntimeTaskApprovalDecision>;
}

export class PawRuntimeTaskError extends Error {
  readonly code: string;
  readonly taskId?: string;
  readonly projection?: PawRuntimeTaskProjection;

  constructor(
    code: string,
    message: string,
    options: {
      taskId?: string;
      projection?: PawRuntimeTaskProjection;
    } = {},
  ) {
    super(message);
    this.name = "PawRuntimeTaskError";
    this.code = code;
    this.taskId = options.taskId;
    this.projection = options.projection;
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>;
  const text = await response.text().catch(() => "");
  let code = `HTTP_${response.status}`;
  let message = text || response.statusText;
  try {
    const parsed = JSON.parse(text) as {
      code?: unknown;
      detail?: unknown;
      message?: unknown;
    };
    const detail = parsed.detail ?? parsed.message;
    if (typeof parsed.code === "string") code = parsed.code;
    if (typeof detail === "string") message = detail;
    if (typeof detail === "object" && detail !== null) {
      const problem = detail as { code?: unknown; message?: unknown };
      if (typeof problem.code === "string") code = problem.code;
      if (typeof problem.message === "string") message = problem.message;
    }
  } catch {
    // Preserve non-JSON response text.
  }
  throw new PawRuntimeTaskError(code, message || "Task request failed");
}

function normalizedHostInit(init?: RequestInit): RequestInit | undefined {
  if (!init) return undefined;
  const normalized = { ...init };
  if (normalized.signal === undefined) delete normalized.signal;
  const headers = {
    ...(normalized.body != null ? { "Content-Type": "application/json" } : {}),
    ...((normalized.headers as Record<string, string>) ?? {}),
  };
  if (Object.keys(headers).length > 0) normalized.headers = headers;
  else delete normalized.headers;
  return Object.keys(normalized).length > 0 ? normalized : undefined;
}

const taskClient = createTaskClient({
  request: async <T>(path: string, init?: RequestInit): Promise<T> => {
    const normalized = normalizedHostInit(init);
    const response = normalized
      ? await hostFetch(path, normalized)
      : await hostFetch(path);
    return readJson<T>(response);
  },
  openStream: async (path, init) => {
    const response = await hostFetch(path, init);
    if (!response.ok) await readJson(response);
    return response;
  },
});

function isTerminal(projection: PawRuntimeTaskProjection): boolean {
  return TERMINAL_STATUSES.has(projection.task.status);
}

function terminalResult(
  projection: PawRuntimeTaskProjection,
): PawRuntimeTaskProjection {
  if (projection.task.status === "completed") return projection;
  throw new PawRuntimeTaskError(
    `TASK_${projection.task.status.toUpperCase()}`,
    `Task ${projection.task.task_id} ended with status ` +
      projection.task.status,
    { taskId: projection.task.task_id, projection },
  );
}

async function wait(delayMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) {
    throw signal.reason ?? new DOMException("Aborted", "AbortError");
  }
  if (delayMs <= 0) return;
  await new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function getProjection(
  taskId: string,
): Promise<PawRuntimeTaskProjection> {
  return taskClient.projection(taskId);
}

async function assertCapabilityAvailable(
  slot: "runner" | "strategy",
  capabilityId?: string,
): Promise<void> {
  if (!capabilityId) return;
  const catalog = await taskClient.capabilities(slot);
  if (!catalog.items.some((item) => item.capability_id === capabilityId)) {
    throw new PawRuntimeTaskError(
      `${slot.toUpperCase()}_CAPABILITY_UNAVAILABLE`,
      `${slot} capability '${capabilityId}' is not active`,
    );
  }
}

async function runTask(
  request: PawRuntimeTaskRequest,
  options: PawRuntimeTaskRunOptions = {},
): Promise<PawRuntimeTaskHandle> {
  await Promise.all([
    assertCapabilityAvailable("runner", request.runnerId),
    assertCapabilityAvailable("strategy", request.strategyId),
  ]);
  const createIdempotencyKey = options.idempotencyKey ?? crypto.randomUUID();
  const created = await taskClient.create(
    {
      objective: request.objective,
      constraints: request.constraints ?? [],
      acceptance_criteria: request.acceptanceCriteria ?? [],
      project_dir: request.projectDir,
      runner_id: request.runnerId,
      strategy_id: request.strategyId,
      approval_level: request.approvalLevel,
    },
    { signal: options.signal, idempotencyKey: createIdempotencyKey },
  );
  await taskClient.start(created.task_id, options.signal);

  const taskId = created.task_id;
  const listeners = new Map<
    string,
    Set<PawTaskEventHandler<PawRuntimeTaskEvent>>
  >();
  const streamController = new AbortController();
  const externalAbort = () => streamController.abort(options.signal?.reason);
  if (options.signal?.aborted) externalAbort();
  else options.signal?.addEventListener("abort", externalAbort, { once: true });
  let lastSequence = 0;
  let resultPromise: Promise<PawRuntimeTaskProjection> | undefined;
  let cancelPromise: Promise<PawRuntimeTaskCancelReceipt> | undefined;
  const approvalCommands = new Map<string, PendingApprovalDecision>();

  function emit(event: PawRuntimeTaskEvent) {
    for (const key of [event.event_type, "event"]) {
      for (const handler of listeners.get(key) ?? []) {
        try {
          handler(event);
        } catch (error) {
          console.error(`[PawRuntimeTask] Error in ${key} handler:`, error);
        }
      }
    }
  }

  async function monitor(): Promise<PawRuntimeTaskProjection> {
    const reconnectAttempts = Math.max(0, options.reconnectAttempts ?? 3);
    const reconnectDelayMs = Math.max(0, options.reconnectDelayMs ?? 250);
    let disconnects = 0;
    try {
      while (true) {
        try {
          for await (const event of taskClient.follow(
            taskId,
            lastSequence,
            streamController.signal,
          )) {
            if (event.sequence <= lastSequence) continue;
            lastSequence = event.sequence;
            emit(event);
          }
        } catch (error) {
          if (streamController.signal.aborted) throw error;
        }

        const projection = await getProjection(taskId);
        lastSequence = Math.max(lastSequence, projection.last_sequence);
        if (isTerminal(projection)) return terminalResult(projection);
        if (disconnects >= reconnectAttempts) {
          throw new PawRuntimeTaskError(
            "TASK_STREAM_INTERRUPTED",
            `Task ${taskId} stream disconnected before a durable terminal ` +
              "projection was available",
            { taskId, projection },
          );
        }
        disconnects += 1;
        await wait(
          Math.min(reconnectDelayMs * disconnects, 2_000),
          streamController.signal,
        );
      }
    } finally {
      options.signal?.removeEventListener("abort", externalAbort);
    }
  }

  function ensureMonitor(): Promise<PawRuntimeTaskProjection> {
    resultPromise ??= monitor();
    return resultPromise;
  }

  const handle: PawRuntimeTaskHandle = {
    taskId,
    get lastSequence() {
      return lastSequence;
    },
    get result() {
      return ensureMonitor();
    },
    on(event, handler) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event)!.add(handler);
      void ensureMonitor().catch(() => undefined);
      return handle;
    },
    off(event, handler) {
      listeners.get(event)?.delete(handler);
      return handle;
    },
    projection: () => getProjection(taskId),
    cancel() {
      if (!cancelPromise) {
        const idempotencyKey = crypto.randomUUID();
        cancelPromise = (async () => {
          const task = await taskClient.cancel(taskId, { idempotencyKey });
          return { taskId, idempotencyKey, task };
        })();
      }
      return cancelPromise;
    },
    decideApproval(approvalId, command) {
      const signature = JSON.stringify({
        decision: command.decision,
        reason: command.reason,
        scope: command.scope ?? "exact",
      });
      let pending = approvalCommands.get(approvalId);
      if (pending && pending.signature !== signature) {
        return Promise.reject(
          new PawRuntimeTaskError(
            "APPROVAL_COMMAND_CONFLICT",
            `Approval ${approvalId} already has another local decision`,
            { taskId },
          ),
        );
      }
      if (!pending) {
        pending = {
          signature,
          idempotencyKey: command.idempotencyKey ?? crypto.randomUUID(),
        };
        approvalCommands.set(approvalId, pending);
      }
      if (!pending.promise) {
        pending.promise = taskClient
          .decideApproval(
            taskId,
            approvalId,
            {
              decision: command.decision,
              reason: command.reason,
              scope: command.scope ?? "exact",
            },
            { idempotencyKey: pending.idempotencyKey },
          )
          .catch((error: unknown) => {
            pending!.promise = undefined;
            throw error;
          });
      }
      return pending.promise;
    },
  };
  return handle;
}

export function createRuntimeTasksNamespace(): PawRuntimeTasksNamespace {
  const hostRuntime = createHostRuntimeNamespace();
  return {
    async run(request, options) {
      try {
        await hostRuntime.require(
          [
            "artifact.references",
            "capability.catalog",
            "chat.interactions",
            "task.event-cursor",
            "task.runtime",
          ],
          { signal: options?.signal },
        );
      } catch (error) {
        const detail = error as { code?: unknown; message?: unknown };
        throw new PawRuntimeTaskError(
          typeof detail.code === "string"
            ? detail.code
            : "HOST_PROTOCOL_INCOMPATIBLE",
          typeof detail.message === "string"
            ? detail.message
            : "QwenPaw Host protocol negotiation failed",
        );
      }
      return runTask(request, options);
    },
  };
}
