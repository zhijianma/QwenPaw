import type {
  ApprovalDecision,
  ApprovalStatus,
  ArtifactRef,
  EvidenceRef,
  ExecutionEvent,
  Plan,
  PlanStep,
  Run,
  Task,
  TaskApprovalResponse,
  TaskArtifactResponse,
  TaskCapabilitySelectionResponse,
  TaskConversationMessageResponse,
  TaskProjectionResponse,
  TaskStatus as GeneratedTaskStatus,
} from "../generated/taskProjection.js";

export type TaskStatus = GeneratedTaskStatus;
export type KernelTask = Task;
export type TaskRun = Run;
export type TaskPlanStep = PlanStep;
export type TaskPlan = Plan;
export type TaskArtifactRef = ArtifactRef;
export type TaskEvidence = EvidenceRef;
export type TaskEvent = ExecutionEvent;
export type TaskArtifact = TaskArtifactResponse;
export type TaskApprovalStatus = ApprovalStatus;
export type TaskApprovalDecision = ApprovalDecision;
export type TaskApproval = TaskApprovalResponse;
export type TaskConversationMessage = TaskConversationMessageResponse;
export type TaskCapability = TaskCapabilitySelectionResponse;
export type TaskProjection = TaskProjectionResponse;

/** Catalog response; generation selection remains a separate Host query. */
export interface TaskCapabilityDescriptor {
  schema?: "qwenpaw.kernel-model.v1";
  capability_id: string;
  slot: string;
  provider_id: string;
  provider_kind: "system" | "plugin";
  version: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  config_schema?: Record<string, unknown> | null;
  restart_policy?: "hot" | "restart_required";
  metadata: Record<string, unknown>;
}
