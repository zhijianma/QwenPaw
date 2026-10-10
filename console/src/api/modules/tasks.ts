import { request } from "../request";
import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type {
  KernelTask,
  TaskApproval,
  TaskArtifact,
  TaskCapabilityDescriptor,
  TaskEvent,
  TaskEvidence,
  TaskPlan,
  TaskProjection,
  TaskRun,
} from "../../contracts/tasks";

export type LiteTask = KernelTask;
export type {
  TaskApproval,
  TaskArtifact,
  TaskArtifactRef,
  TaskCapability,
  TaskCapabilityDescriptor,
  TaskConversationMessage,
  TaskEvent,
  TaskEvidence,
  TaskPlan,
  TaskPlanStep,
  TaskProjection,
  TaskRun,
  TaskStatus,
} from "../../contracts/tasks";

function dispatchSseFrame(frame: string, onEvent: (event: TaskEvent) => void) {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (data) onEvent(JSON.parse(data) as TaskEvent);
}

async function streamTaskEvents(
  taskId: string,
  onEvent: (event: TaskEvent) => void,
  signal?: AbortSignal,
  afterSequence = 0,
) {
  const headers: Record<string, string> = {
    ...buildAuthHeaders(),
    Accept: "text/event-stream",
  };
  if (afterSequence > 0) {
    headers["Last-Event-ID"] = String(afterSequence);
  }
  const response = await fetch(
    getApiUrl(`/tasks/${encodeURIComponent(taskId)}/stream`),
    { headers, signal },
  );
  if (!response.ok || !response.body) {
    throw new Error(`Task event stream failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    frames.forEach((frame) => dispatchSseFrame(frame, onEvent));
    if (done) break;
  }
  if (buffer.trim()) dispatchSseFrame(buffer, onEvent);
}

export const tasksApi = {
  create: (body: {
    objective: string;
    acceptance_criteria?: string[];
    project_dir?: string;
    runner_id?: string;
    strategy_id?: string;
    approval_level?: "strict" | "smart" | "auto" | "off";
  }) =>
    request<LiteTask>("/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  list: (cursor?: string, signal?: AbortSignal) => {
    const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    return request<{ items: LiteTask[]; next_cursor: string | null }>(
      `/tasks${query}`,
      { signal },
    );
  },
  get: (taskId: string, signal?: AbortSignal) =>
    request<{ task: LiteTask; runs: TaskRun[]; plan: TaskPlan | null }>(
      `/tasks/${taskId}`,
      { signal },
    ),
  capabilities: (slot = "strategy", signal?: AbortSignal) =>
    request<{
      registry_generation: number;
      items: TaskCapabilityDescriptor[];
    }>(`/tasks/capabilities?slot=${encodeURIComponent(slot)}`, { signal }),
  projection: (taskId: string, signal?: AbortSignal) =>
    request<TaskProjection>(`/tasks/${encodeURIComponent(taskId)}/projection`, {
      signal,
    }),
  events: (taskId: string, signal?: AbortSignal) =>
    request<{ items: TaskEvent[] }>(`/tasks/${taskId}/events`, { signal }),
  stream: streamTaskEvents,
  approvals: (taskId: string, signal?: AbortSignal) =>
    request<{ items: TaskApproval[] }>(
      `/tasks/${encodeURIComponent(taskId)}/approvals?status=pending`,
      { signal },
    ),
  artifacts: (taskId: string, signal?: AbortSignal) =>
    request<{ items: TaskArtifact[]; evidence: TaskEvidence[] }>(
      `/tasks/${taskId}/artifacts`,
      { signal },
    ),
  artifactContent: (taskId: string, artifactId: string, signal?: AbortSignal) =>
    request<string>(
      `/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(
        artifactId,
      )}/content`,
      { signal },
    ),
  artifactDownloadUrl: (taskId: string, artifactId: string) =>
    getApiUrl(
      `/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(
        artifactId,
      )}/content?disposition=attachment`,
    ),
  cancel: (taskId: string) =>
    request<LiteTask>(`/tasks/${taskId}/cancel`, { method: "POST" }),
  start: (taskId: string) =>
    request<{ task: LiteTask; run: TaskRun }>(`/tasks/${taskId}/start`, {
      method: "POST",
    }),
  pollSensor: (capabilityId: string) =>
    request<{ items: LiteTask[] }>(
      `/tasks/sensors/${encodeURIComponent(capabilityId)}/poll`,
      { method: "POST" },
    ),
  resume: (taskId: string) =>
    request<{ task: LiteTask; run: TaskRun }>(`/tasks/${taskId}/resume`, {
      method: "POST",
    }),
  decide: (
    taskId: string,
    approvalId: string,
    decision: "approved" | "denied",
  ) =>
    request(`/tasks/${taskId}/approvals/${approvalId}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        reason: "Decided in task workbench",
      }),
    }),
};
