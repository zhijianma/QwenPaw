export const HISTORY_MARKDOWN_CHUNK_CHARS = 32 * 1024;

/** Split large persisted Markdown near paragraph boundaries without loss. */
export function splitHistoryMarkdown(
  content: string,
  limit = HISTORY_MARKDOWN_CHUNK_CHARS,
): string[] {
  if (content.length <= limit) return [content];
  const chunks: string[] = [];
  let end = content.length;
  while (end > 0) {
    const desiredStart = Math.max(0, end - limit);
    let start = desiredStart;
    if (desiredStart > 0) {
      const paragraph = content.indexOf("\n\n", desiredStart);
      if (paragraph >= 0 && paragraph + 2 < end) start = paragraph + 2;
    }
    chunks.unshift(content.slice(start, end));
    end = start;
  }
  return chunks;
}

export function latestHistoryMarkdownChunk(
  content: string,
  limit = HISTORY_MARKDOWN_CHUNK_CHARS,
): string {
  const chunks = splitHistoryMarkdown(content, limit);
  return chunks[chunks.length - 1] ?? "";
}
