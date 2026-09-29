import { chatApi } from "../../api/modules/chat";
import type { ChatSubmissionRequest, ControlReceipt } from "../../api/types";

export interface DurableSubmissionAcceptance {
  receipt: ControlReceipt;
}

export interface AllocatedDurableChat {
  chatId: string;
  sessionId: string;
}

export type DurableAdmission = "active" | "queued" | "terminal";
export type ComposerAdmissionOwner =
  | "allocate"
  | "direct"
  | "legacy-queue"
  | "reject"
  | "server-queue";

const STANDARD_REQUEST_FIELDS = new Set([
  "input",
  "session_id",
  "user_id",
  "channel",
  "stream",
  "request_context",
  "model_slot_override",
]);

interface DurableComposerRequestOptions {
  contentParts: Array<Record<string, unknown>>;
  requestContext: Record<string, unknown>;
  messageMetadata: Record<string, unknown>;
  sessionId?: string;
  userId?: string;
  channel?: string;
}

function asObject(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  return value as Record<string, unknown>;
}

export async function allocateDurableChat(options: {
  name: string;
  createSession: (name: string) => Promise<unknown>;
  activateSession: (chatId: string) => void;
}): Promise<AllocatedDurableChat> {
  const created = asObject(await options.createSession(options.name));
  const session = asObject(created?.session);
  const chatId = typeof session?.id === "string" ? session.id.trim() : "";
  if (!chatId) {
    throw new Error("Durable Chat allocation returned no ChatSpec identity");
  }
  const sessionId =
    typeof session?.sessionId === "string" && session.sessionId.trim()
      ? session.sessionId
      : chatId;
  options.activateSession(chatId);
  return { chatId, sessionId };
}

export function resolveComposerAdmissionOwner(options: {
  usesQwenPawBackend: boolean;
  hasStableChat: boolean;
  sameVisit: boolean;
  requiresQueue: boolean;
}): ComposerAdmissionOwner {
  if (options.usesQwenPawBackend) {
    if (options.hasStableChat) return "server-queue";
    return options.sameVisit ? "allocate" : "reject";
  }
  return options.requiresQueue ? "legacy-queue" : "direct";
}

function latestUserInput(
  requestBody: Record<string, unknown>,
): Record<string, unknown> {
  const input = Array.isArray(requestBody.input) ? requestBody.input : [];
  for (let index = input.length - 1; index >= 0; index -= 1) {
    const message = asObject(input[index]);
    if (message?.role === "user") return message;
  }
  throw new Error("Durable submission requires a user input message");
}

function normalizeContentParts(message: Record<string, unknown>) {
  if (typeof message.content === "string" && message.content) {
    return [{ type: "text", text: message.content }];
  }
  if (!Array.isArray(message.content)) {
    throw new Error("Durable submission requires message content");
  }
  const parts = message.content.map(asObject).filter(Boolean) as Array<
    Record<string, unknown>
  >;
  if (parts.length === 0) {
    throw new Error("Durable submission requires message content");
  }
  return parts;
}

export function buildDurableSubmission(
  requestBody: Record<string, unknown>,
  idempotencyKey: string,
  expectedRevision?: number,
): ChatSubmissionRequest {
  const message = latestUserInput(requestBody);
  const modelOverride = requestBody.model_slot_override;
  const validModelOverride =
    typeof modelOverride === "string" || asObject(modelOverride)
      ? (modelOverride as string | Record<string, unknown>)
      : undefined;
  const requestExtensions = Object.fromEntries(
    Object.entries(requestBody).filter(
      ([field]) => !STANDARD_REQUEST_FIELDS.has(field),
    ),
  );
  return {
    idempotency_key: idempotencyKey,
    ...(expectedRevision !== undefined
      ? { expected_revision: expectedRevision }
      : {}),
    content_parts: normalizeContentParts(message),
    request_context: asObject(requestBody.request_context) ?? {},
    message_metadata: asObject(message.metadata) ?? {},
    ...(validModelOverride !== undefined
      ? { model_slot_override: validModelOverride }
      : {}),
    ...(Object.keys(requestExtensions).length > 0
      ? { request_extensions: requestExtensions }
      : {}),
  };
}

export function buildDurableComposerRequest(
  options: DurableComposerRequestOptions,
): Record<string, unknown> {
  if (options.contentParts.length === 0) {
    throw new Error("Durable submission requires message content");
  }
  return {
    input: [
      {
        role: "user",
        metadata: options.messageMetadata,
        content: options.contentParts,
      },
    ],
    ...(options.sessionId ? { session_id: options.sessionId } : {}),
    ...(options.userId ? { user_id: options.userId } : {}),
    ...(options.channel ? { channel: options.channel } : {}),
    stream: true,
    request_context: options.requestContext,
  };
}

export async function submitDurableChatRequest(options: {
  chatId: string;
  agentId: string;
  idempotencyKey: string;
  requestBody: Record<string, unknown>;
}): Promise<DurableSubmissionAcceptance> {
  const receipt = await chatApi.submitTurn(
    options.chatId,
    buildDurableSubmission(options.requestBody, options.idempotencyKey),
    options.agentId,
  );
  return { receipt };
}

function wait(delay: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const timer = setTimeout(finish, delay);
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(new DOMException("The operation was aborted", "AbortError"));
    };
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
  });
}

export async function waitForDurableAdmission(options: {
  chatId: string;
  agentId: string;
  submissionId: string;
  signal?: AbortSignal;
  timeoutMs?: number;
  pollIntervalMs?: number;
}): Promise<DurableAdmission> {
  const deadline = Date.now() + (options.timeoutMs ?? 5000);
  while (true) {
    const projection = await chatApi.getRuntime(options.chatId, {
      agentId: options.agentId,
      signal: options.signal,
    });
    const submission = projection.queue.submissions.find(
      (item) => item.submission_id === options.submissionId,
    );
    if (!submission) return "terminal";
    if (["admitted", "running", "interrupting"].includes(submission.status)) {
      return "active";
    }
    if (Date.now() >= deadline) return "queued";
    await wait(options.pollIntervalMs ?? 50, options.signal);
  }
}
