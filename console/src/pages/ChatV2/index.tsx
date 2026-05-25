import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams, useNavigate } from "react-router-dom";
import { Button } from "antd";
import { MenuFoldOutlined, MenuUnfoldOutlined } from "@ant-design/icons";
import ModelSelector from "../Chat/ModelSelector";
import {
  ChatContainer,
  MessageList,
  MessageInput,
  SessionPanel,
  useChatStore,
  useSessionStore,
} from "../../components/Chat";
import type { ChatConfig, CommandSuggestion } from "../../components/Chat";
import { getApiUrl } from "../../api/config";
import { buildAuthHeaders } from "../../api/authHeaders";
import { chatApi } from "../../api/modules/chat";
import { useAgentStore } from "../../stores/agentStore";
import { usePlugins } from "../../plugins/PluginContext";
import { useTheme } from "../../contexts/ThemeContext";
import { getAgentDisplayName } from "../../utils/agentDisplayName";
import type {
  UserDisplayInfo,
  AssistantDisplayInfo,
} from "../../components/Chat/context/ChatContext";
import styles from "./index.module.less";

export default function ChatV2Page() {
  const { t } = useTranslation();
  const { isDark } = useTheme();
  const { selectedAgent, agents } = useAgentStore();
  const { toolRenderConfig } = usePlugins();
  const [sessionPanelCollapsed, setSessionPanelCollapsed] = useState(false);
  const { chatId } = useParams<{ chatId?: string }>();
  const navigate = useNavigate();

  // Build chat config
  const chatConfig = useMemo<ChatConfig>(
    () => ({
      apiEndpoint: getApiUrl("/console/chat"),
      headers: buildAuthHeaders(),
      enableStream: true,
      enableReconnect: true,
      maxAttachmentSize: 10,
      supportedMediaTypes: ["image/*", "video/*", "audio/*", "application/pdf"],
      userId: "default",
      channel: "console",
    }),
    [],
  );

  // Resolve agent display info
  const currentAgent = useMemo(
    () => agents.find((a) => a.id === selectedAgent),
    [agents, selectedAgent],
  );

  const userInfo = useMemo<UserDisplayInfo>(
    () => ({
      name: chatConfig.userId || "User",
    }),
    [chatConfig.userId],
  );

  const assistantInfo = useMemo<AssistantDisplayInfo>(
    () => ({
      name: currentAgent ? getAgentDisplayName(currentAgent, t) : "QwenPaw",
      avatar: "/qwenpaw.png",
      model: currentAgent?.active_model?.model,
    }),
    [currentAgent, t],
  );

  // Command suggestions
  const commands = useMemo<CommandSuggestion[]>(
    () => [
      {
        command: "/clear",
        value: "clear",
        description: t("chat.commands.clear.description", "Clear chat history"),
      },
      {
        command: "/compact",
        value: "compact",
        description: t("chat.commands.compact.description", "Compact mode"),
      },
      {
        command: "/mission",
        value: "mission",
        description: t(
          "chat.commands.mission.description",
          "Show agent mission",
        ),
      },
      {
        command: "/skills",
        value: "skills",
        description: t(
          "chat.commands.skills.description",
          "Show available skills",
        ),
      },
    ],
    [t],
  );

  // File upload handler
  const handleUpload = useCallback(async (file: File) => {
    const res = await chatApi.uploadFile(file);
    return { url: chatApi.filePreviewUrl(res.url) };
  }, []);

  // Error handler
  const handleError = useCallback((error: Error) => {
    console.error("[ChatV2] Stream error:", error);
  }, []);

  // Load sessions on mount
  const loadSessions = useSessionStore((s) => s.loadSessions);
  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  // Sync URL chatId ↔ activeSessionId
  const setActiveSession = useChatStore((s) => s.setActiveSession);
  const activeSessionId = useChatStore((s) => s.activeSessionId);

  // URL → state
  useEffect(() => {
    const target = chatId || null;
    if (target !== activeSessionId) {
      setActiveSession(target);
    }
  }, [chatId]); // eslint-disable-line react-hooks/exhaustive-deps

  // State → URL
  useEffect(() => {
    const urlChatId = chatId || null;
    if (activeSessionId !== urlChatId) {
      const path = activeSessionId ? `/chat-v2/${activeSessionId}` : "/chat-v2";
      navigate(path, { replace: true });
    }
  }, [activeSessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className={`${styles.chatV2Page} ${isDark ? styles.dark : ""}`}>
      <ChatContainer
        config={chatConfig}
        agentId={selectedAgent}
        toolCards={toolRenderConfig}
        userInfo={userInfo}
        assistantInfo={assistantInfo}
        onError={handleError}
      >
        <SessionPanel
          collapsed={sessionPanelCollapsed}
          onToggleCollapse={() =>
            setSessionPanelCollapsed(!sessionPanelCollapsed)
          }
        />
        <div className={styles.chatMain}>
          <div className={styles.chatHeader}>
            <Button
              type="text"
              size="small"
              icon={
                sessionPanelCollapsed ? (
                  <MenuUnfoldOutlined />
                ) : (
                  <MenuFoldOutlined />
                )
              }
              onClick={() => setSessionPanelCollapsed(!sessionPanelCollapsed)}
              className={styles.collapseBtn}
            />
            <ModelSelector />
          </div>
          <MessageList />
          <MessageInput
            placeholder={t("chat.inputPlaceholder", "Ask me anything...")}
            commands={commands}
            enableAttachments
            onUpload={handleUpload}
            maxFileSize={10}
          />
        </div>
      </ChatContainer>
    </div>
  );
}
