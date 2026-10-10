import type { ChatSubmissionRequest } from "../contracts/chatControls.js";

export interface ExternalChatSource {
  type: string;
  id?: string;
}

export interface ExternalChatInput {
  kind: "external";
  text: string;
  source: ExternalChatSource;
}

export type ChatInput = string | ExternalChatInput;

export interface ChatSubmissionOptions {
  idempotencyKey: string;
  expectedRevision?: number | null;
  requestContext?: Record<string, unknown>;
  messageMetadata?: Record<string, unknown>;
  modelSlotOverride?: string | Record<string, unknown> | null;
  requestExtensions?: Record<string, unknown>;
  priority?: number;
}

function required(value: string, name: string): string {
  if (!value.trim()) throw new TypeError(`${name} must not be empty`);
  return value;
}

/** Build the shared Host contract without introducing another wire model. */
export function buildChatSubmission(
  input: ChatInput,
  options: ChatSubmissionOptions,
): ChatSubmissionRequest {
  const request: ChatSubmissionRequest = {
    idempotency_key: required(options.idempotencyKey, "idempotencyKey"),
    content_parts: [
      {
        type: "text",
        text:
          typeof input === "string"
            ? required(input, "input")
            : required(input.text, "input.text"),
      },
    ],
  };
  if (typeof input !== "string") {
    request.input_trust = "external";
    request.external_source = {
      source_type: required(input.source.type, "input.source.type"),
      ...(input.source.id === undefined
        ? {}
        : { source_id: required(input.source.id, "input.source.id") }),
    };
  }
  if (options.expectedRevision !== undefined) {
    request.expected_revision = options.expectedRevision;
  }
  if (options.requestContext !== undefined) {
    request.request_context = options.requestContext;
  }
  if (options.messageMetadata !== undefined) {
    request.message_metadata = options.messageMetadata;
  }
  if (options.modelSlotOverride !== undefined) {
    request.model_slot_override = options.modelSlotOverride;
  }
  if (options.requestExtensions !== undefined) {
    request.request_extensions = options.requestExtensions;
  }
  if (options.priority !== undefined) request.priority = options.priority;
  return request;
}
