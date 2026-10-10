import type { ChatControlRequest as GeneratedChatControlRequest } from "../generated/chatControlRequest.js";
import type { ChatQueueReorderRequest as GeneratedChatQueueReorderRequest } from "../generated/chatQueueReorderRequest.js";
import type { ChatSteerRequest as GeneratedChatSteerRequest } from "../generated/chatSteerRequest.js";
import type { ChatSubmissionRequest as GeneratedChatSubmissionRequest } from "../generated/chatSubmissionRequest.js";
import type {
  ControlCommandKind,
  ControlCommandStatus,
  ControlReceipt as GeneratedControlReceipt,
  SteerSafePoint,
} from "../generated/controlReceipt.js";
import type { ControlRecord as GeneratedControlRecord } from "../generated/controlRecord.js";
import type {
  QueueProjection as GeneratedQueueProjection,
  SubmissionStatus,
  TurnSubmission,
} from "../generated/queueProjection.js";

export type ChatControlRequest = GeneratedChatControlRequest;
export type ChatSteerRequest = GeneratedChatSteerRequest;
export interface ChatQueueReorderRequest
  extends Omit<GeneratedChatQueueReorderRequest, "ordered_submission_ids"> {
  ordered_submission_ids: string[];
}
export interface ChatSubmissionRequest
  extends Omit<GeneratedChatSubmissionRequest, "content_parts"> {
  content_parts: Array<Record<string, unknown>>;
}
export type ControlReceipt = GeneratedControlReceipt;
export type ControlRecord = GeneratedControlRecord;
export type QueueProjection = GeneratedQueueProjection;
export type {
  ControlCommandKind,
  ControlCommandStatus,
  SteerSafePoint,
  SubmissionStatus,
  TurnSubmission,
};
