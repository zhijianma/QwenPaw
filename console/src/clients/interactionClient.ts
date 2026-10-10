import type {
  ChatInteraction,
  ChatInteractionDecisionRequest,
  ChatInteractionResolution,
} from "../contracts/interactions";

export interface InteractionClientTransport {
  request<T>(path: string, init?: RequestInit): Promise<T>;
}

export function createInteractionClient(transport: InteractionClientTransport) {
  return {
    list: (chatId: string, signal?: AbortSignal) => {
      const path = `/chats/${encodeURIComponent(chatId)}/interactions`;
      return signal
        ? transport.request<ChatInteraction[]>(path, { signal })
        : transport.request<ChatInteraction[]>(path);
    },
    respond: (
      chatId: string,
      interactionId: string,
      body: ChatInteractionDecisionRequest,
    ) =>
      transport.request<ChatInteractionResolution>(
        `/chats/${encodeURIComponent(chatId)}/interactions/` +
          `${encodeURIComponent(interactionId)}/response`,
        { method: "POST", body: JSON.stringify(body) },
      ),
  };
}

export type InteractionClient = ReturnType<typeof createInteractionClient>;
