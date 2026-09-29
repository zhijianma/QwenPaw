import { beforeEach, describe, expect, it, vi } from "vitest";

const sessionApiMock = vi.hoisted(() => ({
  lastActiveChatId: null as string | null,
  getSessionIdentity: vi.fn(
    (id: string): { sessionId: string; chatId?: string } => ({
      sessionId: id,
    }),
  ),
  getPersistedChatId: vi.fn((_id: string): string | null => null),
}));

vi.mock("../../pages/Chat/sessionApi", () => ({ default: sessionApiMock }));

import { getCurrentChatId, getCurrentSessionId } from "./hooks";

describe("getCurrentSessionId", () => {
  beforeEach(() => {
    sessionApiMock.lastActiveChatId = null;
    sessionApiMock.getSessionIdentity.mockImplementation((id: string) => ({
      sessionId: id,
    }));
    sessionApiMock.getPersistedChatId.mockReturnValue(null);
  });

  it("resolves the backend identity from a chat route", () => {
    window.history.replaceState({}, "", "/chat/chat-1");
    sessionApiMock.getSessionIdentity.mockReturnValue({
      sessionId: "backend-1",
    });

    expect(getCurrentSessionId()).toBe("backend-1");
  });

  it("preserves the last dialogue owned by the current PawApp", () => {
    window.history.replaceState({}, "", "/apps/office");
    sessionApiMock.lastActiveChatId = "chat-1";
    sessionApiMock.getSessionIdentity.mockReturnValue({
      sessionId: "pawapp:office:dialogue:1",
    });

    expect(getCurrentSessionId()).toBe("pawapp:office:dialogue:1");
  });

  it("does not expose another app or host session to a PawApp", () => {
    window.history.replaceState({}, "", "/apps/office");
    sessionApiMock.lastActiveChatId = "chat-1";
    sessionApiMock.getSessionIdentity.mockReturnValue({
      sessionId: "ordinary-host-session",
    });

    expect(getCurrentSessionId()).toBeNull();
  });
});

describe("getCurrentChatId", () => {
  beforeEach(() => {
    sessionApiMock.lastActiveChatId = null;
    sessionApiMock.getSessionIdentity.mockImplementation((id: string) => ({
      sessionId: id,
    }));
  });

  it("returns the persisted ChatSpec identity from a chat route", () => {
    window.history.replaceState({}, "", "/chat/route-reference");
    sessionApiMock.getPersistedChatId.mockReturnValue("chat-spec-1");

    expect(getCurrentChatId()).toBe("chat-spec-1");
  });

  it("does not return a runtime session for an unresolved chat", () => {
    window.history.replaceState({}, "", "/chat/local-reference");
    sessionApiMock.getSessionIdentity.mockReturnValue({
      sessionId: "runtime-session",
    });
    sessionApiMock.getPersistedChatId.mockReturnValue(null);

    expect(getCurrentChatId()).toBeNull();
  });

  it("returns only the current PawApp dialogue ChatSpec", () => {
    window.history.replaceState({}, "", "/apps/office");
    sessionApiMock.lastActiveChatId = "chat-spec-2";
    sessionApiMock.getSessionIdentity.mockReturnValue({
      sessionId: "pawapp:office:dialogue:1",
      chatId: "chat-spec-2",
    });
    sessionApiMock.getPersistedChatId.mockReturnValue("chat-spec-2");

    expect(getCurrentChatId()).toBe("chat-spec-2");
  });
});
