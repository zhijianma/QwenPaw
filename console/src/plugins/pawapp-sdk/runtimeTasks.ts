import { hostFetch } from "../hostSdk/fetch";
import type {
  PawRuntimeTask,
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

interface CapabilityDescriptor {
  capability_id: string;
}

interface CapabilityCatalog {
  items: CapabilityDescriptor[];
}

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

async function* taskEvents(
  taskId: string,
  afterSequence: number,
  signal: AbortSignal,
): AsyncGenerator<PawRuntimeTaskEvent> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (afterSequence > 0) {
    headers["Last-Event-ID"] = String(afterSequence);
  }
  const response = await hostFetch(
    `/tasks/${encodeURIComponent(taskId)}/stream`,
    { headers, signal },
  );
  if (!response.ok) await readJson(response);
  const reader = response.body?.getReader();
  if (!reader) {
    throw new PawRuntimeTaskError(
      "TASK_STREAM_UNAVAILABLE",
      `Task ${taskId} event stream has no response body`,
      { taskId },
    );
  }
  const decoder = new TextDecoder();
  let buffer = "";

  function decodeFrame(frame: string): PawRuntimeTaskEvent | undefined {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    return data ? (JSON.parse(data) as PawRuntimeTaskEvent) : undefined;
  }

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const event = decodeFrame(frame);
        if (event) yield event;
      }
      if (done) break;
    }
    const finalEvent = decodeFrame(buffer);
    if (finalEvent) yield finalEvent;
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

async function getProjection(
  taskId: string,
): Promise<PawRuntimeTaskProjection> {
  return readJson<PawRuntimeTaskProjection>(
    await hostFetch(`/tasks/${encodeURIComponent(taskId)}/projection`),
  );
}

async function assertCapabilityAvailable(
  slot: "runner" | "strategy",
  capabilityId?: string,
): Promise<void> {
  if (!capabilityId) return;
  const catalog = await readJson<CapabilityCatalog>(
    await hostFetch(`/tasks/capabilities?slot=${slot}`),
  );
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
  const created = await readJson<PawRuntimeTask>(
    await hostFetch("/tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": createIdempotencyKey,
      },
      body: JSON.stringify({
        objective: request.objective,
        constraints: request.constraints ?? [],
        acceptance_criteria: request.acceptanceCriteria ?? [],
        project_dir: request.projectDir,
        runner_id: request.runnerId,
        strategy_id: request.strategyId,
        approval_level: request.approvalLevel,
      }),
      signal: options.signal,
    }),
  );
  await readJson(
    await hostFetch(`/tasks/${encodeURIComponent(created.task_id)}/start`, {
      method: "POST",
      signal: options.signal,
    }),
  );

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
          for await (const event of taskEvents(
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
          const task = await readJson<PawRuntimeTask>(
            await hostFetch(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
              method: "POST",
              headers: { "Idempotency-Key": idempotencyKey },
            }),
          );
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
        pending.promise = hostFetch(
          `/tasks/${encodeURIComponent(taskId)}/approvals/` +
            `${encodeURIComponent(approvalId)}/decision`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": pending.idempotencyKey,
            },
            body: JSON.stringify({
              decision: command.decision,
              reason: command.reason,
              scope: command.scope ?? "exact",
            }),
          },
        )
          .then((response) =>
            readJson<PawRuntimeTaskApprovalDecision>(response),
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
  return { run: runTask };
}
