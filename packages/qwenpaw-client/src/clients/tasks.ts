import type {
  KernelTask,
  TaskApproval,
  TaskApprovalDecision,
  TaskArtifact,
  TaskCapabilityDescriptor,
  TaskEvent,
  TaskEvidence,
  TaskPlan,
  TaskProjection,
  TaskRun,
} from "../contracts/tasks.js";

export interface TaskClientTransport {
  request<T>(path: string, init?: RequestInit): Promise<T>;
  openStream(path: string, init?: RequestInit): Promise<Response>;
  toUrl?(path: string): string;
}

export interface CreateTaskInput {
  objective: string;
  constraints?: string[];
  acceptance_criteria?: string[];
  project_dir?: string;
  runner_id?: string;
  strategy_id?: string;
  approval_level?: "strict" | "smart" | "auto" | "off";
}

export interface TaskApprovalDecisionInput {
  decision: "approved" | "denied";
  reason: string;
  scope?: "exact" | "similar";
}

function dispatchSseFrame(frame: string): TaskEvent | undefined {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  return data ? (JSON.parse(data) as TaskEvent) : undefined;
}

async function* followTaskEvents(
  transport: TaskClientTransport,
  taskId: string,
  afterSequence = 0,
  signal?: AbortSignal,
): AsyncGenerator<TaskEvent> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (afterSequence > 0) headers["Last-Event-ID"] = String(afterSequence);
  const response = await transport.openStream(
    `/tasks/${encodeURIComponent(taskId)}/stream`,
    { headers, signal },
  );
  if (!response.ok || !response.body) {
    throw new Error(`Task event stream failed: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const event = dispatchSseFrame(frame);
        if (event) yield event;
      }
      if (done) break;
    }
    if (buffer.trim()) {
      const event = dispatchSseFrame(buffer);
      if (event) yield event;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}

export function createTaskClient(transport: TaskClientTransport) {
  const encoded = encodeURIComponent;
  return {
    create: (
      body: CreateTaskInput,
      options: { signal?: AbortSignal; idempotencyKey?: string } = {},
    ) =>
      transport.request<KernelTask>("/tasks", {
        method: "POST",
        ...(options.idempotencyKey
          ? { headers: { "Idempotency-Key": options.idempotencyKey } }
          : {}),
        body: JSON.stringify(body),
        ...(options.signal ? { signal: options.signal } : {}),
      }),
    list: (cursor?: string, signal?: AbortSignal) => {
      const query = cursor ? `?cursor=${encoded(cursor)}` : "";
      return transport.request<{
        items: KernelTask[];
        next_cursor: string | null;
      }>(`/tasks${query}`, { signal });
    },
    get: (taskId: string, signal?: AbortSignal) =>
      transport.request<{
        task: KernelTask;
        runs: TaskRun[];
        plan: TaskPlan | null;
      }>(`/tasks/${encoded(taskId)}`, { signal }),
    capabilities: (slot = "strategy", signal?: AbortSignal) =>
      transport.request<{
        registry_generation: number;
        items: TaskCapabilityDescriptor[];
      }>(`/tasks/capabilities?slot=${encoded(slot)}`, { signal }),
    projection: (taskId: string, signal?: AbortSignal) =>
      transport.request<TaskProjection>(
        `/tasks/${encoded(taskId)}/projection`,
        { signal },
      ),
    events: (taskId: string, signal?: AbortSignal) =>
      transport.request<{ items: TaskEvent[] }>(
        `/tasks/${encoded(taskId)}/events`,
        { signal },
      ),
    follow: (taskId: string, afterSequence = 0, signal?: AbortSignal) =>
      followTaskEvents(transport, taskId, afterSequence, signal),
    approvals: (taskId: string, signal?: AbortSignal) =>
      transport.request<{ items: TaskApproval[] }>(
        `/tasks/${encoded(taskId)}/approvals?status=pending`,
        { signal },
      ),
    artifacts: (taskId: string, signal?: AbortSignal) =>
      transport.request<{
        items: TaskArtifact[];
        evidence: TaskEvidence[];
      }>(`/tasks/${encoded(taskId)}/artifacts`, { signal }),
    artifactContent: (
      taskId: string,
      artifactId: string,
      signal?: AbortSignal,
    ) =>
      transport.request<string>(
        `/tasks/${encoded(taskId)}/artifacts/${encoded(artifactId)}/content`,
        { signal },
      ),
    artifactDownloadUrl: (taskId: string, artifactId: string) => {
      const path =
        `/tasks/${encoded(taskId)}/artifacts/${encoded(artifactId)}/content` +
        "?disposition=attachment";
      return transport.toUrl?.(path) ?? path;
    },
    cancel: (taskId: string, options: { idempotencyKey?: string } = {}) =>
      transport.request<KernelTask>(`/tasks/${encoded(taskId)}/cancel`, {
        method: "POST",
        ...(options.idempotencyKey
          ? { headers: { "Idempotency-Key": options.idempotencyKey } }
          : {}),
      }),
    start: (taskId: string, signal?: AbortSignal) =>
      transport.request<{ task: KernelTask; run: TaskRun }>(
        `/tasks/${encoded(taskId)}/start`,
        { method: "POST", ...(signal ? { signal } : {}) },
      ),
    pollSensor: (capabilityId: string) =>
      transport.request<{ items: KernelTask[] }>(
        `/tasks/sensors/${encoded(capabilityId)}/poll`,
        { method: "POST" },
      ),
    resume: (taskId: string) =>
      transport.request<{ task: KernelTask; run: TaskRun }>(
        `/tasks/${encoded(taskId)}/resume`,
        { method: "POST" },
      ),
    decideApproval: (
      taskId: string,
      approvalId: string,
      body: TaskApprovalDecisionInput,
      options: { idempotencyKey?: string } = {},
    ) =>
      transport.request<TaskApprovalDecision>(
        `/tasks/${encoded(taskId)}/approvals/` +
          `${encoded(approvalId)}/decision`,
        {
          method: "POST",
          ...(options.idempotencyKey
            ? { headers: { "Idempotency-Key": options.idempotencyKey } }
            : {}),
          body: JSON.stringify(body),
        },
      ),
  };
}

export type TaskClient = ReturnType<typeof createTaskClient>;
