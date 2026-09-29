import { request } from "../request";
import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type { ArtifactRef, EvidenceRef } from "../types";

export type TaskStatus =
  | "created"
  | "planned"
  | "running"
  | "waiting_approval"
  | "suspended"
  | "completed"
  | "failed"
  | "cancelled";

export interface LiteTask {
  task_id: string;
  objective: string;
  status: TaskStatus;
  source: string;
  agent_id: string;
  version: number;
  active_run_id: string | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface TaskRun {
  run_id: string;
  attempt: number;
  status: string;
  runner_id: string;
  strategy_id?: string | null;
  registry_generation: number;
  checkpoint_id: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskPlanStep {
  step_id: string;
  title: string;
  objective: string;
  depends_on: string[];
}

export interface TaskPlan {
  plan_id: string;
  revision: number;
  steps: TaskPlanStep[];
  acceptance_criteria: string[];
}

export interface TaskEvent {
  event_id: string;
  task_id: string;
  run_id: string | null;
  sequence: number;
  event_type: string;
  occurred_at: string;
  payload: Record<string, unknown>;
  artifact_refs: TaskArtifactRef[];
  evidence_refs: TaskEvidence[];
}

export type TaskArtifactRef = ArtifactRef;

export interface TaskArtifact extends TaskArtifactRef {
  preview: {
    available: boolean;
    registry_generation: number;
    renderer_id: string | null;
    reason: "" | "too_large" | "unsupported";
  };
}

export type TaskEvidence = EvidenceRef;

export interface TaskApproval {
  approval_id: string;
  task_id: string;
  run_id: string | null;
  source: "tool" | "driver" | "harness" | "proposal" | "system";
  action: string;
  risk: "low" | "medium" | "high" | "critical";
  policy: string;
  continuation: "fail_on_rejection" | "resume_on_decision";
  status: "pending" | "approved" | "denied" | "expired" | "cancelled";
  redacted_arguments: Record<string, unknown>;
  display: {
    title: string;
    summary: string;
    target: string;
    provider: string;
  } | null;
  expires_at: string | null;
  created_at: string;
  decision: {
    decision: "approved" | "denied" | "expired" | "cancelled";
    reason: string;
    scope: "exact" | "similar";
    decided_at: string;
  } | null;
}

export interface TaskConversationMessage {
  role: "user" | "assistant";
  text: string;
  run_id: string | null;
  created_at: string;
  completed_at: string;
}

export interface TaskCapability {
  capability_id: string;
  slot: string;
  registry_generation: number;
}

export interface TaskCapabilityDescriptor {
  capability_id: string;
  slot: string;
  provider_id: string;
  provider_kind: "system" | "plugin";
  version: string;
  metadata: Record<string, unknown>;
}

export interface TaskProjection {
  task: LiteTask;
  active_run: TaskRun | null;
  runs: TaskRun[];
  latest_plan: TaskPlan | null;
  conversation_messages: TaskConversationMessage[];
  tool_activities: TaskEvent[];
  pending_approvals: TaskApproval[];
  recent_decisions: TaskApproval[];
  artifacts: TaskArtifact[];
  evidence: TaskEvidence[];
  checkpoint: Record<string, unknown> | null;
  capabilities: TaskCapability[];
  last_sequence: number;
}

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
