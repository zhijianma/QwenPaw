import { useEffect, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useChatStore, useSessionStore } from "../../../components/Chat";
import { useAgentStore } from "../../../stores/agentStore";

export function useChatV2Router(selectedAgent: string) {
  const { chatId } = useParams<{ chatId?: string }>();
  const navigate = useNavigate();
  const { setLastChatId, getLastChatId } = useAgentStore();

  const setActiveSession = useChatStore((s) => s.setActiveSession);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const loadSessions = useSessionStore((s) => s.loadSessions);

  // Load sessions on mount and when agent changes
  useEffect(() => { loadSessions(); }, [loadSessions, selectedAgent]);

  // URL → state
  useEffect(() => {
    const target = chatId || null;
    if (target !== activeSessionId) setActiveSession(target);
  }, [chatId]); // eslint-disable-line react-hooks/exhaustive-deps

  // State → URL
  useEffect(() => {
    const urlChatId = chatId || null;
    if (activeSessionId !== urlChatId) {
      const path = activeSessionId ? `/chat-v2/${activeSessionId}` : "/chat-v2";
      navigate(path, { replace: true });
    }
  }, [activeSessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Agent switch: preserve session per agent
  const prevAgentRef = useRef(selectedAgent);
  useEffect(() => {
    const prev = prevAgentRef.current;
    if (prev !== selectedAgent && prev !== undefined) {
      if (activeSessionId && prev) setLastChatId(prev, activeSessionId);

      const restored = getLastChatId(selectedAgent);
      if (restored) {
        navigate(`/chat-v2/${restored}`, { replace: true });
      } else {
        navigate("/chat-v2", { replace: true });
      }
    }
    prevAgentRef.current = selectedAgent;
  }, [selectedAgent]); // eslint-disable-line react-hooks/exhaustive-deps

  return { chatId, navigate };
}
