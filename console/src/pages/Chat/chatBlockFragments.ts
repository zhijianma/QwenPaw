import type {
  ChatBlockFragment,
  ChatBlockItem,
  ChatHistoryPage,
  ChatViewIdentity,
} from "../../api/types/chat";

export interface LoadedChatBlockFragment {
  bytes: Uint8Array;
  startByte: number;
  endByte: number;
  totalBytes: number;
  nextCursor: string | null;
  contentSha256: string | null;
}

function decodeBase64(value: string): Uint8Array {
  let decoded: string;
  try {
    decoded = atob(value);
  } catch {
    throw new Error("Chat block fragment is not valid base64");
  }
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

export async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function sameView(
  actual: ChatViewIdentity,
  expected: ChatViewIdentity,
): boolean {
  return (
    actual.conversation_id === expected.conversation_id &&
    actual.head_turn_id === expected.head_turn_id &&
    actual.view_version === expected.view_version
  );
}

/** Validate an untrusted fragment response before exposing any payload bytes. */
export async function validateChatBlockFragment(
  page: ChatHistoryPage<ChatBlockFragment>,
  block: ChatBlockItem,
  view: ChatViewIdentity,
  expectedStart: number,
): Promise<LoadedChatBlockFragment> {
  if (!sameView(page.view, view)) {
    throw new Error("Chat history view changed while loading content");
  }
  if (page.items.length !== 1) {
    throw new Error("Chat block fragment response is incomplete");
  }

  const fragment = page.items[0];
  if (
    fragment.block_id !== block.block_id ||
    fragment.entity_version !== block.entity_version ||
    fragment.total_bytes !== block.total_bytes
  ) {
    throw new Error("Chat block changed while loading content");
  }
  if (
    fragment.start_byte !== expectedStart ||
    fragment.end_byte < fragment.start_byte ||
    fragment.end_byte > fragment.total_bytes
  ) {
    throw new Error("Chat block fragment offsets are not continuous");
  }

  const bytes = decodeBase64(fragment.payload_base64);
  if (bytes.byteLength !== fragment.end_byte - fragment.start_byte) {
    throw new Error("Chat block fragment byte length does not match offsets");
  }
  if ((await sha256Hex(bytes)) !== fragment.fragment_sha256) {
    throw new Error("Chat block fragment integrity check failed");
  }
  if (
    fragment.content_sha256 !== null &&
    block.content_sha256 !== null &&
    fragment.content_sha256 !== block.content_sha256
  ) {
    throw new Error("Chat block content identity changed");
  }
  const complete = fragment.end_byte === fragment.total_bytes;
  if (complete !== (page.newer_cursor === null)) {
    throw new Error("Chat block fragment cursor is inconsistent");
  }

  return {
    bytes,
    startByte: fragment.start_byte,
    endByte: fragment.end_byte,
    totalBytes: fragment.total_bytes,
    nextCursor: page.newer_cursor,
    contentSha256: fragment.content_sha256,
  };
}
