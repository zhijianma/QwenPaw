import React, { useCallback, useEffect, useRef, useState } from "react";
import { Button, Drawer, Empty, Input, List, Spin, Typography } from "antd";
import type { InputRef } from "antd";
import { IconButton } from "@agentscope-ai/design";
import {
  ChevronRight as SparkOperateRightLine,
  Search as SparkSearchLine,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { chatApi } from "../../../../api/modules/chat";
import { ApiError } from "../../../../api/request";
import type {
  ChatSearchHit,
  ChatSpec,
} from "../../../../api/types/chat";
import { buildChatPath } from "../../../../utils/sessionRoute";
import styles from "./index.module.less";

interface ChatSearchPanelProps {
  open: boolean;
  onClose: () => void;
}

interface SearchResult {
  key: string;
  chatId: string;
  chatName: string;
  turnId?: string;
  messageId?: string;
  roleLabel: string;
  matchedText: string;
  timestamp?: string | null;
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

const SEARCH_PAGE_SIZE = 50;
const TITLE_RESULT_LIMIT = 50;

const extractTextFromContent = (content: unknown): string => {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return (content as Array<{ type: string; text?: string }>)
    .filter((part) => part.type === "text" && part.text)
    .map((part) => part.text || "")
    .join("\n");
};

const getRoleLabel = (role: string | null, t: Translate): string =>
  role === "user"
    ? t("chat.search.userMessage")
    : t("chat.search.assistantMessage");

const catalogMap = (chats: ChatSpec[]): Map<string, ChatSpec> =>
  new Map(chats.filter((chat) => chat.id).map((chat) => [chat.id, chat]));

const titleResults = (
  chats: ChatSpec[],
  query: string,
  t: Translate,
): SearchResult[] => {
  const lowerQuery = query.toLowerCase();
  return chats
    .filter(
      (chat) =>
        chat.id && chat.name?.toLowerCase().includes(lowerQuery),
    )
    .slice(0, TITLE_RESULT_LIMIT)
    .map((chat) => ({
      key: `title:${chat.id}`,
      chatId: chat.id,
      chatName: chat.name || "New Chat",
      roleLabel: t("chat.search.titleMatch"),
      matchedText: chat.name || "New Chat",
      timestamp: chat.created_at,
    }));
};

const recallResults = (
  hits: ChatSearchHit[],
  chats: Map<string, ChatSpec>,
  t: Translate,
): SearchResult[] =>
  hits.map((hit) => ({
    key: `block:${hit.block_id}`,
    chatId: hit.conversation_id,
    chatName: chats.get(hit.conversation_id)?.name || "New Chat",
    turnId: hit.turn_id,
    messageId: hit.message_id,
    roleLabel: getRoleLabel(hit.role, t),
    matchedText: hit.snippet,
    timestamp: new Date(hit.created_at_us / 1_000).toISOString(),
  }));

const legacyResults = async (
  chats: ChatSpec[],
  query: string,
  signal: AbortSignal,
  t: Translate,
): Promise<SearchResult[]> => {
  const lowerQuery = query.toLowerCase();
  const results: SearchResult[] = [];
  for (const chat of chats) {
    if (!chat.id) continue;
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    try {
      const history = await chatApi.getChat(chat.id, {
        include_app_owned: false,
        signal,
      });
      for (const message of history.messages || []) {
        const text = extractTextFromContent(message.content);
        const matchIndex = text.toLowerCase().indexOf(lowerQuery);
        if (matchIndex < 0) continue;
        const start = Math.max(0, matchIndex - 80);
        const end = Math.min(text.length, matchIndex + query.length + 80);
        results.push({
          key: `legacy:${chat.id}:${String(message.id || results.length)}`,
          chatId: chat.id,
          chatName: chat.name || "New Chat",
          messageId: String(message.id || ""),
          roleLabel: getRoleLabel(String(message.role || ""), t),
          matchedText: `${start > 0 ? "..." : ""}${text.slice(start, end)}`,
          timestamp: chat.created_at,
        });
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      console.warn(`Failed to load chat ${chat.id}:`, error);
    }
  }
  return results;
};

const isLegacyFallback = (error: unknown): boolean =>
  error instanceof ApiError && (error.status === 404 || error.status === 503);

const formatTimestamp = (raw: string | null | undefined): string => {
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(
    date.getDate(),
  )} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

const ChatSearchPanel: React.FC<ChatSearchPanelProps> = ({ open, onClose }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [nextBeforeSeq, setNextBeforeSeq] = useState<number | null>(null);
  const inputRef = useRef<InputRef>(null);
  const searchTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const searchSeqRef = useRef(0);
  const catalogRef = useRef<Map<string, ChatSpec>>(new Map());

  useEffect(
    () => () => {
      if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
      abortRef.current?.abort();
    },
    [],
  );

  useEffect(() => {
    if (open) {
      const focusTimer = setTimeout(() => inputRef.current?.focus(), 100);
      return () => clearTimeout(focusTimer);
    }
    abortRef.current?.abort();
    setSearchQuery("");
    setSearchResults([]);
    setNextBeforeSeq(null);
    return undefined;
  }, [open]);

  useEffect(() => {
    const query = searchQuery.trim();
    const seq = ++searchSeqRef.current;
    if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    abortRef.current?.abort();
    if (!query) {
      setSearchResults([]);
      setNextBeforeSeq(null);
      setLoading(false);
      return undefined;
    }

    searchTimeoutRef.current = setTimeout(async () => {
      const controller = new AbortController();
      abortRef.current = controller;
      setLoading(true);
      setNextBeforeSeq(null);
      try {
        const chats = await chatApi.listChats({
          archived: false,
          include_app_owned: false,
        });
        if (seq !== searchSeqRef.current || controller.signal.aborted) return;
        catalogRef.current = catalogMap(chats);
        const titles = titleResults(chats, query, t);
        try {
          const page = await chatApi.searchChats(query, {
            limit: SEARCH_PAGE_SIZE,
            signal: controller.signal,
          });
          if (seq !== searchSeqRef.current || controller.signal.aborted) return;
          setSearchResults([
            ...titles,
            ...recallResults(page.items, catalogRef.current, t),
          ]);
          setNextBeforeSeq(page.next_before_seq);
        } catch (error) {
          if (!isLegacyFallback(error)) throw error;
          const messages = await legacyResults(
            chats,
            query,
            controller.signal,
            t,
          );
          if (seq !== searchSeqRef.current || controller.signal.aborted) return;
          setSearchResults([...titles, ...messages]);
        }
      } catch (error) {
        if (
          seq === searchSeqRef.current &&
          !(error instanceof DOMException && error.name === "AbortError")
        ) {
          console.error("Search failed:", error);
          setSearchResults([]);
        }
      } finally {
        if (seq === searchSeqRef.current) setLoading(false);
      }
    }, 300);

    return () => {
      if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current);
    };
  }, [searchQuery, t]);

  const loadMore = useCallback(async () => {
    const query = searchQuery.trim();
    if (!query || nextBeforeSeq === null || loadingMore) return;
    const seq = searchSeqRef.current;
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setLoadingMore(true);
    try {
      const page = await chatApi.searchChats(query, {
        beforeSeq: nextBeforeSeq,
        limit: SEARCH_PAGE_SIZE,
        signal: controller.signal,
      });
      if (seq !== searchSeqRef.current || controller.signal.aborted) return;
      const additional = recallResults(page.items, catalogRef.current, t);
      setSearchResults((current) => {
        const existing = new Set(current.map((item) => item.key));
        return [
          ...current,
          ...additional.filter((item) => !existing.has(item.key)),
        ];
      });
      setNextBeforeSeq(page.next_before_seq);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        console.error("Failed to load more Chat search results:", error);
      }
    } finally {
      if (seq === searchSeqRef.current) setLoadingMore(false);
    }
  }, [loadingMore, nextBeforeSeq, searchQuery, t]);

  const handleResultClick = useCallback(
    (result: SearchResult) => {
      const path = buildChatPath(result.chatId);
      if (result.turnId) {
        const params = new URLSearchParams({ turn_id: result.turnId });
        if (result.messageId) params.set("message_id", result.messageId);
        navigate(`${path}?${params}`);
      } else {
        navigate(path);
      }
      onClose();
    },
    [navigate, onClose],
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      destroyOnHidden
      placement="right"
      width="calc(100vw - 56px)"
      closable={false}
      title={null}
      styles={{
        header: { display: "none" },
        body: {
          padding: 0,
          display: "flex",
          flexDirection: "column",
          height: "100%",
          overflow: "hidden",
        },
        mask: { background: "transparent" },
      }}
      className={styles.drawer}
    >
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.headerTitle}>{t("chat.search.title")}</span>
        </div>
        <div className={styles.headerRight}>
          <IconButton
            bordered={false}
            icon={<SparkOperateRightLine size="1em" />}
            onClick={onClose}
          />
        </div>
      </div>

      <div className={styles.searchSection}>
        <Input
          ref={inputRef}
          placeholder={t("chat.search.placeholder")}
          prefix={
            <SparkSearchLine size="1em" style={{ color: "rgba(0,0,0,0.25)" }} />
          }
          value={searchQuery}
          onChange={(event) => setSearchQuery(event.target.value)}
          allowClear
          className={styles.searchInput}
        />
      </div>

      {searchQuery.trim() && (
        <div className={styles.resultsCount}>
          <Typography.Text type="secondary">
            {loading
              ? t("chat.search.loading")
              : t("chat.search.resultsCount", {
                  count: searchResults.length,
                })}
          </Typography.Text>
        </div>
      )}

      <div className={styles.listWrapper}>
        <div className={styles.topGradient} />
        <div className={styles.list}>
          {loading && searchResults.length === 0 ? (
            <div className={styles.loadingState}>
              <Spin />
            </div>
          ) : searchQuery.trim() && !loading && searchResults.length === 0 ? (
            <Empty
              description={t("chat.search.noResults")}
              style={{ marginTop: 40 }}
            />
          ) : (
            <>
              <List
                rowKey="key"
                dataSource={searchResults}
                renderItem={(item) => (
                  <div
                    className={styles.searchResultItem}
                    onClick={() => handleResultClick(item)}
                  >
                    <div className={styles.resultHeader}>
                      <span className={styles.resultChatName}>
                        {item.chatName}
                      </span>
                      <span className={styles.resultRole}>
                        {item.roleLabel}
                      </span>
                    </div>
                    <div className={styles.resultContent}>
                      <Typography.Text ellipsis style={{ fontSize: 13 }}>
                        {item.matchedText}
                      </Typography.Text>
                    </div>
                    {item.timestamp && (
                      <div className={styles.resultTime}>
                        {formatTimestamp(item.timestamp)}
                      </div>
                    )}
                  </div>
                )}
              />
              {nextBeforeSeq !== null && (
                <Button
                  type="text"
                  loading={loadingMore}
                  onClick={() => void loadMore()}
                  className={styles.loadMore}
                >
                  {t("chat.search.loadMore")}
                </Button>
              )}
            </>
          )}
        </div>
        <div className={styles.bottomGradient} />
      </div>
    </Drawer>
  );
};

export default ChatSearchPanel;
