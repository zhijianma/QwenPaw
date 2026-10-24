import {
  Component,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { createPortal } from "react-dom";
import {
  Bubble,
  type IAgentScopeRuntimeWebUIMessage,
} from "@agentscope-ai/chat";
import { ArrowDown, LocateFixed } from "lucide-react";
import { useTranslation } from "react-i18next";
import { EagerHistoryContent } from "./HostBubbles";
import {
  historyMessageWindow,
  type HistoryUserMessage,
} from "./historyMessageWindowStore";
import styles from "./HistoryMessageWindow.module.less";

const EDGE_TOLERANCE_PX = 1;
const WHEEL_GESTURE_IDLE_MS = 160;
const EMPTY_TURN_ROWS: IAgentScopeRuntimeWebUIMessage[][] = [];

const HistoryMessage = memo(function HistoryMessage({
  message,
  isLast,
}: {
  message: IAgentScopeRuntimeWebUIMessage;
  isLast: boolean;
}) {
  return (
    <Bubble
      {...message}
      isLast={isLast}
      style={{ contentVisibility: "visible", containIntrinsicSize: "none" }}
    />
  );
});

function formatNavigationTime(createdAt: string): string {
  const date = new Date(createdAt);
  if (Number.isNaN(date.getTime())) return "";
  const month = date.getMonth() + 1;
  const day = date.getDate();
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${month}月${day}日 ${hour}:${minute}`;
}

export function HistoryNavigationDirectory({
  activeMessageId,
  items,
  openVersion,
  onSelect,
}: {
  activeMessageId: string | undefined;
  items: HistoryUserMessage[];
  openVersion: number;
  onSelect: (item: HistoryUserMessage) => void;
}) {
  const { t } = useTranslation();
  const activeItemRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const scrollActiveItemIntoView = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      const activeItem = activeItemRef.current;
      const list = listRef.current;
      if (!activeItem || !list) return;
      const nextScrollTop =
        activeItem.offsetTop -
        (list.clientHeight - activeItem.offsetHeight) / 2;
      list.scrollTo({ top: Math.max(0, nextScrollTop), behavior });
    },
    [],
  );

  useEffect(() => {
    if (!openVersion) return undefined;
    const frame = requestAnimationFrame(() => {
      scrollActiveItemIntoView("auto");
    });
    return () => cancelAnimationFrame(frame);
  }, [openVersion, scrollActiveItemIntoView]);

  return (
    <div className={styles.navigationDirectory}>
      <div className={styles.navigationDirectoryTitle}>
        <span>
          {t("chat.messageNavigation.title", { count: items.length })}
        </span>
        <button
          type="button"
          aria-label="Scroll directory to active user message"
          className={styles.navigationDirectoryLocate}
          disabled={!activeMessageId}
          onClick={(event) => {
            event.stopPropagation();
            scrollActiveItemIntoView();
          }}
        >
          <LocateFixed size={14} />
        </button>
      </div>
      <div className={styles.navigationDirectoryList} ref={listRef}>
        {items.map((item) => {
          const active = item.messageId === activeMessageId;
          return (
            <button
              key={item.messageId}
              type="button"
              className={`${styles.navigationItem} ${
                active ? styles.navigationItemActive : ""
              }`}
              onClick={() => onSelect(item)}
              ref={active ? activeItemRef : undefined}
            >
              <span>{item.preview || "User message"}</span>
              <time dateTime={item.createdAt}>
                {formatNavigationTime(item.createdAt)}
              </time>
            </button>
          );
        })}
      </div>
    </div>
  );
}

interface ContentsProps {
  messages: IAgentScopeRuntimeWebUIMessage[];
  sessionId: string;
}

interface ScrollAnchor {
  element: HTMLElement;
  offset: number;
}

// Capture the actual visible DOM before React inserts a page.
class HistoryContents extends Component<ContentsProps> {
  private content: HTMLDivElement | null = null;

  getSnapshotBeforeUpdate(previous: ContentsProps): ScrollAnchor | null {
    const content = this.content;
    const scroll = content?.parentElement;
    if (!content || !scroll || previous.sessionId !== this.props.sessionId) {
      return null;
    }
    const top = scroll.getBoundingClientRect().top;
    const element = Array.from(
      content.querySelectorAll<HTMLElement>("[data-role][id]"),
    ).find((item) => item.id && item.getBoundingClientRect().bottom > top);
    return element
      ? { element, offset: element.getBoundingClientRect().top - top }
      : null;
  }

  componentDidUpdate(
    _previous: ContentsProps,
    _state: unknown,
    anchor: ScrollAnchor | null,
  ) {
    const scroll = this.content?.parentElement;
    if (!scroll || !anchor?.element.isConnected) return;
    const offset =
      anchor.element.getBoundingClientRect().top -
      scroll.getBoundingClientRect().top;
    if (Math.abs(offset - anchor.offset) > 0.5) {
      scroll.scrollTop += offset - anchor.offset;
    }
  }

  render() {
    return (
      <div
        ref={(node) => {
          this.content = node;
        }}
        className={styles.page}
      >
        <EagerHistoryContent>
          {this.props.messages.map((message, index) => (
            <HistoryMessage
              key={message.id}
              message={message}
              isLast={index === this.props.messages.length - 1}
            />
          ))}
        </EagerHistoryContent>
      </div>
    );
  }
}

export function HistoryMessageWindowCard() {
  const current = useSyncExternalStore(
    historyMessageWindow.subscribe,
    historyMessageWindow.getSnapshot,
    () => null,
  );
  const placeholderRef = useRef<HTMLSpanElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const initialSessionRef = useRef<string>();
  const pendingNavigationRef = useRef<string>();
  const pendingBottomRef = useRef(false);
  const [atBottom, setAtBottom] = useState(true);
  const wheelUntilRef = useRef(0);
  const [portalTarget, setPortalTarget] = useState<HTMLElement | null>(null);
  const turnRows = current?.turnRows ?? EMPTY_TURN_ROWS;
  const messages = useMemo(() => turnRows.flat(), [turnRows]);
  const latestUser = current?.userMessages[current.userMessages.length - 1];

  const locate = useCallback((messageId: string) => {
    const scroll = scrollRef.current;
    const element = Array.from(
      scroll?.querySelectorAll<HTMLElement>("[data-role][id]") ?? [],
    ).find((item) => item.id === messageId);
    if (!scroll || !element) return false;
    const header = scroll
      .closest(".qwenpaw-chat-anywhere-layout-right")
      ?.querySelector<HTMLElement>(
        ".qwenpaw-chat-anywhere-layout-right-header",
      );
    const headerInset = header
      ? Math.max(
          0,
          header.getBoundingClientRect().bottom -
            scroll.getBoundingClientRect().top,
        )
      : 0;
    scroll.scrollTop +=
      element.getBoundingClientRect().top -
      scroll.getBoundingClientRect().top -
      headerInset -
      24;
    return true;
  }, []);

  useLayoutEffect(() => {
    setPortalTarget(
      placeholderRef.current?.closest<HTMLElement>(
        ".qwenpaw-chat-anywhere-message-list",
      ) ?? null,
    );
  }, []);

  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (!current || !scroll) return;
    if (initialSessionRef.current !== current.sessionId && messages.length) {
      initialSessionRef.current = current.sessionId;
      scroll.scrollTop = scroll.scrollHeight;
    }
    const pending = pendingNavigationRef.current;
    if (pending && locate(pending)) {
      pendingNavigationRef.current = undefined;
      if (pendingBottomRef.current) {
        scroll.scrollTop = scroll.scrollHeight;
        pendingBottomRef.current = false;
      }
    }
  }, [current, messages, locate, portalTarget]);

  const loadNewerHistory = useCallback(() => {
    if (!current) return;
    const lastUser = [...messages]
      .reverse()
      .find((item) => item.role === "user");
    const index = current.userMessages.findIndex(
      (item) => item.messageId === lastUser?.id,
    );
    const next =
      current.userMessages[
        Math.min(index + 5, current.userMessages.length - 1)
      ];
    if (index >= 0 && next && next.messageId !== lastUser?.id) {
      current.onNavigateToUserMessage(next, "append-newer");
    }
  }, [current, messages]);

  const updateActiveMessage = useCallback(() => {
    const scroll = scrollRef.current;
    if (!scroll || !current) return;
    const remaining =
      scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop;
    setAtBottom(remaining <= EDGE_TOLERANCE_PX);
    if (
      historyMessageWindow.getSnapshot()?.loading ||
      scroll.clientHeight <= 0 ||
      scroll.scrollHeight <= scroll.clientHeight
    ) {
      return;
    }
    // Fetch before the native scroll reaches an unloaded boundary.
    const threshold = scroll.clientHeight / 2;
    if (scroll.scrollTop <= threshold && current.canLoadOlder) {
      current.onLoadOlder(scroll);
    } else if (remaining <= threshold) {
      loadNewerHistory();
    }
  }, [current, loadNewerHistory]);

  useEffect(() => {
    const scroll = scrollRef.current;
    if (!scroll || !current) return;
    const cancelNavigation = () => {
      if (!pendingNavigationRef.current) return;
      pendingNavigationRef.current = undefined;
      pendingBottomRef.current = false;
      current.onCancelPending();
    };
    const wheel = (event: WheelEvent) => {
      cancelNavigation();
      if (current.loading || Date.now() < wheelUntilRef.current) return;
      if (event.deltaY < 0 && scroll.scrollTop <= EDGE_TOLERANCE_PX) {
        if (!current.canLoadOlder) return;
        wheelUntilRef.current = Date.now() + WHEEL_GESTURE_IDLE_MS;
        current.onLoadOlder(scroll);
      } else if (
        event.deltaY > 0 &&
        scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop <=
          EDGE_TOLERANCE_PX
      ) {
        wheelUntilRef.current = Date.now() + WHEEL_GESTURE_IDLE_MS;
        loadNewerHistory();
      }
    };
    scroll.addEventListener("wheel", wheel, { passive: true });
    scroll.addEventListener("pointerdown", cancelNavigation);
    scroll.addEventListener("touchstart", cancelNavigation, { passive: true });
    scroll.addEventListener("keydown", cancelNavigation);
    return () => {
      scroll.removeEventListener("wheel", wheel);
      scroll.removeEventListener("pointerdown", cancelNavigation);
      scroll.removeEventListener("touchstart", cancelNavigation);
      scroll.removeEventListener("keydown", cancelNavigation);
    };
  }, [current, loadNewerHistory, portalTarget]);

  useEffect(() => {
    const scroll = scrollRef.current;
    if (!current || !scroll || current.loading) return;
    const frame = requestAnimationFrame(() => {
      if (
        scroll.clientHeight > 0 &&
        scroll.scrollHeight <= scroll.clientHeight + 1
      ) {
        if (current.canLoadOlder) {
          current.onFillOlder(scroll);
        } else {
          loadNewerHistory();
        }
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [current, loadNewerHistory, portalTarget]);

  return (
    <span ref={placeholderRef}>
      {current && portalTarget
        ? createPortal(
            <div className={styles.window}>
              <div
                ref={scrollRef}
                className={styles.list}
                data-history-message-window="true"
                data-history-loading={String(current.loading)}
                onScroll={updateActiveMessage}
              >
                <HistoryContents
                  messages={messages}
                  sessionId={current.sessionId}
                />
              </div>
              {(current.onReturnToLatest ||
                !atBottom ||
                (current.userMessages.length > 0 &&
                  !messages.some(
                    (item) => item.id === latestUser?.messageId,
                  ))) && (
                <button
                  type="button"
                  className={styles.scrollToBottom}
                  aria-label="Scroll to bottom"
                  onClick={() => {
                    if (current.onReturnToLatest) {
                      current.onReturnToLatest();
                      return;
                    }
                    const latest = latestUser;
                    if (
                      latest &&
                      !messages.some((item) => item.id === latest.messageId)
                    ) {
                      pendingNavigationRef.current = latest.messageId;
                      pendingBottomRef.current = true;
                      current.onNavigateToUserMessage(latest, "replace");
                    } else if (scrollRef.current) {
                      scrollRef.current.scrollTop =
                        scrollRef.current.scrollHeight;
                    }
                  }}
                >
                  <ArrowDown size={18} />
                </button>
              )}
            </div>,
            portalTarget,
          )
        : null}
    </span>
  );
}
