import { Button } from "antd";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AssistantMarkdown } from "../AssistantMarkdown";
import { sha256Hex } from "../chatBlockFragments";
import {
  appendBoundedFragment,
  type FragmentWindow,
  getTextContent,
} from "../lazyHistoryBlockModel";
import sessionApi from "../sessionApi";
import styles from "./LazyHistoryBlock.module.less";

interface LazyHistoryBlockProps {
  blockId: string;
  preview: string;
  totalBytes: number;
  messageId: string;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function parseCompleteContent(bytes: Uint8Array): unknown {
  const text = new TextDecoder().decode(bytes);
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function LazyHistoryBlock({
  blockId,
  preview,
  totalBytes,
  messageId,
}: LazyHistoryBlockProps) {
  const { t } = useTranslation();
  const [window, setWindow] = useState<FragmentWindow | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [completeContent, setCompleteContent] = useState<unknown>(null);
  const [started, setStarted] = useState(false);
  const [complete, setComplete] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const expectedStart = useRef(0);
  const controller = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  const loadNext = async () => {
    if (loading || complete) return;
    controller.current?.abort();
    const nextController = new AbortController();
    controller.current = nextController;
    setLoading(true);
    setError(null);
    try {
      const fragment = await sessionApi.loadBlockFragment(
        blockId,
        cursor,
        expectedStart.current,
        nextController.signal,
      );
      const nextWindow = appendBoundedFragment(
        window,
        fragment.bytes,
        fragment.startByte,
        fragment.endByte,
      );
      if (
        fragment.nextCursor === null &&
        nextWindow.start === 0 &&
        fragment.contentSha256 &&
        (await sha256Hex(nextWindow.bytes)) !== fragment.contentSha256
      ) {
        throw new Error("Chat block integrity check failed");
      }
      expectedStart.current = fragment.endByte;
      setWindow(nextWindow);
      setCursor(fragment.nextCursor);
      setStarted(true);
      setComplete(fragment.nextCursor === null);
      if (fragment.nextCursor === null && nextWindow.start === 0) {
        setCompleteContent(parseCompleteContent(nextWindow.bytes));
      }
    } catch (loadError) {
      if (
        loadError instanceof DOMException &&
        loadError.name === "AbortError"
      ) {
        return;
      }
      setError(
        loadError instanceof Error
          ? loadError.message
          : "Unable to load this content",
      );
    } finally {
      if (controller.current === nextController) {
        controller.current = null;
        setLoading(false);
      }
    }
  };

  const textContent = getTextContent(completeContent);
  const rawContent = window ? new TextDecoder().decode(window.bytes) : preview;

  return (
    <div className={styles.root} data-chat-lazy-block={blockId}>
      {textContent !== null ? (
        <AssistantMarkdown
          content={textContent}
          cursor={false}
          isStreaming={false}
          messageId={`${messageId}:${blockId}`}
        />
      ) : (
        <pre className={started ? styles.rawWindow : styles.preview}>
          {completeContent === null
            ? rawContent
            : JSON.stringify(completeContent, null, 2)}
        </pre>
      )}
      <div className={styles.actions}>
        {!complete && (
          <Button
            size="small"
            loading={loading}
            onClick={() => void loadNext()}
          >
            {started
              ? t("chat.lazyHistory.loadNext", "Load next chunk")
              : t("chat.lazyHistory.load", "Load content")}
          </Button>
        )}
        <span className={styles.meta}>
          {window
            ? `${formatBytes(window.start)}–${formatBytes(
                window.end,
              )} / ${formatBytes(totalBytes)}`
            : formatBytes(totalBytes)}
        </span>
      </div>
      {error && <span className={styles.error}>{error}</span>}
    </div>
  );
}
