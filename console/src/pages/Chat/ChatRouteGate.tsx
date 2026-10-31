import { Suspense, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation } from "react-router-dom";
import { chatApi } from "../../api/modules/chat";
import { useAgentStore } from "../../stores/agentStore";
import { getSessionIdFromPath } from "../../utils/sessionRoute";
import { lazyWithRetry } from "../../utils/lazyWithRetry";
import { prefetchChatHistory } from "./chatRoutePrefetch";
import styles from "./ChatRouteGate.module.less";

const loadChatPage = () => import("./index");
const ChatPage = lazyWithRetry(loadChatPage, "Chat/index");

type GateState = "resolving" | "ready" | "error";

export default function ChatRouteGate() {
  const { t } = useTranslation();
  const location = useLocation();
  const initialReference = useRef(
    getSessionIdFromPath(location.pathname),
  ).current;
  const needsResolution =
    initialReference !== undefined && initialReference !== "new";
  const [state, setState] = useState<GateState>(
    needsResolution ? "resolving" : "ready",
  );
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!needsResolution || !initialReference) return;

    const controller = new AbortController();
    const resolveRoute = async () => {
      setState("resolving");
      try {
        const currentAgentId = useAgentStore.getState().selectedAgent;
        const { agent_id: agentId, chat } = await chatApi.resolveChat(
          initialReference,
          {
            currentAgentId,
            signal: controller.signal,
          },
        );
        if (controller.signal.aborted) return;

        if (agentId !== currentAgentId) {
          useAgentStore.getState().setSelectedAgent(agentId);
        }
        const historyPromise = prefetchChatHistory(
          chat.id,
          agentId,
          controller.signal,
        );
        if (chat.status === "running") {
          const [{ default: sessionApi }] = await Promise.all([
            import("./sessionApi"),
            loadChatPage(),
          ]);
          if (controller.signal.aborted) return;
          sessionApi.registerResolvedChat(chat, sessionApi.getActiveOwner());
          setState("ready");
          return;
        }

        const [{ default: sessionApi }] = await Promise.all([
          import("./sessionApi"),
          loadChatPage(),
          historyPromise,
        ]);
        if (controller.signal.aborted) return;
        sessionApi.registerResolvedChat(chat, sessionApi.getActiveOwner());
        setState("ready");
      } catch (error) {
        if (
          controller.signal.aborted ||
          (error instanceof DOMException && error.name === "AbortError")
        ) {
          return;
        }
        setState("error");
      }
    };

    void resolveRoute();
    return () => controller.abort();
  }, [attempt, initialReference, needsResolution]);

  if (state === "error") {
    return (
      <div className={styles.gate} role="alert">
        <div className={styles.error}>
          <span>{t("chat.sessionLoadFailed")}</span>
          <button
            className={styles.retry}
            type="button"
            onClick={() => setAttempt((value) => value + 1)}
          >
            {t("common.retry")}
          </button>
        </div>
      </div>
    );
  }

  if (state === "resolving") {
    return (
      <div className={styles.gate} role="status">
        {t("common.loading")}
      </div>
    );
  }

  return (
    <Suspense
      fallback={
        <div className={styles.gate} role="status">
          {t("common.loading")}
        </div>
      }
    >
      <ChatPage />
    </Suspense>
  );
}
