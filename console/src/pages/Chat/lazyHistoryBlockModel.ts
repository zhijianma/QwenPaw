export const MAX_FRAGMENT_WINDOW_BYTES = 1024 * 1024;

export interface FragmentWindow {
  bytes: Uint8Array;
  start: number;
  end: number;
}

export function appendBoundedFragment(
  current: FragmentWindow | null,
  bytes: Uint8Array,
  start: number,
  end: number,
): FragmentWindow {
  if (
    !current ||
    current.end !== start ||
    current.bytes.byteLength + bytes.byteLength > MAX_FRAGMENT_WINDOW_BYTES
  ) {
    return { bytes, start, end };
  }
  const merged = new Uint8Array(current.bytes.byteLength + bytes.byteLength);
  merged.set(current.bytes);
  merged.set(bytes, current.bytes.byteLength);
  return { bytes: merged, start: current.start, end };
}

export function getTextContent(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  return record.type === "text" && typeof record.text === "string"
    ? record.text
    : null;
}
