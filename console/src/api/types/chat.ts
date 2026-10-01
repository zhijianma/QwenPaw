import type { ArtifactRef } from "./artifacts";

export type ChatStatus = "idle" | "running";
export type ChatSource = "chat" | "cron" | "subagent";
export type ChatGroupKind = "default" | "cron" | "subagents" | "custom";

export interface ChatGroup {
  id: string;
  name: string;
  order: number;
  kind: ChatGroupKind;
  source?: ChatSource | null;
  pinned: boolean;
}

export interface ChatForkOrigin {
  parent_chat_id: string;
  source_message_id: string;
}

export interface ChatForkRequest {
  source_message_id: string;
  idempotency_key: string;
  name?: string;
}

export interface ExternalQueueFallbackRequest {
  observation_id: string;
  backend_id: string;
}

export interface ExternalQueueFallbackReceipt {
  recorded: boolean;
}

export type InteractionKind = "approval" | "user_input" | "suggestion";
export type InteractionMode = "blocking" | "non_blocking";
export type InteractionStatus = "open" | "resolved" | "expired" | "cancelled";

export interface InteractionOption {
  option_id: string;
  label: string;
  description: string;
  value: Record<string, unknown>;
}

export interface ChatInteraction {
  interaction_id: string;
  kind: InteractionKind;
  mode: InteractionMode;
  agent_id: string;
  conversation_id: string;
  invocation_id: string;
  correlation_id: string;
  source_id?: string | null;
  title: string;
  prompt: string;
  options: InteractionOption[];
  response_schema: Record<string, unknown>;
  metadata: Record<string, unknown>;
  status: InteractionStatus;
  revision: number;
  created_at: string;
  expires_at?: string | null;
}

export interface ChatInteractionDecisionRequest {
  idempotency_key: string;
  expected_revision: number;
  selected_option_ids?: string[];
  text?: string;
  values?: Record<string, unknown>;
}

export interface ChatInteractionResolution {
  interaction_id: string;
  status: InteractionStatus;
  revision: number;
  response?: {
    selected_option_ids: string[];
    text: string;
    values: Record<string, unknown>;
  } | null;
  detail: string;
  resolved_at: string;
}

export type SubmissionStatus =
  | "queued"
  | "admitted"
  | "running"
  | "interrupting"
  | "succeeded"
  | "failed"
  | "interrupted"
  | "cancelled";

export type ControlCommandKind =
  | "steer"
  | "interrupt_current"
  | "cancel_queued"
  | "stop_and_clear"
  | "reorder";

export type ControlCommandStatus =
  | "accepted"
  | "applied"
  | "rejected"
  | "conflict";

export type SteerSafePoint =
  | "before_reasoning"
  | "after_reasoning"
  | "before_tool_batch"
  | "after_tool_batch";

export interface SubmissionInputEnvelope {
  schema?: string;
  kind: string;
  payload: Record<string, unknown>;
}

export interface TurnSubmission {
  schema?: string;
  agent_id: string;
  conversation_id: string;
  priority: number;
  content: string;
  artifact_refs: ArtifactRef[];
  request_context: Record<string, unknown>;
  input_envelope?: SubmissionInputEnvelope | null;
  idempotency_key: string;
  correlation_id: string;
  submission_id: string;
  sequence: number;
  queue_position: number;
  invocation_id?: string | null;
  status: SubmissionStatus;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface QueueProjection {
  schema?: string;
  agent_id: string;
  conversation_id: string;
  revision: number;
  active_submission_id?: string | null;
  submissions: TurnSubmission[];
  updated_at: string;
}

export type RuntimeObservationCategory =
  | "model"
  | "action"
  | "control"
  | "guardrail"
  | "compaction"
  | "hitl"
  | "interrupt"
  | "verification";

export interface RuntimeObservation {
  schema?: "qwenpaw.runtime-observation.v1";
  observation_id: string;
  category: RuntimeObservationCategory;
  stage: "intent" | "policy" | "execution" | "evidence";
  status: string;
  source: { source_type: string; source_id: string };
  task_id?: string | null;
  run_id?: string | null;
  conversation_id?: string | null;
  invocation_id?: string | null;
  correlation_id?: string | null;
  registry_generation?: number | null;
  title: string;
  facts: Record<string, unknown>;
  occurred_at: string;
}

export interface ObservationPage {
  schema?: "qwenpaw.observation-page.v1";
  items: RuntimeObservation[];
  next_cursor?: string | null;
}

export interface ConversationRuntimeProjection {
  schema?: string;
  agent_id: string;
  conversation_id: string;
  queue: QueueProjection;
  interactions: ChatInteraction[];
  activity?: ObservationPage;
  cursor: string;
  observed_at: string;
}

export interface ChatControlRequest {
  idempotency_key: string;
  expected_revision: number;
}

export interface ChatSubmissionRequest
  extends Omit<ChatControlRequest, "expected_revision"> {
  expected_revision?: number;
  content_parts: Array<Record<string, unknown>>;
  request_context?: Record<string, unknown>;
  message_metadata?: Record<string, unknown>;
  model_slot_override?: string | Record<string, unknown> | null;
  request_extensions?: Record<string, unknown>;
  priority?: number;
}

export interface ChatSteerRequest extends ChatControlRequest {
  instruction: string;
}

export interface ChatQueueReorderRequest extends ChatControlRequest {
  ordered_submission_ids: string[];
}

export interface ControlReceipt {
  schema?: string;
  receipt_id: string;
  command_id?: string | null;
  submission_id?: string | null;
  kind: ControlCommandKind | "enqueue";
  status: ControlCommandStatus;
  agent_id: string;
  conversation_id: string;
  revision: number;
  detail: string;
  applied_at_safe_point?: SteerSafePoint | null;
  recorded_at: string;
}

export interface ChatSpec {
  id: string; // Chat UUID identifier
  session_id: string; // Session identifier (channel:user_id format)
  user_id: string; // User identifier
  channel: string; // Channel name, default: "default"
  name?: string; // Chat display name
  created_at: string | null; // Chat creation timestamp (ISO 8601)
  updated_at: string | null; // Chat last update timestamp (ISO 8601)
  last_finished_at?: string | null; // Most recent task completion timestamp
  meta?: Record<string, unknown>; // Additional metadata
  status?: ChatStatus; // Conversation status: idle or running
  pinned?: boolean; // Whether the chat is pinned to the top
  archived_at?: string | null; // When the chat was archived (ISO 8601), null = active
  archived?: boolean; // Computed: whether the chat is archived
  source?: ChatSource;
  group_id?: string | null;
  parent_session_id?: string | null;
  root_session_id?: string | null;
  fork_origin?: ChatForkOrigin | null;
}

export interface Message {
  id?: string;
  source_message_id?: string | null;
  role: string;
  content: unknown;
  [key: string]: unknown;
}

export interface ChatHistory {
  messages: Message[];
  status?: ChatStatus; // Conversation status: idle or running
}

export interface ChatUpdateRequest {
  name?: string;
  pinned?: boolean;
  group_id?: string;
}

export interface ChatDeleteResponse {
  success: boolean;
  chat_id: string;
}

export interface BatchArchiveResult {
  succeeded: string[];
  failed: Array<{
    chat_id: string;
    reason: "not_found" | "in_progress";
    message: string;
  }>;
}

// Legacy Session type alias for backward compatibility
export type Session = ChatSpec;
