export type ChatStatus = "idle" | "running";
export type ChatSource = "chat" | "cron" | "subagent";
export type ChatGroupKind = "default" | "cron" | "subagents" | "custom";

export interface ChatGroup {
  id: string;
  name: string;
  order: number;
  kind: ChatGroupKind;
  source?: ChatSource | null;
  pinned: boolean;
}

export interface ChatSpec {
  id: string; // Chat UUID identifier
  session_id: string; // Session identifier (channel:user_id format)
  user_id: string; // User identifier
  channel: string; // Channel name, default: "default"
  name?: string; // Chat display name
  created_at: string | null; // Chat creation timestamp (ISO 8601)
  updated_at: string | null; // Chat last update timestamp (ISO 8601)
  last_finished_at?: string | null; // Most recent task completion timestamp
  meta?: Record<string, unknown>; // Additional metadata
  status?: ChatStatus; // Conversation status: idle or running
  pinned?: boolean; // Whether the chat is pinned to the top
  archived_at?: string | null; // When the chat was archived (ISO 8601), null = active
  archived?: boolean; // Computed: whether the chat is archived
  source?: ChatSource;
  group_id?: string | null;
  parent_session_id?: string | null;
  root_session_id?: string | null;
}

export interface Message {
  role: string;
  content: unknown;
  [key: string]: unknown;
}

export interface ChatHistory {
  messages: Message[];
  status?: ChatStatus; // Conversation status: idle or running
}

export interface ChatViewIdentity {
  conversation_id: string;
  head_turn_id: string | null;
  view_version: string;
}

export interface ChatCapabilities {
  turn_pagination: boolean;
  message_pagination: boolean;
  block_pagination: boolean;
  block_fragments: boolean;
  live_resync?: boolean;
  chat_copy?: boolean;
  edit_last_user_message: boolean;
}

export interface ChatView {
  conversation_id: string;
  display_name: string;
  lifecycle: string;
  migration_state: string;
  catalog_generation: number;
  head_turn_id: string | null;
  view_version: string;
  context_revision_id: string;
  active_run: { run_id: string; state: string } | null;
  capabilities: ChatCapabilities;
}

export interface ChatToolLink {
  provider_call_id: string;
  tool_name: string | null;
  state: string;
  call_ref: string;
  result_ref: string | null;
  result_load_state: "loaded" | "not_loaded" | "pending" | "unavailable" | null;
}

export interface ChatBlockItem {
  block_id: string;
  message_id: string;
  kind: string;
  ordinal: number;
  state: string;
  total_bytes: number;
  content_sha256: string | null;
  entity_version: number;
  metadata: unknown;
  metadata_complete: boolean;
  created_at_us: number;
  updated_at_us: number;
  content: unknown;
  content_loaded: boolean;
  content_cursor: string | null;
  preview?: string;
  tool_link?: ChatToolLink;
}

export interface ChatMessageItem {
  message_id: string;
  turn_id: string;
  role: string;
  ordinal: number;
  state: string;
  metadata: Record<string, unknown>;
  metadata_complete: boolean;
  created_at_us: number;
  updated_at_us: number;
  blocks: ChatBlockItem[];
  child_cursor: string | null;
  children_complete: boolean;
}

export interface ChatTurnItem {
  turn_id: string;
  parent_turn_id: string | null;
  state: string;
  lineage_depth: number;
  usage: unknown;
  usage_complete: boolean;
  error_digest: string | null;
  created_at_us: number;
  updated_at_us: number;
  terminal_at_us: number | null;
  messages: ChatMessageItem[];
  child_cursor: string | null;
  children_complete: boolean;
}

export interface ChatHistoryPage<T> {
  items: T[];
  older_cursor: string | null;
  newer_cursor: string | null;
  view: ChatViewIdentity;
  bytes: number;
  truncated_entity: boolean;
}

export interface ChatBlockFragment {
  block_id: string;
  entity_version: number;
  start_byte: number;
  end_byte: number;
  total_bytes: number;
  payload_base64: string;
  payload_encoding: "base64";
  fragment_sha256: string;
  content_sha256: string | null;
}

export interface ChatSearchHit {
  recall_seq: number;
  conversation_id: string;
  turn_id: string;
  message_id: string;
  block_id: string;
  role: string | null;
  snippet: string;
  created_at_us: number;
}

export interface ChatSearchPage {
  items: ChatSearchHit[];
  next_before_seq: number | null;
}

export interface ChatResolution {
  agent_id: string;
  chat: ChatSpec;
}

export interface ChatUpdateRequest {
  name?: string;
  pinned?: boolean;
  group_id?: string;
}

export interface ChatDeleteResponse {
  success: boolean;
  chat_id: string;
}

export interface BatchArchiveResult {
  succeeded: string[];
  failed: Array<{
    chat_id: string;
    reason: "not_found" | "in_progress";
    message: string;
  }>;
}

// Legacy Session type alias for backward compatibility
export type Session = ChatSpec;
