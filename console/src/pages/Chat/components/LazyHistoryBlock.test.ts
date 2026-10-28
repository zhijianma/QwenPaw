import { describe, expect, it } from "vitest";
import {
  appendBoundedFragment,
  getTextContent,
  MAX_FRAGMENT_WINDOW_BYTES,
} from "../lazyHistoryBlockModel";

describe("lazy history block window", () => {
  it("appends continuous fragments while the window remains bounded", () => {
    const first = appendBoundedFragment(null, new Uint8Array([1, 2]), 0, 2);
    const second = appendBoundedFragment(first, new Uint8Array([3, 4]), 2, 4);

    expect([...second.bytes]).toEqual([1, 2, 3, 4]);
    expect(second).toMatchObject({ start: 0, end: 4 });
  });

  it("rolls the window instead of retaining an oversized payload", () => {
    const first = {
      bytes: new Uint8Array(MAX_FRAGMENT_WINDOW_BYTES),
      start: 0,
      end: MAX_FRAGMENT_WINDOW_BYTES,
    };
    const nextBytes = new Uint8Array([7, 8]);
    const next = appendBoundedFragment(
      first,
      nextBytes,
      first.end,
      first.end + nextBytes.byteLength,
    );

    expect(next.bytes).toBe(nextBytes);
    expect(next.start).toBe(first.end);
    expect(next.bytes.byteLength).toBeLessThanOrEqual(
      MAX_FRAGMENT_WINDOW_BYTES,
    );
  });

  it("extracts markdown only from a complete text content object", () => {
    expect(getTextContent({ type: "text", text: "hello" })).toBe("hello");
    expect(getTextContent({ type: "tool", text: "hello" })).toBeNull();
  });
});
