const MARKDOWN_OR_RICH_TEXT = /[\\`*_{}<>()!|~\r\n&\t\f]/;
const MARKDOWN_LINE_START = /^(?:\s{0,3}[#>+-]\s|\s*\d+[.)]\s)/;

/** Only bypass Markdown when its output is provably equivalent plain text. */
export function isLargePlainText(content: string): boolean {
  return (
    content.length >= 4_096 &&
    !MARKDOWN_OR_RICH_TEXT.test(content) &&
    !MARKDOWN_LINE_START.test(content) &&
    !content.includes("[") &&
    !content.includes("]") &&
    !content.includes("://") &&
    !content.includes("www.") &&
    !content.includes("@")
  );
}
