export type InteractionKind = "approval" | "user_input" | "suggestion";
export type InteractionMode = "blocking" | "non_blocking";
export type InteractionStatus = "open" | "resolved" | "expired" | "cancelled";
export type InteractionContinuationMode =
  | "live_invocation"
  | "checkpoint"
  | "conversation_turn";
export type UserInputReason =
  | "missing_required_fact"
  | "material_preference"
  | "scope_authorization"
  | "high_impact_decision";

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
  chat_id: string;
  /** @deprecated Use chat_id. */
  conversation_id?: string;
  invocation_id: string;
  correlation_id: string;
  task_id?: string | null;
  source_id?: string | null;
  user_input_reason?: UserInputReason | null;
  continuation_mode?: InteractionContinuationMode;
  continuation_checkpoint_id?: string | null;
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
    interaction_id?: string;
    idempotency_key?: string;
    expected_revision?: number;
    actor?: { type: string; id: string };
    selected_option_ids: string[];
    text: string;
    values: Record<string, unknown>;
    responded_at?: string;
  } | null;
  detail: string;
  resolved_at: string;
}
