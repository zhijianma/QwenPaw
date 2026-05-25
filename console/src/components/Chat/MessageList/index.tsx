import React, { useCallback, useEffect, useMemo, useRef } from "react";
import { useChatStore } from "../stores/chatStore";
import MessageItem from "./MessageItem";
import styles from "./MessageList.module.less";

const EMPTY_MESSAGES: never[] = [];

export interface MessageListProps {
  /** Custom welcome screen when no messages */
  welcome?: React.ReactNode;
  /** Called when a prompt from welcome is clicked */
  onPromptClick?: (prompt: string) => void;
  /** Whether to auto-scroll on new messages */
  autoScroll?: boolean;
}

const MessageList: React.FC<MessageListProps> = ({
  welcome,
  autoScroll = true,
}) => {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const messagesMap = useChatStore((s) => s.messages);
  const messages = useMemo(
    () =>
      activeSessionId
        ? messagesMap[activeSessionId] || EMPTY_MESSAGES
        : EMPTY_MESSAGES,
    [activeSessionId, messagesMap],
  );
  const streamingMessageId = useChatStore((s) => s.streamingMessageId);
  const containerRef = useRef<HTMLDivElement>(null);
  const isUserScrollRef = useRef(false);

  // Auto-scroll to bottom on new messages
  const scrollToBottom = useCallback(() => {
    if (!autoScroll || isUserScrollRef.current) return;
    const container = containerRef.current;
    if (container) {
      container.scrollTop = container.scrollHeight;
    }
  }, [autoScroll]);

  useEffect(() => {
    scrollToBottom();
  }, [messages.length, streamingMessageId, scrollToBottom]);

  // Detect user scroll
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      const isAtBottom = scrollHeight - scrollTop - clientHeight < 50;
      isUserScrollRef.current = !isAtBottom;
    };

    container.addEventListener("scroll", handleScroll);
    return () => container.removeEventListener("scroll", handleScroll);
  }, []);

  // Show welcome when no messages
  if (!activeSessionId || messages.length === 0) {
    if (welcome) {
      return <div className={styles.messageList}>{welcome}</div>;
    }
    return (
      <div className={styles.messageList}>
        <div className={styles.welcome}>
          <div className={styles.welcomeAvatar}>
            <img src="/qwenpaw.png" alt="QwenPaw" />
          </div>
          <div className={styles.welcomeGreeting}>Hi, how can I help you?</div>
          <div className={styles.welcomeDescription}>
            I&apos;m your AI assistant. Ask me anything.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div ref={containerRef} className={styles.messageList}>
      {messages.map((msg, idx) => (
        <div key={msg.id} className={styles.messageItem}>
          <MessageItem
            message={msg}
            index={idx}
            isLast={idx === messages.length - 1}
            isStreaming={msg.id === streamingMessageId}
          />
        </div>
      ))}
    </div>
  );
};

export default MessageList;
