import type { ArtifactRef, EvidenceRef } from "./artifacts";

export type TaskStatus =
  | "created"
  | "planned"
  | "running"
  | "waiting_approval"
  | "suspended"
  | "completed"
  | "failed"
  | "cancelled";

export interface KernelTask {
  task_id: string;
  objective: string;
  status: TaskStatus;
  source: string;
  agent_id: string;
  constraints?: string[];
  acceptance_criteria?: string[];
  execution_contract?: Record<string, unknown> | null;
  version: number;
  active_run_id: string | null;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export interface TaskRun {
  run_id: string;
  task_id?: string;
  plan_id?: string | null;
  attempt: number;
  status: string;
  runner_id: string;
  strategy_id?: string | null;
  registry_generation: number;
  invocation_id?: string | null;
  correlation_id?: string | null;
  checkpoint_id: string | null;
  started_at: string | null;
  execution_deadline_at?: string | null;
  finished_at: string | null;
  metadata?: Record<string, unknown>;
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

export type TaskArtifactRef = ArtifactRef;
export type TaskEvidence = EvidenceRef;

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

export interface TaskArtifact extends TaskArtifactRef {
  preview: {
    available: boolean;
    registry_generation: number;
    renderer_id: string | null;
    reason: "" | "too_large" | "unsupported";
  };
}

export type TaskApprovalStatus =
  | "pending"
  | "approved"
  | "denied"
  | "expired"
  | "cancelled";

export interface TaskApprovalDecision {
  approval_id: string;
  decision: Exclude<TaskApprovalStatus, "pending">;
  actor: {
    type: string;
    id: string;
  };
  scope: "exact" | "similar";
  reason: string;
  decided_at: string;
}

export interface TaskApproval {
  approval_id: string;
  task_id: string;
  run_id: string | null;
  source: "tool" | "driver" | "harness" | "proposal" | "system";
  action: string;
  risk: "low" | "medium" | "high" | "critical";
  policy: string;
  continuation: "fail_on_rejection" | "resume_on_decision";
  status: TaskApprovalStatus;
  redacted_arguments: Record<string, unknown>;
  display: {
    title: string;
    summary: string;
    target: string;
    provider: string;
  } | null;
  expires_at: string | null;
  created_at: string;
  decision: Omit<TaskApprovalDecision, "approval_id" | "actor"> | null;
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
  task: KernelTask;
  active_run: TaskRun | null;
  runs: TaskRun[];
  latest_plan: TaskPlan | null;
  conversation_messages: TaskConversationMessage[];
  tool_activities: TaskEvent[];
  pending_approvals: TaskApproval[];
  recent_decisions: TaskApproval[];
  artifacts: TaskArtifact[];
  artifact_registry?: Array<Record<string, unknown>>;
  evidence: TaskEvidence[];
  evidence_registry?: Array<Record<string, unknown>>;
  verifications?: Array<Record<string, unknown>>;
  verification_registry?: Array<Record<string, unknown>>;
  result_package?: Record<string, unknown> | null;
  usage?: Record<string, unknown>;
  checkpoint: Record<string, unknown> | null;
  capabilities: TaskCapability[];
  last_sequence: number;
}
