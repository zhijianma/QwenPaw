import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type {
  IAgentScopeRuntimeWebUIMessage,
  IAgentScopeRuntimeWebUIRef,
} from "@agentscope-ai/chat";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChatHistoryPaginationBridge from "./ChatHistoryPaginationBridge";
import sessionApi, { type HistoryLoadResult } from "../sessionApi";
import { isAtOlderHistoryEdge } from "../chatHistoryPagination";

describe("ChatHistoryPaginationBridge", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("recognizes the older edge of a reverse message list", () => {
    const scroller = document.createElement("div");
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    scroller.scrollTop = -600;
    expect(isAtOlderHistoryEdge(scroller)).toBe(true);
    scroller.scrollTop = -300;
    expect(isAtOlderHistoryEdge(scroller)).toBe(false);
  });

  it("attaches after the normalized history owner becomes ready", async () => {
    const root = document.createElement("div");
    const scroller = document.createElement("div");
    scroller.className =
      "chat-anywhere-message-list-bubble-scroll bubble-list-order-desc";
    root.append(scroller);
    document.body.append(root);
    let ready = false;
    vi.spyOn(sessionApi, "hasPaginatedHistory").mockImplementation(() => ready);
    const chatRef = {
      current: {
        messages: { setSessionMessages: vi.fn() },
      } as unknown as IAgentScopeRuntimeWebUIRef,
    };

    const view = render(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={chatRef}
        sessionId="session-late-owner"
      />,
    );
    expect(scroller.style.overflowAnchor).toBe("");

    ready = true;
    root.append(document.createElement("div"));

    await waitFor(() => expect(scroller.style.overflowAnchor).toBe("none"));

    const replacement = document.createElement("div");
    replacement.className = scroller.className;
    scroller.replaceWith(replacement);
    await waitFor(() => expect(replacement.style.overflowAnchor).toBe("none"));
    expect(scroller.style.overflowAnchor).toBe("");
    view.unmount();
    root.remove();
  });

  it("isolates in-flight pagination locks across Chat switches", async () => {
    const root = document.createElement("div");
    const scroller = document.createElement("div");
    scroller.className =
      "chat-anywhere-message-list-bubble-scroll bubble-list-order-desc";
    root.append(scroller);
    document.body.append(root);
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    scroller.scrollTop = -600;
    vi.spyOn(sessionApi, "hasPaginatedHistory").mockReturnValue(true);
    let resolveFirst!: (value: HistoryLoadResult) => void;
    let resolveSecond!: (value: HistoryLoadResult) => void;
    const loadOlder = vi
      .spyOn(sessionApi, "loadOlderHistory")
      .mockImplementationOnce(
        () => new Promise((resolve) => (resolveFirst = resolve)),
      )
      .mockImplementationOnce(
        () => new Promise((resolve) => (resolveSecond = resolve)),
      );
    const chatRef = {
      current: {
        messages: { setSessionMessages: vi.fn() },
      } as unknown as IAgentScopeRuntimeWebUIRef,
    };
    const result: HistoryLoadResult = {
      messages: [],
      noMore: false,
      replacedWindow: false,
      windowAtLimit: false,
      turnCount: 0,
      atOlderLimit: false,
      atTail: true,
    };

    const view = render(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={chatRef}
        sessionId="session-a"
      />,
    );
    await waitFor(() => expect(loadOlder).toHaveBeenCalledTimes(1));
    view.rerender(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={chatRef}
        sessionId="session-b"
      />,
    );
    await waitFor(() => expect(loadOlder).toHaveBeenCalledTimes(2));

    resolveFirst(result);
    await Promise.resolve();
    fireEvent.scroll(scroller);
    await Promise.resolve();
    expect(loadOlder).toHaveBeenCalledTimes(2);

    resolveSecond(result);
    view.unmount();
    root.remove();
  });

  it("hydrates an off-window turn before scrolling to its user Bubble", async () => {
    const root = document.createElement("div");
    const scroller = document.createElement("div");
    scroller.className =
      "chat-anywhere-message-list-bubble-scroll bubble-list-order-desc";
    root.append(scroller);
    document.body.append(root);
    vi.spyOn(sessionApi, "hasPaginatedHistory").mockReturnValue(true);
    vi.spyOn(sessionApi, "loadHistoryAtTurn").mockResolvedValue({
      messages: [{ id: "target-user", role: "user", cards: [] }],
      noMore: false,
      replacedWindow: true,
      windowAtLimit: false,
      turnCount: 5,
      atOlderLimit: false,
      atTail: false,
      targetBubbleId: "target-user",
    });
    const target = document.createElement("div");
    target.id = "target-user";
    target.scrollIntoView = vi.fn();
    const setSessionMessages = vi.fn(() => {
      scroller.append(target);
    });
    const chatRef = {
      current: {
        messages: { setSessionMessages },
      } as unknown as IAgentScopeRuntimeWebUIRef,
    };
    const onTargetResolved = vi.fn();

    const view = render(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={chatRef}
        sessionId="session-target"
        targetTurnId="turn-target"
        onTargetResolved={onTargetResolved}
      />,
    );

    await waitFor(() => {
      expect(sessionApi.loadHistoryAtTurn).toHaveBeenCalledWith(
        "session-target",
        "turn-target",
      );
      expect(target.scrollIntoView).toHaveBeenCalledWith({
        block: "start",
        behavior: "auto",
      });
      expect(onTargetResolved).toHaveBeenCalledTimes(1);
    });
    view.unmount();
    root.remove();
  });

  it("replaces only history and preserves the visible anchor", async () => {
    const root = document.createElement("div");
    const scroller = document.createElement("div");
    scroller.className =
      "chat-anywhere-message-list-bubble-scroll bubble-list-order-desc";
    const anchor = document.createElement("div");
    scroller.append(anchor);
    root.append(scroller);
    document.body.append(root);
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    scroller.scrollTop = -600;
    let anchorTop = 100;
    vi.spyOn(anchor, "getBoundingClientRect").mockImplementation(
      () => ({ top: anchorTop }) as DOMRect,
    );
    vi.spyOn(sessionApi, "hasPaginatedHistory").mockReturnValue(true);
    vi.spyOn(sessionApi, "loadOlderHistory").mockResolvedValue({
      messages: [{ id: "older", role: "user", cards: [] }],
      noMore: true,
      replacedWindow: true,
      windowAtLimit: false,
      turnCount: 2,
      atOlderLimit: true,
      atTail: true,
    });
    const setSessionMessages = vi.fn(
      (
        _id: string,
        update: (
          previous: IAgentScopeRuntimeWebUIMessage[],
        ) => IAgentScopeRuntimeWebUIMessage[],
      ) => {
        anchorTop = 150;
        const next = update([
          {
            id: "old-history",
            role: "user",
            cards: [],
            history: true,
          } as IAgentScopeRuntimeWebUIMessage & { history: true },
          { id: "live", role: "assistant", cards: [] },
        ]);
        expect(next.map((message) => message.id)).toEqual(["older", "live"]);
        return next;
      },
    );
    const chatRef = {
      current: {
        messages: { setSessionMessages },
      } as unknown as IAgentScopeRuntimeWebUIRef,
    };

    const view = render(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={chatRef}
        sessionId="chat"
      />,
    );

    await waitFor(() => {
      expect(sessionApi.loadOlderHistory).toHaveBeenCalledTimes(1);
      expect(setSessionMessages).toHaveBeenCalledTimes(1);
      expect(scroller.scrollTop).toBe(-550);
    });
    view.unmount();
    root.remove();
  });

  it("keeps a reader anchored when the live tail mutates", async () => {
    const root = document.createElement("div");
    const scroller = document.createElement("div");
    scroller.className =
      "chat-anywhere-message-list-bubble-scroll bubble-list-order-desc";
    const anchor = document.createElement("div");
    scroller.append(anchor);
    root.append(scroller);
    document.body.append(root);
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue({
      top: 0,
      bottom: 400,
    } as DOMRect);
    let anchorTop = 100;
    vi.spyOn(anchor, "getBoundingClientRect").mockImplementation(
      () => ({ top: anchorTop, bottom: anchorTop + 40 }) as DOMRect,
    );
    scroller.scrollTop = -300;
    vi.spyOn(sessionApi, "hasPaginatedHistory").mockReturnValue(true);
    vi.spyOn(sessionApi, "loadOlderHistory").mockResolvedValue({
      messages: [],
      noMore: true,
      replacedWindow: false,
      windowAtLimit: false,
      turnCount: 1,
      atOlderLimit: true,
      atTail: true,
    });

    const view = render(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={{ current: null }}
        sessionId="chat"
      />,
    );
    scroller.dispatchEvent(new Event("scroll"));
    anchorTop = 150;
    scroller.append(document.createElement("div"));
    await Promise.resolve();
    // Model the SDK's unconditional new-message scroll after the DOM commit.
    scroller.scrollTop = 0;
    scroller.dispatchEvent(new Event("scroll"));

    await waitFor(() => expect(scroller.scrollTop).toBe(-250));
    view.unmount();
    root.remove();
  });

  it("serially fills a large viewport with five-turn pages", async () => {
    const root = document.createElement("div");
    const scroller = document.createElement("div");
    scroller.className =
      "chat-anywhere-message-list-bubble-scroll bubble-list-order-desc";
    root.append(scroller);
    document.body.append(root);
    let replacements = 0;
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 1000 },
      scrollHeight: {
        configurable: true,
        get: () => 400 + replacements * 450,
      },
    });
    vi.spyOn(sessionApi, "hasPaginatedHistory").mockReturnValue(true);
    vi.spyOn(sessionApi, "loadOlderHistory")
      .mockResolvedValueOnce({
        messages: [{ id: "page-2", role: "user", cards: [] }],
        noMore: false,
        replacedWindow: true,
        windowAtLimit: false,
        turnCount: 10,
        atOlderLimit: false,
        atTail: true,
      })
      .mockResolvedValueOnce({
        messages: [{ id: "page-3", role: "user", cards: [] }],
        noMore: false,
        replacedWindow: true,
        windowAtLimit: false,
        turnCount: 15,
        atOlderLimit: false,
        atTail: true,
      });
    const setSessionMessages = vi.fn(() => {
      replacements += 1;
      return [];
    });
    const chatRef = {
      current: {
        messages: { setSessionMessages },
      } as unknown as IAgentScopeRuntimeWebUIRef,
    };

    const view = render(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={chatRef}
        sessionId="chat"
      />,
    );

    await waitFor(() => {
      expect(sessionApi.loadOlderHistory).toHaveBeenCalledTimes(2);
      expect(sessionApi.loadOlderHistory).toHaveBeenNthCalledWith(1, "chat", {
        extendOnly: true,
      });
      expect(sessionApi.loadOlderHistory).toHaveBeenNthCalledWith(2, "chat", {
        extendOnly: true,
      });
    });
    view.unmount();
    root.remove();
  });

  it("moves toward newer history and can atomically return to the tail", async () => {
    const root = document.createElement("div");
    const scroller = document.createElement("div");
    scroller.className =
      "chat-anywhere-message-list-bubble-scroll bubble-list-order-desc";
    const anchor = document.createElement("div");
    scroller.append(anchor);
    root.append(scroller);
    document.body.append(root);
    Object.defineProperties(scroller, {
      clientHeight: { configurable: true, value: 400 },
      scrollHeight: { configurable: true, value: 1000 },
    });
    scroller.scrollTop = -600;
    vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue({
      top: 100,
      bottom: 140,
    } as DOMRect);
    vi.spyOn(sessionApi, "hasPaginatedHistory").mockReturnValue(true);
    vi.spyOn(sessionApi, "loadOlderHistory").mockResolvedValue({
      messages: [{ id: "older-window", role: "user", cards: [] }],
      noMore: false,
      replacedWindow: true,
      windowAtLimit: false,
      turnCount: 24,
      atOlderLimit: false,
      atTail: false,
    });
    const loadNewer = vi
      .spyOn(sessionApi, "loadNewerHistory")
      .mockResolvedValue({
        messages: [{ id: "newer-window", role: "user", cards: [] }],
        noMore: true,
        replacedWindow: true,
        windowAtLimit: false,
        turnCount: 24,
        atOlderLimit: false,
        atTail: true,
      });
    const returnLatest = vi
      .spyOn(sessionApi, "returnToLatestHistory")
      .mockResolvedValue({
        messages: [{ id: "tail", role: "user", cards: [] }],
        noMore: true,
        replacedWindow: true,
        windowAtLimit: false,
        turnCount: 5,
        atOlderLimit: false,
        atTail: true,
      });
    const setSessionMessages = vi.fn();
    const chatRef = {
      current: {
        messages: { setSessionMessages },
      } as unknown as IAgentScopeRuntimeWebUIRef,
    };

    const view = render(
      <ChatHistoryPaginationBridge
        rootRef={{ current: root }}
        chatRef={chatRef}
        sessionId="chat"
      />,
    );

    await screen.findByRole("button", {
      name: "chat.history.returnLatest",
    });
    scroller.scrollTop = 0;
    scroller.dispatchEvent(new Event("scroll"));
    await waitFor(() => expect(loadNewer).toHaveBeenCalledWith("chat"));

    scroller.scrollTop = -600;
    scroller.dispatchEvent(new Event("scroll"));
    const latestButton = await screen.findByRole("button", {
      name: "chat.history.returnLatest",
    });
    fireEvent.click(latestButton);
    await waitFor(() => expect(returnLatest).toHaveBeenCalledWith("chat"));
    view.unmount();
    root.remove();
  });
});
