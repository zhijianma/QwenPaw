import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button, Modal, Result } from "antd";
import {
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ExclamationCircleOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import ModelSelector from "../Chat/ModelSelector";
import {
  ChatContainer,
  MessageList,
  MessageInput,
  SessionPanel,
} from "../../components/Chat";
import type { ChatConfig, CommandSuggestion } from "../../components/Chat";
import { getApiUrl } from "../../api/config";
import { buildAuthHeaders } from "../../api/authHeaders";
import { chatApi } from "../../api/modules/chat";
import { useAgentStore } from "../../stores/agentStore";
import { usePlugins } from "../../plugins/PluginContext";
import { useTheme } from "../../contexts/ThemeContext";
import { getAgentDisplayName } from "../../utils/agentDisplayName";
import WhisperSpeechButton from "../Chat/components/WhisperSpeechButton";
import type {
  UserDisplayInfo,
  AssistantDisplayInfo,
} from "../../components/Chat/context/ChatContext";
import { useMultimodalCapabilities } from "./hooks/useMultimodalCapabilities";
import { useModelCheck } from "./hooks/useModelCheck";
import { useWhisperSpeech } from "./hooks/useWhisperSpeech";
import { useChatV2Router } from "./hooks/useChatV2Router";
import styles from "./index.module.less";

export default function ChatV2Page() {
  const { t } = useTranslation();
  const { isDark } = useTheme();
  const { selectedAgent, agents } = useAgentStore();
  const { toolRenderConfig } = usePlugins();
  const [sessionPanelCollapsed, setSessionPanelCollapsed] = useState(false);

  // Custom hooks
  const { navigate } = useChatV2Router(selectedAgent);
  const { whisperEnabled, whisperSpeechRef, handleTranscription } =
    useWhisperSpeech();
  const multimodalCaps = useMultimodalCapabilities(selectedAgent);
  const { showPrompt: showModelPrompt, setShowPrompt: setShowModelPrompt } =
    useModelCheck(selectedAgent);

  // Chat config
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

  // Agent display info
  const currentAgent = useMemo(
    () => agents.find((a) => a.id === selectedAgent),
    [agents, selectedAgent],
  );

  const userInfo = useMemo<UserDisplayInfo>(
    () => ({ name: chatConfig.userId || "User" }),
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

  // Commands
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

  const handleUpload = useCallback(async (file: File) => {
    const res = await chatApi.uploadFile(file);
    return { url: chatApi.filePreviewUrl(res.url) };
  }, []);

  const handleError = useCallback((error: Error) => {
    console.error("[ChatV2] Stream error:", error);
  }, []);

  const whisperPrefix = whisperEnabled ? (
    <WhisperSpeechButton
      ref={whisperSpeechRef}
      onTranscription={handleTranscription}
    />
  ) : undefined;

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
            prefix={whisperPrefix}
            allowSpeech={!whisperEnabled}
            supportsMultimodal={multimodalCaps.supportsMultimodal}
          />
        </div>
      </ChatContainer>

      <Modal
        open={showModelPrompt}
        closable={false}
        footer={null}
        width={480}
        styles={{
          content: isDark
            ? { background: "#1f1f1f", boxShadow: "0 8px 32px rgba(0,0,0,0.5)" }
            : undefined,
        }}
      >
        <Result
          icon={<ExclamationCircleOutlined style={{ color: "#faad14" }} />}
          title={
            <span
              style={{ color: isDark ? "rgba(255,255,255,0.88)" : undefined }}
            >
              {t("modelConfig.promptTitle")}
            </span>
          }
          subTitle={
            <span
              style={{ color: isDark ? "rgba(255,255,255,0.55)" : undefined }}
            >
              {t("modelConfig.promptMessage")}
            </span>
          }
          extra={[
            <Button key="skip" onClick={() => setShowModelPrompt(false)}>
              {t("modelConfig.skipButton")}
            </Button>,
            <Button
              key="configure"
              type="primary"
              icon={<SettingOutlined />}
              onClick={() => {
                setShowModelPrompt(false);
                navigate("/models");
              }}
            >
              {t("modelConfig.configureButton")}
            </Button>,
          ]}
        />
      </Modal>
    </div>
  );
}
