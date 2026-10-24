import { describe, expect, it } from "vitest";
import {
  latestHistoryMarkdownChunk,
  splitHistoryMarkdown,
} from "./historyMarkdownChunks";

describe("historyMarkdownChunks", () => {
  it("keeps ordinary Markdown in one chunk", () => {
    expect(splitHistoryMarkdown("# Result\n\nComplete", 100)).toEqual([
      "# Result\n\nComplete",
    ]);
  });

  it("splits large Markdown without dropping content", () => {
    const content = Array.from(
      { length: 20 },
      (_, index) => `## Section ${index}\n\n${"x".repeat(20)}\n\n`,
    ).join("");
    const chunks = splitHistoryMarkdown(content, 80);

    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.join("")).toBe(content);
    expect(chunks.every((chunk) => chunk.length <= 80)).toBe(true);
    expect(latestHistoryMarkdownChunk(content, 80)).toBe(
      chunks[chunks.length - 1],
    );
  });

  it("makes progress when the content ends at a paragraph boundary", () => {
    const content = `${"x".repeat(200)}\n\n`;
    const chunks = splitHistoryMarkdown(content, 80);

    expect(chunks.join("")).toBe(content);
    expect(chunks.every((chunk) => chunk.length > 0)).toBe(true);
    expect(chunks.every((chunk) => chunk.length <= 80)).toBe(true);
  });
});
