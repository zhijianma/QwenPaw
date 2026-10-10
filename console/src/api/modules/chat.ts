import { request } from "../request";
import { createChatControlClient } from "../../clients/chatControlClient";
import { createChatClient } from "../../clients/chatClient";
import { createInteractionClient } from "../../clients/interactionClient";
import { getApiUrl, getApiToken } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type {
  ArtifactRef,
  ChatSpec,
  ChatHistory,
  ChatDeleteResponse,
  ChatUpdateRequest,
  ChatGroup,
  ChatForkRequest,
  ChatControlRequest,
  ChatQueueReorderRequest,
  ChatSteerRequest,
  ChatSubmissionRequest,
  ControlReceipt,
  ConversationRuntimeProjection,
  QueueProjection,
  BatchArchiveResult,
  Session,
  EvidenceRef,
  ExternalQueueFallbackRequest,
  ExternalQueueFallbackReceipt,
} from "../types";

/** Response from POST /console/upload. url = filename only; agent_id from header. */
export interface ChatUploadResponse {
  url: string;
  file_name: string;
  size: number;
  stored_name?: string;
  artifact_ref: ArtifactRef;
  evidence_ref: EvidenceRef;
  artifact_receipt: string;
}

export interface ChatStatusResponse {
  status: "idle" | "running";
}

const FILES_PREVIEW = "/files/preview";
const chatControlClient = createChatControlClient({ request });
const chatClient = createChatClient({
  request,
  openStream: (path, init) =>
    fetch(getApiUrl(path), {
      ...init,
      headers: {
        ...buildAuthHeaders(),
        ...Object.fromEntries(new Headers(init?.headers).entries()),
      },
    }),
});
const interactionClient = createInteractionClient({ request });

export const chatApi = {
  recordExternalQueueFallback: (
    payload: ExternalQueueFallbackRequest,
    agentId: string,
  ) =>
    request<ExternalQueueFallbackReceipt>(
      "/chats/compatibility/external-queue/hits",
      {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "X-Agent-Id": agentId },
      },
    ),

  /** Upload a file for chat attachment. Returns URL path for content. */
  uploadFile: async (file: File): Promise<ChatUploadResponse> => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(getApiUrl("/console/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(
        `Upload failed: ${response.status} ${response.statusText}${
          text ? ` - ${text}` : ""
        }`,
      );
    }
    return response.json();
  },

  filePreviewUrl: (filename: string): string => {
    if (!filename) return "";
    if (filename.startsWith("http://") || filename.startsWith("https://"))
      return filename;
    const cleaned = filename.replace(/^\/+/, "");
    const path = `${FILES_PREVIEW}/${cleaned}`;
    const url = getApiUrl(path);

    const token = getApiToken();
    if (token) {
      return `${url}?token=${encodeURIComponent(token)}`;
    }

    return url;
  },
  artifactContentUrl: (
    chatId: string,
    artifactId: string,
    disposition: "inline" | "attachment" = "inline",
  ): string =>
    getApiUrl(
      `/chats/${encodeURIComponent(chatId)}/artifacts/${encodeURIComponent(
        artifactId,
      )}/content?disposition=${disposition}`,
    ),
  listChats: (params?: {
    user_id?: string;
    channel?: string;
    archived?: boolean;
    include_app_owned?: boolean;
    agentId?: string;
  }) =>
    chatClient.list({
      userId: params?.user_id,
      channel: params?.channel,
      archived: params?.archived,
      includeAppOwned: params?.include_app_owned,
      agentId: params?.agentId,
    }) as Promise<ChatSpec[]>,

  createChat: (chat: Partial<ChatSpec>) =>
    chatClient.create(chat) as Promise<ChatSpec>,

  forkChat: (parentChatId: string, payload: ChatForkRequest) =>
    chatClient.fork(parentChatId, payload) as Promise<ChatSpec>,

  submitTurn: (
    chatId: string,
    payload: ChatSubmissionRequest,
    agentId?: string,
  ): Promise<ControlReceipt> =>
    chatControlClient.submit(chatId, payload, { agentId }),

  getQueue: (chatId: string, agentId?: string): Promise<QueueProjection> =>
    chatControlClient.queue(chatId, { agentId }),

  getRuntime: (
    chatId: string,
    options?: { signal?: AbortSignal; agentId?: string },
  ) =>
    chatClient.runtime(
      chatId,
      options,
    ) as Promise<ConversationRuntimeProjection>,

  steer: (
    chatId: string,
    payload: ChatSteerRequest,
    agentId?: string,
  ): Promise<ControlReceipt> =>
    chatControlClient.steer(chatId, payload, { agentId }),

  interrupt: (
    chatId: string,
    payload: ChatControlRequest,
    agentId?: string,
  ): Promise<ControlReceipt> =>
    chatControlClient.interrupt(chatId, payload, { agentId }),

  stopAndClear: (
    chatId: string,
    payload: ChatControlRequest,
    agentId?: string,
  ): Promise<ControlReceipt> =>
    chatControlClient.stopAndClear(chatId, payload, { agentId }),

  cancelQueued: (
    chatId: string,
    submissionId: string,
    payload: ChatControlRequest,
    agentId?: string,
  ): Promise<ControlReceipt> =>
    chatControlClient.cancelQueued(chatId, submissionId, payload, {
      agentId,
    }),

  reorderQueue: (
    chatId: string,
    payload: ChatQueueReorderRequest,
    agentId?: string,
  ): Promise<ControlReceipt> =>
    chatControlClient.reorder(chatId, payload, { agentId }),

  listInteractions: interactionClient.list,

  respondToInteraction: interactionClient.respond,

  getChat: (
    chatId: string,
    options?: {
      signal?: AbortSignal;
      include_app_owned?: boolean;
      agentId?: string;
    },
  ) => {
    return chatClient.history(chatId, {
      signal: options?.signal,
      includeAppOwned: options?.include_app_owned,
      agentId: options?.agentId,
    }) as Promise<ChatHistory>;
  },

  getChatStatus: (
    chatId: string,
    options?: { signal?: AbortSignal; agentId?: string },
  ) => {
    return request<ChatStatusResponse>(
      `/chats/${encodeURIComponent(chatId)}/status`,
      {
        signal: options?.signal,
        headers: options?.agentId
          ? { "X-Agent-Id": options.agentId }
          : undefined,
      },
    );
  },

  updateChat: (chatId: string, chat: ChatUpdateRequest) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "PUT",
      body: JSON.stringify(chat),
    }),

  deleteChat: (chatId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "DELETE",
    }),

  batchDeleteChats: (chatIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(chatIds),
      },
    ),

  archiveChat: (chatId: string) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}/archive`, {
      method: "POST",
    }),

  unarchiveChat: (chatId: string) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}/unarchive`, {
      method: "POST",
    }),

  batchArchiveChats: (chatIds: string[]) =>
    request<BatchArchiveResult>("/chats/actions/batch-archive", {
      method: "POST",
      body: JSON.stringify({ chat_ids: chatIds }),
    }),

  batchUnarchiveChats: (chatIds: string[]) =>
    request<BatchArchiveResult>("/chats/actions/batch-unarchive", {
      method: "POST",
      body: JSON.stringify({ chat_ids: chatIds }),
    }),

  listGroups: () => request<ChatGroup[]>("/chats/groups"),

  createGroup: (name: string) =>
    request<ChatGroup>("/chats/groups", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  updateGroup: (groupId: string, update: { name?: string; pinned?: boolean }) =>
    request<ChatGroup>(`/chats/groups/${encodeURIComponent(groupId)}`, {
      method: "PUT",
      body: JSON.stringify(update),
    }),

  reorderGroups: (groupIds: string[]) =>
    request<ChatGroup[]>("/chats/groups/order", {
      method: "PUT",
      body: JSON.stringify({ group_ids: groupIds }),
    }),

  deleteGroup: (groupId: string) =>
    request<{ success: boolean; group_id: string }>(
      `/chats/groups/${encodeURIComponent(groupId)}`,
      { method: "DELETE" },
    ),

  stopChat: (chatId: string, agentId?: string) =>
    request<void>(`/console/chat/stop?chat_id=${encodeURIComponent(chatId)}`, {
      method: "POST",
      ...(agentId ? { headers: { "X-Agent-Id": agentId } } : {}),
    }),
};

export const sessionApi = {
  listSessions: (params?: { user_id?: string; channel?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    const query = searchParams.toString();
    return request<Session[]>(`/chats${query ? `?${query}` : ""}`);
  },

  getSession: (sessionId: string) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(sessionId)}`),

  deleteSession: (sessionId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),

  createSession: (session: Partial<Session>) =>
    request<Session>("/chats", {
      method: "POST",
      body: JSON.stringify(session),
    }),

  updateSession: (sessionId: string, session: ChatUpdateRequest) =>
    request<Session>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "PUT",
      body: JSON.stringify(session),
    }),

  batchDeleteSessions: (sessionIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(sessionIds),
      },
    ),
};
