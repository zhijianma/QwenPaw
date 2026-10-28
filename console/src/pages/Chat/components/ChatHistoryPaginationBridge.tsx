import type { IAgentScopeRuntimeWebUIRef } from "@agentscope-ai/chat";
import { ChevronsDown, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import sessionApi from "../sessionApi";
import { useChatHistoryStore } from "../chatHistoryStore";
import { isAtOlderHistoryEdge } from "../chatHistoryPagination";
import styles from "./ChatHistoryPaginationBridge.module.less";

const SCROLLER_SELECTOR =
  '[class*="chat-anywhere-message-list-bubble-scroll"]' +
  '[class*="bubble-list-order-desc"]';
const BUBBLE_SELECTOR = ".qwenpaw-bubble-list > .qwenpaw-bubble";
const ACTIVE_ANCHOR_SELECTOR = `${BUBBLE_SELECTOR}[class*="-anchor-target-active"]`;
const HOST_ANCHOR_CLASS = "qwenpaw-host-anchor-target";
const HOST_ANCHOR_SELECTOR = `${BUBBLE_SELECTOR}.${HOST_ANCHOR_CLASS}`;
const HOST_STABLE_WINDOW_CLASS = "qwenpaw-host-anchor-window-stable";
const LIVE_EDGE_THRESHOLD_PX = 96;
const VIEWPORT_OVERSCAN_PX = 160;
const ANCHOR_ALIGNMENT_TOLERANCE_PX = 2;
const ANCHOR_ALIGNMENT_STABLE_FRAMES = 3;
const ANCHOR_ALIGNMENT_MAX_FRAMES = 120;
const ANCHOR_VISIBLE_TOP_GAP_PX = 16;

interface VisibleAnchor {
  element: HTMLElement;
  id: string;
  top: number;
  scrollTop: number;
}

function bubbleElements(scroller: HTMLElement): HTMLElement[] {
  const bubbles = Array.from(
    scroller.querySelectorAll<HTMLElement>(BUBBLE_SELECTOR),
  );
  if (bubbles.length > 0) return bubbles;
  return Array.from(scroller.children).filter(
    (child): child is HTMLElement => child instanceof HTMLElement,
  );
}

function captureVisibleAnchor(scroller: HTMLElement): VisibleAnchor | null {
  const bounds = scroller.getBoundingClientRect();
  const children = bubbleElements(scroller);
  const element =
    children.find((child) => {
      const rect = child.getBoundingClientRect();
      return rect.bottom > bounds.top && rect.top < bounds.bottom;
    }) ?? children[children.length - 1];
  if (!element) return null;
  return {
    element,
    id: element.id,
    top: element.getBoundingClientRect().top,
    scrollTop: scroller.scrollTop,
  };
}

function resolveAnchor(
  scroller: HTMLElement,
  anchor: VisibleAnchor,
): HTMLElement | null {
  if (anchor.element.isConnected) return anchor.element;
  if (!anchor.id) return null;
  return (
    bubbleElements(scroller).find((element) => element.id === anchor.id) ?? null
  );
}

function isViewportUnderfilled(scroller: HTMLElement): boolean {
  return scroller.scrollHeight <= scroller.clientHeight + VIEWPORT_OVERSCAN_PX;
}

function visibleScrollerTop(scroller: HTMLElement): number {
  const scrollerRect = scroller.getBoundingClientRect();
  const layout = scroller.closest<HTMLElement>(
    '[class*="chat-anywhere-layout-right"]',
  );
  const header = layout?.querySelector<HTMLElement>(
    '[class*="chat-anywhere-layout-right-header"]',
  );
  const headerRect = header?.getBoundingClientRect();
  const headerOverlap = headerRect
    ? Math.max(0, headerRect.bottom - scrollerRect.top)
    : 0;
  const scrollPaddingTop =
    Number.parseFloat(getComputedStyle(scroller).scrollPaddingTop) || 0;
  const visibleOffset = Math.max(
    scrollPaddingTop,
    headerOverlap + (headerOverlap > 0 ? ANCHOR_VISIBLE_TOP_GAP_PX : 0),
  );
  return scrollerRect.top + visibleOffset;
}
interface ChatHistoryPaginationBridgeProps {
  rootRef: RefObject<HTMLElement | null>;
  chatRef: RefObject<IAgentScopeRuntimeWebUIRef | null>;
  sessionId?: string;
  targetTurnId?: string | null;
  onTargetResolved?: () => void;
}

/**
 * Connect normalized backend pages to the current SDK message window.
 *
 * The Runtime WebUI currently exposes its message store but not Bubble.List's
 * backend pagination callback. This adapter keeps that dependency boundary
 * local: it watches the reverse list's older edge, replaces only historical
 * messages, and preserves the visible DOM anchor across the update.
 */
export default function ChatHistoryPaginationBridge({
  rootRef,
  chatRef,
  sessionId,
  targetTurnId,
  onTargetResolved,
}: ChatHistoryPaginationBridgeProps) {
  const { t } = useTranslation();
  const exhaustedRef = useRef(false);
  const fillStoppedRef = useRef(false);
  const awayFromTailRef = useRef(false);
  const returnToLatestRef = useRef<() => Promise<void>>(async () => undefined);
  const [awayFromTail, setAwayFromTail] = useState(false);
  const [returningToLatest, setReturningToLatest] = useState(false);
  const historyToken = useChatHistoryStore((state) => {
    const path = state.activeKey ? state.paths[state.activeKey] : undefined;
    return (
      `${state.activeKey ?? ""}:${path?.ownerEpoch ?? 0}:` +
      `${path?.view?.view_version ?? "pending"}`
    );
  });

  useEffect(() => {
    exhaustedRef.current = false;
    fillStoppedRef.current = false;
    awayFromTailRef.current = false;
    setAwayFromTail(false);
  }, [historyToken, sessionId]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !sessionId) return;
    let disposed = false;
    let scroller: HTMLElement | null = null;
    let contentObserver: MutationObserver | null = null;
    let anchorObserver: MutationObserver | null = null;
    let resizeObserver: ResizeObserver | null = null;
    let preserveFrame = 0;
    let initialFillFrame = 0;
    let anchorAlignmentFrame = 0;
    let anchorAlignmentSequence = 0;
    let layoutReady = false;
    let visibleAnchor: VisibleAnchor | null = null;
    let preservingHistory = false;
    let aligningAnchor = false;
    let readingHistory = false;
    let previousOverflowAnchor = "";
    let loading = false;
    let targetStarted = false;

    const updateTailState = (atTail: boolean) => {
      awayFromTailRef.current = !atTail;
      if (!disposed) setAwayFromTail(!atTail);
    };

    const replaceHistoryMessages = (
      messages: Awaited<
        ReturnType<typeof sessionApi.loadOlderHistory>
      >["messages"],
    ) => {
      const ids = new Set(messages.map((message) => message.id));
      chatRef.current?.messages.setSessionMessages(sessionId, (previous) => [
        ...messages.map((message) => ({ ...message, history: true })),
        ...previous.filter(
          (message) =>
            !(message as typeof message & { history?: boolean }).history &&
            !ids.has(message.id),
        ),
      ]);
    };

    const rememberPosition = () => {
      if (!scroller) return;
      readingHistory = scroller.scrollTop < -LIVE_EDGE_THRESHOLD_PX;
      visibleAnchor = readingHistory ? captureVisibleAnchor(scroller) : null;
    };

    const cancelAnchorAlignment = (keepStableWindow = false) => {
      anchorAlignmentSequence += 1;
      if (anchorAlignmentFrame) {
        cancelAnimationFrame(anchorAlignmentFrame);
        anchorAlignmentFrame = 0;
      }
      scroller
        ?.querySelectorAll<HTMLElement>(HOST_ANCHOR_SELECTOR)
        .forEach((target) => target.classList.remove(HOST_ANCHOR_CLASS));
      if (!keepStableWindow) {
        scroller?.classList.remove(HOST_STABLE_WINDOW_CLASS);
      }
      aligningAnchor = false;
    };

    const alignActiveAnchor = () => {
      const currentScroller = scroller;
      const initialTarget = currentScroller?.querySelector<HTMLElement>(
        ACTIVE_ANCHOR_SELECTOR,
      );
      if (!currentScroller || !initialTarget) return;

      cancelAnchorAlignment(true);
      const sequence = anchorAlignmentSequence;
      let attempts = 0;
      let stableFrames = 0;
      aligningAnchor = true;
      initialTarget.classList.add(HOST_ANCHOR_CLASS);
      currentScroller.classList.add(HOST_STABLE_WINDOW_CLASS);

      const finish = () => {
        if (sequence !== anchorAlignmentSequence) return;
        initialTarget.classList.remove(HOST_ANCHOR_CLASS);
        anchorAlignmentFrame = requestAnimationFrame(() => {
          if (disposed || sequence !== anchorAlignmentSequence) return;
          const targetRect = initialTarget.getBoundingClientRect();
          if (targetRect.width > 0 && targetRect.height > 0) {
            currentScroller.scrollBy({
              top: targetRect.top - visibleScrollerTop(currentScroller),
              left: 0,
              behavior: "auto",
            });
          }
          anchorAlignmentFrame = 0;
          aligningAnchor = false;
          rememberPosition();
        });
      };
      const align = () => {
        if (disposed || sequence !== anchorAlignmentSequence) return;
        const target =
          currentScroller.querySelector<HTMLElement>(HOST_ANCHOR_SELECTOR);
        if (!target) {
          finish();
          return;
        }

        const targetRect = target.getBoundingClientRect();
        const offset = targetRect.top - visibleScrollerTop(currentScroller);
        if (
          targetRect.width > 0 &&
          targetRect.height > 0 &&
          Math.abs(offset) <= ANCHOR_ALIGNMENT_TOLERANCE_PX
        ) {
          stableFrames += 1;
        } else {
          stableFrames = 0;
          if (targetRect.width > 0 && targetRect.height > 0) {
            currentScroller.scrollBy({
              top: offset,
              left: 0,
              behavior: "auto",
            });
          }
        }

        attempts += 1;
        if (
          (stableFrames >= ANCHOR_ALIGNMENT_STABLE_FRAMES &&
            !target.matches(ACTIVE_ANCHOR_SELECTOR)) ||
          attempts >= ANCHOR_ALIGNMENT_MAX_FRAMES
        ) {
          finish();
          return;
        }
        anchorAlignmentFrame = requestAnimationFrame(align);
      };
      anchorAlignmentFrame = requestAnimationFrame(align);
    };

    const preserveHistoryPosition = () => {
      if (
        disposed ||
        preservingHistory ||
        aligningAnchor ||
        !readingHistory ||
        !scroller ||
        !visibleAnchor ||
        preserveFrame
      ) {
        return;
      }
      const snapshot = visibleAnchor;
      preserveFrame = requestAnimationFrame(() => {
        preserveFrame = 0;
        if (
          disposed ||
          preservingHistory ||
          !scroller ||
          !resolveAnchor(scroller, snapshot)
        ) {
          return;
        }
        const element = resolveAnchor(scroller, snapshot);
        if (!element) return;
        const topDelta = element.getBoundingClientRect().top - snapshot.top;
        scroller.scrollTop = snapshot.scrollTop + topDelta;
        visibleAnchor = captureVisibleAnchor(scroller);
      });
    };

    const loadOlder = async () => {
      const currentScroller = scroller;
      if (
        disposed ||
        !currentScroller ||
        !layoutReady ||
        aligningAnchor ||
        loading ||
        exhaustedRef.current ||
        (!isAtOlderHistoryEdge(currentScroller) &&
          !isViewportUnderfilled(currentScroller))
      ) {
        return;
      }
      const fillingViewport = isViewportUnderfilled(currentScroller);
      if (fillingViewport && fillStoppedRef.current) return;
      loading = true;
      const oldHeight = currentScroller.scrollHeight;
      const anchor = bubbleElements(currentScroller)
        .reverse()
        .find((element) => element instanceof HTMLElement);
      const oldAnchorTop = anchor?.getBoundingClientRect().top;
      try {
        const result = await sessionApi.loadOlderHistory?.(sessionId, {
          extendOnly: fillingViewport,
        });
        if (!result || disposed) return;
        updateTailState(result.atTail);
        exhaustedRef.current = result.atOlderLimit;
        if (
          fillingViewport &&
          (result.windowAtLimit || !result.replacedWindow)
        ) {
          fillStoppedRef.current = true;
        }
        if (!result.replacedWindow) return;
        preservingHistory = true;
        replaceHistoryMessages(result.messages);
        requestAnimationFrame(() => {
          if (disposed) return;
          if (anchor?.isConnected && oldAnchorTop !== undefined) {
            currentScroller.scrollTop +=
              anchor.getBoundingClientRect().top - oldAnchorTop;
          } else {
            currentScroller.scrollTop -=
              currentScroller.scrollHeight - oldHeight;
          }
          preservingHistory = false;
          rememberPosition();
          if (isViewportUnderfilled(currentScroller)) void loadOlder();
        });
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          console.error("Failed to load older chat history", error);
        }
      } finally {
        loading = false;
      }
    };

    const loadNewer = async () => {
      const currentScroller = scroller;
      if (
        disposed ||
        !currentScroller ||
        aligningAnchor ||
        loading ||
        !awayFromTailRef.current ||
        currentScroller.scrollTop < -LIVE_EDGE_THRESHOLD_PX
      ) {
        return;
      }
      loading = true;
      const anchor = captureVisibleAnchor(currentScroller);
      try {
        const result = await sessionApi.loadNewerHistory?.(sessionId);
        if (!result || disposed) return;
        updateTailState(result.atTail);
        exhaustedRef.current = result.atOlderLimit;
        if (!result.replacedWindow) return;
        preservingHistory = true;
        replaceHistoryMessages(result.messages);
        requestAnimationFrame(() => {
          if (disposed) return;
          const element = anchor
            ? resolveAnchor(currentScroller, anchor)
            : null;
          if (element && anchor) {
            currentScroller.scrollTop =
              anchor.scrollTop +
              (element.getBoundingClientRect().top - anchor.top);
          } else if (result.atTail) {
            currentScroller.scrollTop = 0;
          }
          preservingHistory = false;
          rememberPosition();
        });
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          console.error("Failed to load newer chat history", error);
        }
      } finally {
        loading = false;
      }
    };

    returnToLatestRef.current = async () => {
      const currentScroller = scroller;
      if (disposed || !currentScroller || loading) return;
      cancelAnchorAlignment(true);
      loading = true;
      setReturningToLatest(true);
      try {
        const result = await sessionApi.returnToLatestHistory?.(sessionId);
        if (!result || disposed) return;
        updateTailState(result.atTail);
        exhaustedRef.current = result.atOlderLimit;
        if (!result.replacedWindow) return;
        preservingHistory = true;
        replaceHistoryMessages(result.messages);
        requestAnimationFrame(() => {
          if (disposed) return;
          currentScroller.classList.remove(HOST_STABLE_WINDOW_CLASS);
          currentScroller.scrollTop = 0;
          preservingHistory = false;
          readingHistory = false;
          visibleAnchor = null;
        });
      } catch (error) {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          console.error("Failed to return to latest chat history", error);
        }
      } finally {
        loading = false;
        if (!disposed) setReturningToLatest(false);
      }
    };

    const navigateToTarget = async () => {
      const currentScroller = scroller;
      if (
        disposed ||
        !currentScroller ||
        !targetTurnId ||
        targetStarted ||
        loading ||
        !sessionApi.hasPaginatedHistory?.(sessionId)
      ) {
        return;
      }
      targetStarted = true;
      loading = true;
      try {
        const result = await sessionApi.loadHistoryAtTurn?.(
          sessionId,
          targetTurnId,
        );
        if (!result || disposed || !result.replacedWindow) return;
        updateTailState(result.atTail);
        exhaustedRef.current = result.atOlderLimit;
        preservingHistory = true;
        replaceHistoryMessages(result.messages);
        const scrollWhenMounted = (attempt: number) => {
          requestAnimationFrame(() => {
            if (disposed) return;
            const target = result.targetBubbleId
              ? document.getElementById(result.targetBubbleId)
              : null;
            if (target && currentScroller.contains(target)) {
              target.scrollIntoView({ block: "start", behavior: "auto" });
              preservingHistory = false;
              rememberPosition();
              onTargetResolved?.();
              return;
            }
            if (attempt < 7) {
              scrollWhenMounted(attempt + 1);
              return;
            }
            preservingHistory = false;
            console.error("Failed to mount target Chat turn", targetTurnId);
          });
        };
        scrollWhenMounted(0);
      } catch (error) {
        targetStarted = false;
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          console.error("Failed to navigate to Chat history target", error);
        }
      } finally {
        loading = false;
      }
    };

    const handleScroll = () => {
      if (!preserveFrame && !preservingHistory && !aligningAnchor) {
        rememberPosition();
      }
      if (aligningAnchor) return;
      if (
        awayFromTailRef.current &&
        scroller &&
        scroller.scrollTop >= -LIVE_EDGE_THRESHOLD_PX
      ) {
        void loadNewer();
      } else {
        void loadOlder();
      }
    };
    const scheduleInitialFill = () => {
      if (initialFillFrame) cancelAnimationFrame(initialFillFrame);
      layoutReady = false;
      initialFillFrame = requestAnimationFrame(() => {
        initialFillFrame = requestAnimationFrame(() => {
          initialFillFrame = 0;
          if (disposed) return;
          layoutReady = true;
          handleScroll();
        });
      });
    };
    const attachScroller = () => {
      if (!sessionApi.hasPaginatedHistory?.(sessionId)) return;
      if (scroller?.isConnected) return;
      const next = root.querySelector<HTMLElement>(SCROLLER_SELECTOR);
      if (!next || next === scroller) return;
      scroller?.removeEventListener("scroll", handleScroll);
      if (scroller) scroller.style.overflowAnchor = previousOverflowAnchor;
      contentObserver?.disconnect();
      scroller = next;
      previousOverflowAnchor = scroller.style.overflowAnchor;
      scroller.style.overflowAnchor = "none";
      rememberPosition();
      scroller.addEventListener("scroll", handleScroll, { passive: true });
      contentObserver = new MutationObserver(preserveHistoryPosition);
      contentObserver.observe(scroller, {
        childList: true,
        characterData: true,
        subtree: true,
      });
      anchorObserver?.disconnect();
      anchorObserver = new MutationObserver((mutations) => {
        if (
          !aligningAnchor &&
          mutations.some(
            (mutation) =>
              mutation.target instanceof HTMLElement &&
              mutation.target.matches(ACTIVE_ANCHOR_SELECTOR) &&
              !mutation.target.classList.contains(HOST_ANCHOR_CLASS),
          )
        ) {
          alignActiveAnchor();
        }
      });
      anchorObserver.observe(scroller, {
        attributes: true,
        attributeFilter: ["class"],
        subtree: true,
      });
      if (typeof ResizeObserver !== "undefined") {
        resizeObserver?.disconnect();
        resizeObserver = new ResizeObserver(() => {
          if (!layoutReady) return;
          fillStoppedRef.current = false;
          void loadOlder();
        });
        resizeObserver.observe(scroller);
      }
      if (targetTurnId) void navigateToTarget();
      else scheduleInitialFill();
    };
    const observer = new MutationObserver(attachScroller);
    observer.observe(root, { childList: true, subtree: true });
    attachScroller();
    return () => {
      disposed = true;
      observer.disconnect();
      contentObserver?.disconnect();
      anchorObserver?.disconnect();
      resizeObserver?.disconnect();
      if (preserveFrame) cancelAnimationFrame(preserveFrame);
      if (initialFillFrame) cancelAnimationFrame(initialFillFrame);
      cancelAnchorAlignment();
      scroller?.removeEventListener("scroll", handleScroll);
      if (scroller) scroller.style.overflowAnchor = previousOverflowAnchor;
      returnToLatestRef.current = async () => undefined;
    };
  }, [
    chatRef,
    historyToken,
    onTargetResolved,
    rootRef,
    sessionId,
    targetTurnId,
  ]);

  if (!awayFromTail) return null;
  const label = t("chat.history.returnLatest");
  return (
    <button
      aria-label={label}
      className={styles.returnLatest}
      disabled={returningToLatest}
      onClick={() => void returnToLatestRef.current()}
      title={label}
      type="button"
    >
      {returningToLatest ? (
        <LoaderCircle aria-hidden className={styles.spinning} size={16} />
      ) : (
        <ChevronsDown aria-hidden size={16} />
      )}
      <span>{label}</span>
    </button>
  );
}
