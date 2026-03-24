import { request } from "../request";
import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import type {
  ChatSpec,
  ChatHistory,
  ChatDeleteResponse,
  Session,
} from "../types";

/** Response from POST /console/upload. url = filename only; agent_id from header. */
export interface ChatUploadResponse {
  url: string;
  file_name: string;
  stored_name?: string;
}

const CONSOLE_FILES_PREFIX = "/console/files";

/** API URL → blob: URL cache (page-session lifetime). */
const _blobUrlCache = new Map<string, string>();
/** blob: URL → stored filename (e.g. "abc123_photo.png") reverse lookup. */
const _blobToStoredName = new Map<string, string>();

function getSelectedAgentId(): string {
  try {
    const agentStorage = localStorage.getItem("copaw-agent-storage");
    if (agentStorage) {
      const parsed = JSON.parse(agentStorage);
      const id = parsed?.state?.selectedAgent;
      if (id) return id;
    }
  } catch {
    // ignore
  }
  return "";
}

export const chatApi = {
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

  /** Build full API URL for a console file. Backend returns filename only; agent_id from header/context (selectedAgent). */
  fileUrl: (filename: string): string => {
    if (!filename) return "";
    if (filename.startsWith("http://") || filename.startsWith("https://"))
      return filename;
    const agentId = getSelectedAgentId() || "default";
    const path = `${CONSOLE_FILES_PREFIX}/${agentId}/${filename.replace(
      /^\/+/,
      "",
    )}`;
    return getApiUrl(path);
  },

  /** Fetch a file URL with auth headers and return a cached blob: URL. Falls back to plain URL on error. */
  fileBlobUrl: async (filename: string): Promise<string> => {
    const apiUrl = chatApi.fileUrl(filename);
    if (!apiUrl) return "";
    const cached = _blobUrlCache.get(apiUrl);
    if (cached) return cached;
    try {
      const res = await fetch(apiUrl, { headers: buildAuthHeaders() });
      if (!res.ok) return apiUrl;
      const blobUrl = URL.createObjectURL(await res.blob());
      _blobUrlCache.set(apiUrl, blobUrl);
      // Extract stored filename from the API path for reverse lookup.
      const storedName =
        apiUrl.match(/\/console\/files\/[^/]+\/(.+)$/)?.[1] ?? filename;
      _blobToStoredName.set(blobUrl, storedName);
      return blobUrl;
    } catch {
      return apiUrl;
    }
  },

  /** Resolve a blob: URL or API URL back to the stored filename sent to the backend. */
  storedNameFromUrl: (url: string): string => {
    if (url.startsWith("blob:")) return _blobToStoredName.get(url) ?? url;
    const m = url.match(/\/console\/files\/[^/]+\/(.+)$/);
    return m ? m[1] : url;
  },
  listChats: (params?: { user_id?: string; channel?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    const query = searchParams.toString();
    return request<ChatSpec[]>(`/chats${query ? `?${query}` : ""}`);
  },

  createChat: (chat: Partial<ChatSpec>) =>
    request<ChatSpec>("/chats", {
      method: "POST",
      body: JSON.stringify(chat),
    }),

  getChat: (chatId: string) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(chatId)}`),

  updateChat: (chatId: string, chat: Partial<ChatSpec>) =>
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

  stopChat: (chatId: string) =>
    request<void>(`/console/chat/stop?chat_id=${encodeURIComponent(chatId)}`, {
      method: "POST",
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

  updateSession: (sessionId: string, session: Partial<Session>) =>
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
