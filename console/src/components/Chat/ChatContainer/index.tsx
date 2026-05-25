import React, { useCallback, useEffect, useMemo, useRef } from "react";
import {
  ChatContext,
  type ChatContextValue,
  type UserDisplayInfo,
  type AssistantDisplayInfo,
} from "../context/ChatContext";
import { useChatStore } from "../stores/chatStore";
import { useSessionStore } from "../stores/sessionStore";
import { useChatStream } from "../hooks/useChatStream";
import { useToolCards } from "../hooks/useToolCards";
import { SESSION_STATUS } from "../constants";
import type { ChatConfig, ChatInputData, ToolCardRegistry } from "../types";
import styles from "./ChatContainer.module.less";

export interface ChatContainerProps {
  /** Chat API configuration */
  config: ChatConfig;
  /** Agent ID for context */
  agentId?: string;
  /** Custom tool card components */
  toolCards?: ToolCardRegistry;
  /** User display info (name, avatar) */
  userInfo?: UserDisplayInfo;
  /** Assistant display info (name, avatar, model) */
  assistantInfo?: AssistantDisplayInfo;
  /** Children components (MessageList, MessageInput, SessionPanel) */
  children: React.ReactNode;
  /** Custom class name */
  className?: string;
  /** Called when history should be cleared */
  onHistoryClear?: () => void;
  /** Called on stream errors */
  onError?: (error: Error) => void;
}

const ChatContainer: React.FC<ChatContainerProps> = ({
  config,
  agentId,
  toolCards = {},
  userInfo,
  assistantInfo,
  children,
  className,
  onHistoryClear,
  onError,
}) => {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const clearMessages = useChatStore((s) => s.clearMessages);

  const { registry } = useToolCards({ cards: toolCards });

  const handleHistoryClear = useCallback(() => {
    if (activeSessionId) {
      clearMessages(activeSessionId);
    }
    onHistoryClear?.();
  }, [activeSessionId, clearMessages, onHistoryClear]);

  const { send, cancel, reconnect } = useChatStream({
    config,
    sessionId: activeSessionId,
    onHistoryClear: handleHistoryClear,
    onError,
  });

  // Auto-reconnect when switching FROM one session TO another that is still running
  const prevSessionRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = prevSessionRef.current;
    if (!activeSessionId || activeSessionId === prev) {
      prevSessionRef.current = activeSessionId;
      return;
    }
    prevSessionRef.current = activeSessionId;

    // Only reconnect when switching between sessions (prev was a real session)
    // Skip when coming from null (new session just created by send)
    if (!prev) return;

    const session = useSessionStore
      .getState()
      .sessions.find((s) => s.id === activeSessionId);
    if (session?.status === SESSION_STATUS.RUNNING) {
      reconnect();
    }
  }, [activeSessionId, reconnect]);

  const handleSend = useCallback(
    (input: ChatInputData) => {
      send(input);
    },
    [send],
  );

  const handleCancel = useCallback(() => {
    cancel();
  }, [cancel]);

  const handleReconnect = useCallback(() => {
    reconnect();
  }, [reconnect]);

  const contextValue = useMemo<ChatContextValue>(
    () => ({
      config,
      onSend: handleSend,
      onCancel: handleCancel,
      onReconnect: handleReconnect,
      toolCardRegistry: registry,
      agentId,
      userInfo,
      assistantInfo,
    }),
    [
      config,
      handleSend,
      handleCancel,
      handleReconnect,
      registry,
      agentId,
      userInfo,
      assistantInfo,
    ],
  );

  return (
    <ChatContext.Provider value={contextValue}>
      <div className={`${styles.chatContainer} ${className || ""}`}>
        {children}
      </div>
    </ChatContext.Provider>
  );
};

export default ChatContainer;
