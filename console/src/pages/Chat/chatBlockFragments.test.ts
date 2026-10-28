import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ChatBlockFragment,
  ChatBlockItem,
  ChatHistoryPage,
  ChatViewIdentity,
} from "../../api/types/chat";
import { sha256Hex, validateChatBlockFragment } from "./chatBlockFragments";

const view: ChatViewIdentity = {
  conversation_id: "chat-1",
  head_turn_id: "turn-1",
  view_version: "view-1",
};

const block: ChatBlockItem = {
  block_id: "block-1",
  message_id: "message-1",
  kind: "text",
  ordinal: 0,
  state: "complete",
  total_bytes: 5,
  content_sha256: null,
  entity_version: 3,
  metadata: {},
  metadata_complete: true,
  created_at_us: 1,
  updated_at_us: 1,
  content: null,
  content_loaded: false,
  content_cursor: "first",
};

async function page(
  overrides: Partial<ChatBlockFragment> = {},
): Promise<ChatHistoryPage<ChatBlockFragment>> {
  const bytes = new TextEncoder().encode("hello");
  return {
    items: [
      {
        block_id: block.block_id,
        entity_version: block.entity_version,
        start_byte: 0,
        end_byte: bytes.byteLength,
        total_bytes: bytes.byteLength,
        payload_base64: btoa(String.fromCharCode(...bytes)),
        payload_encoding: "base64",
        fragment_sha256: await sha256Hex(bytes),
        content_sha256: null,
        ...overrides,
      },
    ],
    older_cursor: null,
    newer_cursor: null,
    view,
    bytes: 128,
    truncated_entity: false,
  };
}

describe("chat block fragment validation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("accepts one continuous fragment with a valid digest", async () => {
    const result = await validateChatBlockFragment(
      await page(),
      block,
      view,
      0,
    );

    expect(new TextDecoder().decode(result.bytes)).toBe("hello");
    expect(result.endByte).toBe(5);
    expect(result.nextCursor).toBeNull();
  });

  it("rejects a response from another stable view", async () => {
    const response = await page();
    response.view = { ...view, view_version: "view-2" };

    await expect(
      validateChatBlockFragment(response, block, view, 0),
    ).rejects.toThrow("view changed");
  });

  it("rejects gaps before decoding the next fragment", async () => {
    await expect(
      validateChatBlockFragment(
        await page({ start_byte: 1, end_byte: 6 }),
        block,
        view,
        0,
      ),
    ).rejects.toThrow("offsets are not continuous");
  });

  it("rejects payload bytes that do not match the signed digest", async () => {
    await expect(
      validateChatBlockFragment(
        await page({ fragment_sha256: "0".repeat(64) }),
        block,
        view,
        0,
      ),
    ).rejects.toThrow("integrity check failed");
  });
});
