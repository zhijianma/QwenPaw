import { hostFetch } from "../hostSdk/fetch";
import type {
  PawInteraction,
  PawInteractionResolution,
  PawInteractionResponseRequest,
  PawInteractionsNamespace,
} from "./types";

interface PendingResponse {
  readonly signature: string;
  readonly idempotencyKey: string;
  promise?: Promise<PawInteractionResolution>;
}

export class PawInteractionError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "PawInteractionError";
    this.code = code;
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>;
  const text = await response.text().catch(() => "");
  throw new PawInteractionError(
    `HTTP_${response.status}`,
    text || response.statusText || "Interaction request failed",
  );
}

function requiredChatId(
  explicit: string | undefined,
  provider: () => string | null,
): string {
  const chatId = explicit ?? provider();
  if (!chatId?.trim()) {
    throw new PawInteractionError(
      "CHAT_ID_REQUIRED",
      "A ChatSpec.id is required for Interaction operations",
    );
  }
  return chatId;
}

function responseSignature(request: PawInteractionResponseRequest): string {
  return JSON.stringify({
    expectedRevision: request.expectedRevision,
    selectedOptionIds: request.selectedOptionIds ?? [],
    text: request.text ?? "",
    values: request.values ?? {},
  });
}

export function createInteractionsNamespace(
  currentChatId: () => string | null,
): PawInteractionsNamespace {
  const pending = new Map<string, PendingResponse>();

  return {
    async list(options = {}) {
      const chatId = requiredChatId(options.chatId, currentChatId);
      return readJson<PawInteraction[]>(
        await hostFetch(`/chats/${encodeURIComponent(chatId)}/interactions`, {
          signal: options.signal,
        }),
      );
    },
    respond(request) {
      const chatId = requiredChatId(request.chatId, currentChatId);
      const commandKey = [
        chatId,
        request.interactionId,
        request.expectedRevision,
      ].join(":");
      const signature = responseSignature(request);
      let command = pending.get(commandKey);
      if (command && command.signature !== signature) {
        return Promise.reject(
          new PawInteractionError(
            "INTERACTION_COMMAND_CONFLICT",
            "The same Interaction revision already has another response",
          ),
        );
      }
      if (!command) {
        command = {
          signature,
          idempotencyKey: request.idempotencyKey ?? crypto.randomUUID(),
        };
        pending.set(commandKey, command);
      }
      if (!command.promise) {
        const body = {
          idempotency_key: command.idempotencyKey,
          expected_revision: request.expectedRevision,
          selected_option_ids: request.selectedOptionIds ?? [],
          text: request.text ?? "",
          values: request.values ?? {},
        };
        command.promise = hostFetch(
          `/chats/${encodeURIComponent(chatId)}/interactions/` +
            `${encodeURIComponent(request.interactionId)}/response`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
        )
          .then((response) => readJson<PawInteractionResolution>(response))
          .catch((error: unknown) => {
            command!.promise = undefined;
            throw error;
          });
      }
      return command.promise;
    },
  };
}
