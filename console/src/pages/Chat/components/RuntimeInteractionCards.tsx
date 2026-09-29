import { Button, Card, Input, Space, Typography } from "antd";
import { Lightbulb, MessageCircleQuestion, ShieldCheck } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { chatApi } from "../../../api/modules/chat";
import type { ChatInteraction } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useConversationRuntimeProjection } from "../runtimeProjectionStore";
import styles from "./RuntimeInteractionCards.module.less";

function interactionIcon(kind: ChatInteraction["kind"]) {
  if (kind === "approval") return <ShieldCheck size={17} />;
  if (kind === "suggestion") return <Lightbulb size={17} />;
  return <MessageCircleQuestion size={17} />;
}

interface RuntimeInteractionCardsProps {
  active: boolean;
  agentId: string;
  chatId?: string;
  hasLegacyApprovals: boolean;
  hiddenInteractionIds: ReadonlySet<string>;
}

export function RuntimeInteractionCards({
  active,
  agentId,
  chatId,
  hasLegacyApprovals,
  hiddenInteractionIds,
}: RuntimeInteractionCardsProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { projection, refresh } = useConversationRuntimeProjection({
    active,
    agentId,
    chatId,
  });
  const interactions = projection?.interactions ?? [];
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const decisionKeysRef = useRef(new Map<string, string>());

  const loadOpen = useCallback(async () => {
    await refresh();
  }, [refresh]);

  const respond = useCallback(
    async (
      interaction: ChatInteraction,
      response: {
        selected_option_ids?: string[];
        text?: string;
        values?: Record<string, unknown>;
      },
    ) => {
      if (!chatId || submittingId) return;
      const interactionId = interaction.interaction_id;
      const idempotencyKey =
        decisionKeysRef.current.get(interactionId) ?? crypto.randomUUID();
      decisionKeysRef.current.set(interactionId, idempotencyKey);
      setSubmittingId(interactionId);
      try {
        await chatApi.respondToInteraction(chatId, interactionId, {
          idempotency_key: idempotencyKey,
          expected_revision: interaction.revision,
          ...response,
        });
        decisionKeysRef.current.delete(interactionId);
        await loadOpen();
      } catch (error) {
        message.error(
          error instanceof Error
            ? error.message
            : t("chat.interaction.responseFailed"),
        );
      } finally {
        setSubmittingId(null);
      }
    },
    [chatId, loadOpen, message, submittingId, t],
  );

  const visible = interactions.filter(
    (item) => !hiddenInteractionIds.has(item.interaction_id),
  );
  if (!visible.length) return null;

  return (
    <div
      className={styles.stack}
      data-has-legacy-approval={hasLegacyApprovals || undefined}
      aria-live="polite"
    >
      {visible.map((interaction) => {
        const isSubmitting = submittingId === interaction.interaction_id;
        const answer = answers[interaction.interaction_id] ?? "";
        return (
          <Card
            key={interaction.interaction_id}
            className={styles.card}
            size="small"
            title={
              <span className={styles.title}>
                {interactionIcon(interaction.kind)}
                {interaction.title}
              </span>
            }
          >
            <Typography.Paragraph className={styles.prompt}>
              {interaction.prompt}
            </Typography.Paragraph>

            {interaction.options.length > 0 ? (
              <Space wrap className={styles.actions}>
                {interaction.options.map((option) => (
                  <Button
                    key={option.option_id}
                    danger={option.option_id === "deny"}
                    type={
                      option.option_id.startsWith("approve")
                        ? "primary"
                        : "default"
                    }
                    loading={isSubmitting}
                    disabled={Boolean(submittingId) && !isSubmitting}
                    onClick={() =>
                      respond(interaction, {
                        selected_option_ids: [option.option_id],
                      })
                    }
                  >
                    {option.label}
                  </Button>
                ))}
              </Space>
            ) : interaction.kind === "suggestion" ? (
              <Button
                loading={isSubmitting}
                onClick={() =>
                  respond(interaction, { values: { dismissed: true } })
                }
              >
                {t("chat.interaction.dismiss")}
              </Button>
            ) : (
              <Space.Compact className={styles.answerRow}>
                <Input
                  value={answer}
                  placeholder={t("chat.interaction.answerPlaceholder")}
                  disabled={Boolean(submittingId)}
                  onChange={(event) =>
                    setAnswers((current) => ({
                      ...current,
                      [interaction.interaction_id]: event.target.value,
                    }))
                  }
                  onPressEnter={() => {
                    if (answer.trim()) {
                      void respond(interaction, { text: answer.trim() });
                    }
                  }}
                />
                <Button
                  type="primary"
                  loading={isSubmitting}
                  disabled={!answer.trim() || Boolean(submittingId)}
                  onClick={() => respond(interaction, { text: answer.trim() })}
                >
                  {t("chat.interaction.submit")}
                </Button>
              </Space.Compact>
            )}
          </Card>
        );
      })}
    </div>
  );
}

export default RuntimeInteractionCards;
