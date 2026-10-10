import type { QueueProjection } from "./chatControls.js";
import type { ChatInteraction } from "./interactions.js";
import type { ActionRecord as GeneratedActionRecord } from "../generated/actionRecord.js";
import type { ConversationArtifactRecord as GeneratedConversationArtifactRecord } from "../generated/conversationArtifactRecord.js";
import type { ObservationPage as GeneratedObservationPage } from "../generated/observationPage.js";
import type { ConversationTrajectoryPage as GeneratedConversationTrajectoryPage } from "../generated/conversationTrajectoryPage.js";

export type ActionRecord = GeneratedActionRecord;
export type ConversationArtifactRecord = GeneratedConversationArtifactRecord;
export type ObservationPage = GeneratedObservationPage;
export type ConversationTrajectoryPage = GeneratedConversationTrajectoryPage;

export type ChatStatus = "idle" | "running";
export type ChatSource = "chat" | "cron" | "subagent";

export interface ChatForkOrigin {
  parent_chat_id: string;
  source_message_id: string;
}

export interface ChatSpec {
  id: string;
  session_id: string;
  user_id: string;
  channel: string;
  name?: string;
  created_at: string | null;
  updated_at: string | null;
  last_finished_at?: string | null;
  meta?: Record<string, unknown>;
  status?: ChatStatus;
  pinned?: boolean;
  archived_at?: string | null;
  archived?: boolean;
  source?: ChatSource;
  group_id?: string | null;
  parent_session_id?: string | null;
  root_session_id?: string | null;
  fork_origin?: ChatForkOrigin | null;
}

export interface ChatMessage {
  id?: string;
  source_message_id?: string | null;
  role: string;
  content: unknown;
  [key: string]: unknown;
}

export interface ChatHistory {
  messages: ChatMessage[];
  status?: ChatStatus;
}

export interface ChatForkRequest {
  source_message_id: string;
  idempotency_key: string;
  name?: string;
}

export type RuntimeObservationCategory =
  | "model"
  | "action"
  | "control"
  | "guardrail"
  | "compaction"
  | "hitl"
  | "interrupt"
  | "verification"
  | "artifact"
  | "evidence"
  | "outcome";

export interface RuntimeObservation {
  schema?: "qwenpaw.runtime-observation.v1";
  observation_id: string;
  category: RuntimeObservationCategory;
  stage: "intent" | "policy" | "execution" | "evidence";
  status: string;
  source: { source_type: string; source_id: string };
  title: string;
  facts: Record<string, unknown>;
  occurred_at: string;
  [key: string]: unknown;
}

export type ConversationOutcomeStatus =
  | "achieved"
  | "partial"
  | "not_achieved"
  | "abandoned";

export interface ConversationOutcome {
  outcome_id: string;
  agent_id: string;
  chat_id: string;
  correlation_id: string;
  status: ConversationOutcomeStatus;
  producer_id: string;
  summary: string;
  invocation_id?: string | null;
  registry_generation?: number | null;
  task_id?: string | null;
  run_id?: string | null;
  artifact_ids: string[];
  evidence_ids: string[];
  verification_ids: string[];
  supersedes_outcome_id?: string | null;
  created_at: string;
}

export type ConversationExecutionState =
  | "queued"
  | "running"
  | "waiting_user"
  | "inactive"
  | "failed"
  | "interrupted"
  | "cancelled"
  | "achieved"
  | "partial"
  | "not_achieved"
  | "abandoned";

export interface ConversationExecutionChain {
  schema?: "qwenpaw.conversation-execution-chain.v1";
  chat_id: string;
  /** @deprecated Use chat_id. */
  conversation_id?: string;
  correlation_id: string;
  state: ConversationExecutionState;
  submission_ids: string[];
  invocation_ids: string[];
  head_submission_id: string;
  head_invocation_id?: string | null;
  latest_submission_status: QueueProjection["submissions"][number]["status"];
  open_interaction_ids: string[];
  outcome?: ConversationOutcome | null;
  accepted_at: string;
  latest_submission_at: string;
}

export interface CommunicationCapability {
  schema?: "qwenpaw.communication-capability.v1";
  capability_id: string;
  delivery_mode:
    | "request_response"
    | "request_stream"
    | "durable_handle"
    | "durable_channel";
  ordering_scope:
    | "none"
    | "connection"
    | "conversation"
    | "correlation"
    | "global";
  idempotency: "not_applicable" | "optional" | "required";
  cursor_semantics: "none" | "snapshot_change" | "replay_offset";
  retention: "ephemeral" | "latest_state" | "durable";
  backpressure: "none" | "coalesce_latest" | "server_queue" | "acknowledged";
  disconnect_policy:
    | "cancel"
    | "continue"
    | "reconnect_snapshot"
    | "resume_cursor";
}

export interface CommunicationContract {
  schema?: "qwenpaw.communication-contract.v1";
  contract_id: string;
  capabilities: CommunicationCapability[];
}

export interface ConversationRuntimeProjection {
  schema?: string;
  agent_id: string;
  chat_id: string;
  /** @deprecated Use chat_id. */
  conversation_id?: string;
  queue: QueueProjection;
  interactions: ChatInteraction[];
  execution_chains?: ConversationExecutionChain[];
  execution_window_truncated?: boolean;
  activity?: {
    schema?: "qwenpaw.observation-page.v1";
    items: RuntimeObservation[];
    next_cursor?: string | null;
  };
  communication_contract?: CommunicationContract;
  cursor: string;
  observed_at: string;
}
