/** Stable frontend names backed by generated Kernel response contracts. */
import type {
  ContinuationMode,
  InteractionKind,
  InteractionMode,
  InteractionOption,
  InteractionRequest,
  InteractionStatus,
  UserInputReason,
} from "./generated/interactionRequest";
import type { InteractionResolution } from "./generated/interactionResolution";

export type {
  InteractionKind,
  InteractionMode,
  InteractionOption,
  InteractionStatus,
  UserInputReason,
};
export type InteractionContinuationMode = ContinuationMode;

export type ChatInteraction = InteractionRequest & {
  /** @deprecated Use chat_id. */
  conversation_id?: string;
};

export interface ChatInteractionDecisionRequest {
  idempotency_key: string;
  expected_revision: number;
  selected_option_ids?: string[];
  text?: string;
  values?: Record<string, unknown>;
}

export type ChatInteractionResolution = InteractionResolution;
