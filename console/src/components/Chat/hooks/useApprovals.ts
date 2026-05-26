import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApprovalContext } from "../../../contexts/ApprovalContext";
import { commandsApi } from "../../../api/modules/commands";
import { chatApi } from "../../../api/modules/chat";
import { useAppMessage } from "../../../hooks/useAppMessage";

export interface ApprovalMessageData {
  requestId: string;
  sessionId: string;
  rootSessionId?: string;
  agentId: string;
  toolName: string;
  severity: string;
  findingsCount: number;
  findingsSummary: string;
  toolParams: Record<string, unknown>;
  createdAt: number;
  timeoutSeconds: number;
}

export function useApprovals(
  chatId: string | undefined,
  activeSessionId: string | null,
) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { approvals, setApprovals } = useApprovalContext();
  const [approvalRequests, setApprovalRequests] = useState<
    Map<string, ApprovalMessageData>
  >(new Map());
  const prevApprovalKeyRef = useRef("");

  // Filter approvals for current session
  useEffect(() => {
    const currentSessionId = activeSessionId || chatId || "";

    let effectiveSessionId = currentSessionId;
    if (!effectiveSessionId && approvals.length > 0) {
      effectiveSessionId = approvals[0].root_session_id;
    }

    const sessionApprovals = effectiveSessionId
      ? approvals.filter(
          (approval) => approval.root_session_id === effectiveSessionId,
        )
      : approvals;

    const approvalKey = sessionApprovals
      .map((a) => a.request_id)
      .sort()
      .join(",");

    if (approvalKey === prevApprovalKeyRef.current) return;
    prevApprovalKeyRef.current = approvalKey;

    const newMap = new Map<string, ApprovalMessageData>();
    for (const approval of sessionApprovals) {
      newMap.set(approval.request_id, {
        requestId: approval.request_id,
        sessionId: approval.session_id,
        rootSessionId: approval.root_session_id,
        agentId: approval.agent_id,
        toolName: approval.tool_name,
        severity: approval.severity,
        findingsCount: approval.findings_count,
        findingsSummary: approval.findings_summary,
        toolParams: approval.tool_params,
        createdAt: approval.created_at,
        timeoutSeconds: approval.timeout_seconds,
      });
    }

    setApprovalRequests(newMap);
  }, [approvals, chatId, activeSessionId]);

  const handleApprove = useCallback(
    async (requestId: string) => {
      const rootSessionId = activeSessionId || chatId || "";

      try {
        const cardElement = document.querySelector(
          `[data-approval-id="${requestId}"]`,
        );
        if (cardElement) {
          cardElement.classList.add("approvalCardExit");
        }

        await commandsApi.sendApprovalCommand(
          "approve",
          requestId,
          rootSessionId,
        );
        setApprovals((prev) =>
          prev.filter((item) => item.request_id !== requestId),
        );
        message.success(t("approval.approved"));

        setTimeout(() => {
          setApprovalRequests((prev) => {
            const next = new Map(prev);
            next.delete(requestId);
            return next;
          });
        }, 300);
      } catch (error) {
        message.error(t("approval.approveFailed"));
        console.error("Failed to approve:", error);
      }
    },
    [activeSessionId, chatId, t, message, setApprovals],
  );

  const handleDeny = useCallback(
    async (requestId: string) => {
      const rootSessionId = activeSessionId || chatId || "";

      try {
        const cardElement = document.querySelector(
          `[data-approval-id="${requestId}"]`,
        );
        if (cardElement) {
          cardElement.classList.add("approvalCardExit");
        }

        await commandsApi.sendApprovalCommand("deny", requestId, rootSessionId);
        setApprovals((prev) =>
          prev.filter((item) => item.request_id !== requestId),
        );
        message.success(t("approval.denied"));

        setTimeout(() => {
          setApprovalRequests((prev) => {
            const next = new Map(prev);
            next.delete(requestId);
            return next;
          });
        }, 300);
      } catch (error) {
        message.error(t("approval.denyFailed"));
        console.error("Failed to deny:", error);
      }
    },
    [activeSessionId, chatId, t, message, setApprovals],
  );

  const handleCancel = useCallback(
    (rootSessionId: string | undefined) => {
      const resolvedChatId = activeSessionId || chatId || "";
      if (!resolvedChatId) {
        console.warn("[Chat] No chat_id resolved, cannot cancel task");
        return;
      }

      chatApi
        .stopChat(resolvedChatId)
        .then(() => {
          setApprovals((prev) =>
            prev.filter((item) => item.root_session_id !== rootSessionId),
          );
        })
        .catch((err) => {
          console.error("[Chat] stopChat failed:", err);
        });
    },
    [activeSessionId, chatId, setApprovals],
  );

  return {
    approvalRequests,
    handleApprove,
    handleDeny,
    handleCancel,
  };
}
