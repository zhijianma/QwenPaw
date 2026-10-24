import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { createPortal } from "react-dom";
import { useContextSelector } from "use-context-selector";
import { ChatAnywhereMessagesContext } from "@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/Context/ChatAnywhereMessagesContext";
import { Popover, Tooltip } from "antd";
import { ChevronDown, ChevronUp, List } from "lucide-react";
import { HistoryNavigationDirectory } from "./HistoryMessageWindow";
import {
  historyMessageWindow,
  type HistoryUserMessage,
} from "./historyMessageWindowStore";
import {
  activeUserAtScrollPosition,
  getNavigationUsers,
  mergeNavigationUsers,
} from "./userMessageNavigation";
import sessionApi from "./sessionApi";
import styles from "./HistoryMessageWindow.module.less";

export function ChatUserMessageNavigation({
  sessionId,
  onNavigate,
  onCancel,
}: {
  sessionId?: string;
  onNavigate: (item: HistoryUserMessage) => void;
  onCancel: () => void;
}) {
  // Assistant deltas must not trigger directory renders or network requests.
  const usersKey = useContextSelector(ChatAnywhereMessagesContext, (value) =>
    JSON.stringify(getNavigationUsers(value.messages)),
  );
  const visible = useMemo<HistoryUserMessage[]>(
    () => JSON.parse(usersKey),
    [usersKey],
  );
  const window = useSyncExternalStore(
    historyMessageWindow.subscribe,
    historyMessageWindow.getSnapshot,
    () => null,
  );
  const [directory, setDirectory] = useState<{
    sessionId: string;
    items: HistoryUserMessage[];
  }>();
  const marker = useRef<HTMLSpanElement>(null);
  const [host, setHost] = useState<HTMLElement | null>(null);
  const [active, setActive] = useState<string>();
  const [open, setOpen] = useState(false);
  const [openVersion, setOpenVersion] = useState(0);
  const pending = useRef<HistoryUserMessage>();
  const currentWindow = window?.sessionId === sessionId ? window : null;
  const persisted =
    directory && directory.sessionId === sessionId
      ? directory.items
      : currentWindow?.userMessages ?? [];
  const items = mergeNavigationUsers(persisted, visible);

  useLayoutEffect(() => {
    setHost(
      marker.current?.closest<HTMLElement>(
        ".qwenpaw-chat-anywhere-layout-right",
      ) ?? null,
    );
  }, []);

  useEffect(() => {
    pending.current = undefined;
    setOpen(false);
    setActive(undefined);
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    void sessionApi
      .loadUserMessageIndex(sessionId, controller.signal)
      .then((entries) => {
        if (!controller.signal.aborted) {
          setDirectory({ sessionId, items: entries });
          historyMessageWindow.setUserMessages(sessionId, entries);
        }
      })
      .catch(() => {
        // Keep the last complete index and locally visible users on failure.
      });
    return () => controller.abort();
  }, [sessionId, usersKey]);

  const getScroll = useCallback(
    () =>
      host?.querySelector<HTMLElement>("[data-history-message-window]") ??
      host?.querySelector<HTMLElement>(
        ".qwenpaw-chat-anywhere-message-list-bubble-scroll",
      ),
    [host],
  );
  const locate = (item: HistoryUserMessage) => {
    const scroll = getScroll();
    if (!scroll) return false;
    const alias = visible.find(
      (entry) =>
        entry.messageId === item.clientMessageId ||
        (entry.clientMessageId &&
          entry.clientMessageId === item.clientMessageId),
    );
    const element = Array.from(
      scroll.querySelectorAll<HTMLElement>('[data-role="user"][id]'),
    ).find(
      (node) => node.id === item.messageId || node.id === alias?.messageId,
    );
    if (!element) return false;
    const top = scroll.getBoundingClientRect().top;
    const header = host?.querySelector<HTMLElement>(
      ".qwenpaw-chat-anywhere-layout-right-header",
    );
    const inset = Math.max(
      0,
      (header?.getBoundingClientRect().bottom ?? top) - top,
    );
    scroll.scrollTop += element.getBoundingClientRect().top - top - inset - 24;
    setActive(item.messageId);
    return true;
  };
  useLayoutEffect(() => {
    if (pending.current && locate(pending.current)) pending.current = undefined;
  });

  useEffect(() => {
    if (!host) return;
    const cancel = () => {
      if (!pending.current) return;
      pending.current = undefined;
      onCancel();
    };
    const update = () => {
      const scroll = getScroll();
      if (!scroll) return;
      const header = host.querySelector<HTMLElement>(
        ".qwenpaw-chat-anywhere-layout-right-header",
      );
      const top =
        Math.max(
          scroll.getBoundingClientRect().top,
          header?.getBoundingClientRect().bottom ?? 0,
        ) + 24;
      const users = Array.from(
        scroll.querySelectorAll<HTMLElement>('[data-role="user"][id]'),
      ).map((element) => ({
        id: element.id,
        top: element.getBoundingClientRect().top,
      }));
      const reversed = scroll.className.includes("bubble-list-order-desc");
      const atBottom = reversed
        ? Math.abs(scroll.scrollTop) <= 1
        : scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop <= 1;
      const id = activeUserAtScrollPosition(users, top, atBottom);
      if (id) setActive(id);
    };
    const frame = requestAnimationFrame(update);
    host.addEventListener("scroll", update, true);
    host.addEventListener("wheel", cancel, { passive: true });
    host.addEventListener("pointerdown", cancel);
    host.addEventListener("keydown", cancel);
    host.addEventListener("touchstart", cancel, { passive: true });
    return () => {
      cancelAnimationFrame(frame);
      host.removeEventListener("scroll", update, true);
      host.removeEventListener("wheel", cancel);
      host.removeEventListener("pointerdown", cancel);
      host.removeEventListener("keydown", cancel);
      host.removeEventListener("touchstart", cancel);
    };
  }, [host, onCancel, currentWindow, getScroll, usersKey]);

  const select = (item: HistoryUserMessage) => {
    onCancel();
    pending.current = undefined;
    setOpen(false);
    if (locate(item) || !item.anchor) return;
    pending.current = item;
    const targetIndex = items.findIndex(
      (entry) => entry.messageId === item.messageId,
    );
    // Include following turns so the target can align below the header.
    const pageEnd = items[Math.min(targetIndex + 2, items.length - 1)];
    onNavigate({ ...item, anchor: pageEnd?.anchor || item.anchor });
  };
  const found = items.findIndex(
    (item) =>
      Boolean(active) &&
      (item.messageId === active || item.clientMessageId === active),
  );
  const index = found >= 0 ? found : items.length - 1;
  return (
    <span ref={marker}>
      {host && sessionId && items.length > 0
        ? createPortal(
            <nav
              className={styles.navigation}
              aria-label="User message navigation"
            >
              <Tooltip title="Previous user message" placement="left">
                <button
                  type="button"
                  aria-label="Previous user message"
                  disabled={index <= 0}
                  onClick={() => select(items[index - 1])}
                >
                  <ChevronUp size={18} />
                </button>
              </Tooltip>
              <Popover
                open={open}
                onOpenChange={(value) => {
                  setOpen(value);
                  if (value) setOpenVersion((version) => version + 1);
                }}
                content={
                  <HistoryNavigationDirectory
                    activeMessageId={items[index]?.messageId}
                    items={items}
                    openVersion={openVersion}
                    onSelect={select}
                  />
                }
                overlayClassName={styles.navigationDirectoryPopover}
                placement="left"
                styles={{ body: { padding: 0 } }}
                trigger="click"
              >
                <button type="button" aria-label="User message directory">
                  <List size={18} />
                  <span>{items.length}</span>
                </button>
              </Popover>
              <Tooltip title="Next user message" placement="left">
                <button
                  type="button"
                  aria-label="Next user message"
                  disabled={index >= items.length - 1}
                  onClick={() => select(items[index + 1])}
                >
                  <ChevronDown size={18} />
                </button>
              </Tooltip>
            </nav>,
            host,
          )
        : null}
    </span>
  );
}
