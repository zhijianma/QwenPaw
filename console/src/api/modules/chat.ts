import { ApiError, request } from "../request";
import { getApiUrl, getApiToken } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type {
  ChatSpec,
  ChatHistory,
  ChatResolution,
  ChatDeleteResponse,
  ChatUpdateRequest,
  ChatGroup,
  BatchArchiveResult,
  ChatBlockFragment,
  ChatBlockItem,
  ChatHistoryPage,
  ChatMessageItem,
  ChatSearchPage,
  ChatTurnItem,
  ChatView,
  Session,
} from "../types";

/** Response from POST /console/upload. url = filename only; agent_id from header. */
export interface ChatUploadResponse {
  url: string;
  file_name: string;
  stored_name?: string;
}

export interface ChatStatusResponse {
  status: "idle" | "running";
}

interface ChatPageOptions {
  headTurnId?: string | null;
  viewVersion?: string | null;
  cursor?: string | null;
  targetTurnId?: string | null;
  limit?: number;
  signal?: AbortSignal;
  agentId?: string;
}

interface ChatLiveOptions {
  cursor?: { run_epoch: string; event_sequence: number } | null;
  signal?: AbortSignal;
  agentId?: string;
}

function historyQuery(options: ChatPageOptions): string {
  const params = new URLSearchParams();
  if (options.headTurnId) params.set("head_turn_id", options.headTurnId);
  if (options.viewVersion) params.set("view_version", options.viewVersion);
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.targetTurnId) params.set("target_turn_id", options.targetTurnId);
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  const query = params.toString();
  return query ? `?${query}` : "";
}

function historyRequestOptions(options: ChatPageOptions): RequestInit {
  return {
    signal: options.signal,
    headers: options.agentId ? { "X-Agent-Id": options.agentId } : undefined,
  };
}

const FILES_PREVIEW = "/files/preview";

export const chatApi = {
  resolveChat: (
    chatId: string,
    options?: { currentAgentId?: string; signal?: AbortSignal },
  ) => {
    const params = new URLSearchParams({ chat_id: chatId });
    if (options?.currentAgentId)
      params.set("current_agent_id", options.currentAgentId);
    return request<ChatResolution>(`/chats/resolve?${params}`, {
      signal: options?.signal,
    });
  },

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
  listChats: (params?: {
    user_id?: string;
    channel?: string;
    archived?: boolean;
    include_app_owned?: boolean;
    agentId?: string;
  }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    if (params?.archived !== undefined)
      searchParams.append("archived", String(params.archived));
    if (params?.include_app_owned !== undefined)
      searchParams.append(
        "include_app_owned",
        String(params.include_app_owned),
      );
    const query = searchParams.toString();
    const path = `/chats${query ? `?${query}` : ""}`;
    return params?.agentId
      ? request<ChatSpec[]>(path, {
          headers: { "X-Agent-Id": params.agentId },
        })
      : request<ChatSpec[]>(path);
  },

  createChat: (chat: Partial<ChatSpec>) =>
    request<ChatSpec>("/chats", {
      method: "POST",
      body: JSON.stringify(chat),
    }),

  getChat: (
    chatId: string,
    options?: { signal?: AbortSignal; include_app_owned?: boolean },
  ) => {
    const searchParams = new URLSearchParams();
    if (options?.include_app_owned !== undefined)
      searchParams.append(
        "include_app_owned",
        String(options.include_app_owned),
      );
    const query = searchParams.toString();
    return request<ChatHistory>(
      `/chats/${encodeURIComponent(chatId)}${query ? `?${query}` : ""}`,
      {
        signal: options?.signal,
      },
    );
  },

  getChatView: (
    chatId: string,
    options?: { signal?: AbortSignal; agentId?: string },
  ) =>
    request<ChatView>(`/chats/${encodeURIComponent(chatId)}/view`, {
      signal: options?.signal,
      headers: options?.agentId ? { "X-Agent-Id": options.agentId } : undefined,
    }),

  createChatCopy: (chatId: string, sourceTurnId: string, agentId?: string) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}/copies`, {
      method: "POST",
      headers: agentId ? { "X-Agent-Id": agentId } : undefined,
      body: JSON.stringify({
        source_turn_id: sourceTurnId,
      }),
    }),

  openChatLive: async (chatId: string, options: ChatLiveOptions) => {
    const params = new URLSearchParams();
    if (options.cursor) {
      params.set("run_epoch", options.cursor.run_epoch);
      params.set("event_sequence", String(options.cursor.event_sequence));
    }
    const query = params.toString();
    const path = `/chats/${encodeURIComponent(chatId)}/live${
      query ? `?${query}` : ""
    }`;
    const headers = {
      ...buildAuthHeaders(),
      ...(options.agentId ? { "X-Agent-Id": options.agentId } : {}),
    };
    const response = await fetch(getApiUrl(path), {
      method: "GET",
      headers,
      signal: options.signal,
    });
    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      throw new ApiError(
        response.status,
        detail || `Chat live request failed: ${response.status}`,
      );
    }
    return response;
  },

  getTurnPage: (chatId: string, options: ChatPageOptions) =>
    request<ChatHistoryPage<ChatTurnItem>>(
      `/chats/${encodeURIComponent(chatId)}/turns${historyQuery(options)}`,
      historyRequestOptions(options),
    ),

  searchChats: (
    query: string,
    options?: {
      beforeSeq?: number | null;
      limit?: number;
      signal?: AbortSignal;
      agentId?: string;
    },
  ) => {
    const params = new URLSearchParams({ query });
    if (options?.beforeSeq !== undefined && options.beforeSeq !== null) {
      params.set("before_seq", String(options.beforeSeq));
    }
    if (options?.limit !== undefined) {
      params.set("limit", String(options.limit));
    }
    return request<ChatSearchPage>(`/chats/search?${params}`, {
      signal: options?.signal,
      headers: options?.agentId ? { "X-Agent-Id": options.agentId } : undefined,
    });
  },

  getMessagePage: (chatId: string, turnId: string, options: ChatPageOptions) =>
    request<ChatHistoryPage<ChatMessageItem>>(
      `/chats/${encodeURIComponent(chatId)}/turns/${encodeURIComponent(
        turnId,
      )}/messages${historyQuery(options)}`,
      historyRequestOptions(options),
    ),

  getBlockPage: (chatId: string, messageId: string, options: ChatPageOptions) =>
    request<ChatHistoryPage<ChatBlockItem>>(
      `/chats/${encodeURIComponent(chatId)}/messages/${encodeURIComponent(
        messageId,
      )}/blocks${historyQuery(options)}`,
      historyRequestOptions(options),
    ),

  getBlockFragment: (
    chatId: string,
    blockId: string,
    options: ChatPageOptions,
  ) =>
    request<ChatHistoryPage<ChatBlockFragment>>(
      `/chats/${encodeURIComponent(chatId)}/blocks/${encodeURIComponent(
        blockId,
      )}/content${historyQuery(options)}`,
      historyRequestOptions(options),
    ),

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

  archiveChat: (chatId: string, agentId?: string) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}/archive`, {
      method: "POST",
      headers: agentId ? { "X-Agent-Id": agentId } : undefined,
    }),

  unarchiveChat: (chatId: string, agentId?: string) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}/unarchive`, {
      method: "POST",
      headers: agentId ? { "X-Agent-Id": agentId } : undefined,
    }),

  batchArchiveChats: (chatIds: string[], agentId?: string) =>
    request<BatchArchiveResult>("/chats/actions/batch-archive", {
      method: "POST",
      body: JSON.stringify({ chat_ids: chatIds }),
      headers: agentId ? { "X-Agent-Id": agentId } : undefined,
    }),

  batchUnarchiveChats: (chatIds: string[], agentId?: string) =>
    request<BatchArchiveResult>("/chats/actions/batch-unarchive", {
      method: "POST",
      body: JSON.stringify({ chat_ids: chatIds }),
      headers: agentId ? { "X-Agent-Id": agentId } : undefined,
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

  deleteSession: (sessionId: string, agentId?: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
      headers: agentId ? { "X-Agent-Id": agentId } : undefined,
    }),

  createSession: (session: Partial<Session>) =>
    request<Session>("/chats", {
      method: "POST",
      body: JSON.stringify(session),
    }),

  updateSession: (
    sessionId: string,
    session: ChatUpdateRequest,
    agentId?: string,
  ) =>
    request<Session>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "PUT",
      body: JSON.stringify(session),
      headers: agentId ? { "X-Agent-Id": agentId } : undefined,
    }),

  batchDeleteSessions: (sessionIds: string[], agentId?: string) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(sessionIds),
        headers: agentId ? { "X-Agent-Id": agentId } : undefined,
      },
    ),
};
