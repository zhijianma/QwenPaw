import {
  ChevronDown,
  ChevronUp,
  CircleCheck,
  CircleX,
  Clock3,
  FileCheck2,
  GitBranch,
  Hand,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  ConversationExecutionChain,
  RuntimeObservation,
  RuntimeObservationCategory,
} from "../../../api/types";
import { useConversationRuntimeProjection } from "../runtimeProjectionStore";
import styles from "./RuntimeActivityPanel.module.less";

interface RuntimeActivityPanelProps {
  active: boolean;
  agentId: string;
  chatId?: string;
}

const MAX_VISIBLE_ACTIVITY = 5;

function latestChain(
  chains: ConversationExecutionChain[],
): ConversationExecutionChain | undefined {
  return [...chains].sort(
    (left, right) =>
      Date.parse(right.latest_submission_at) -
      Date.parse(left.latest_submission_at),
  )[0];
}

function isMeaningfulActivity(observation: RuntimeObservation): boolean {
  if (observation.category === "compaction") return false;
  if (observation.source.source_type === "qwenpaw.control.submission") {
    return false;
  }
  if (observation.category !== "model") return true;
  return (
    observation.source.source_type === "qwenpaw.model.resource-wait" ||
    observation.source.source_type === "qwenpaw.model.step-continuation" ||
    ["blocked", "failed", "partial"].includes(observation.status)
  );
}

function categoryIcon(category: RuntimeObservationCategory) {
  if (category === "action") return <Wrench aria-hidden size={15} />;
  if (category === "guardrail") {
    return <ShieldCheck aria-hidden size={15} />;
  }
  if (category === "hitl") return <Hand aria-hidden size={15} />;
  if (category === "artifact" || category === "evidence") {
    return <FileCheck2 aria-hidden size={15} />;
  }
  if (category === "verification" || category === "outcome") {
    return <CircleCheck aria-hidden size={15} />;
  }
  if (category === "interrupt") return <CircleX aria-hidden size={15} />;
  if (category === "control") return <GitBranch aria-hidden size={15} />;
  return <Clock3 aria-hidden size={15} />;
}

function shortSource(observation: RuntimeObservation): string {
  const sourceId = observation.source.source_id;
  const compactId =
    sourceId.length > 10 ? sourceId.slice(sourceId.length - 8) : sourceId;
  return `${observation.source.source_type} · ${compactId}`;
}

export default function RuntimeActivityPanel({
  active,
  agentId,
  chatId,
}: RuntimeActivityPanelProps) {
  const { t } = useTranslation();
  const { projection } = useConversationRuntimeProjection({
    active,
    agentId,
    chatId,
  });
  const [expanded, setExpanded] = useState(true);
  const chain = useMemo(
    () => latestChain(projection?.execution_chains ?? []),
    [projection?.execution_chains],
  );
  const activity = useMemo(() => {
    const correlationId = chain?.correlation_id;
    return (projection?.activity?.items ?? [])
      .filter(
        (item) =>
          isMeaningfulActivity(item) &&
          (!correlationId || item.correlation_id === correlationId),
      )
      .sort(
        (left, right) =>
          Date.parse(right.occurred_at) - Date.parse(left.occurred_at),
      )
      .slice(0, MAX_VISIBLE_ACTIVITY);
  }, [chain?.correlation_id, projection?.activity?.items]);

  if (!activity.length) return null;

  const state = chain?.state ?? "inactive";
  return (
    <section
      className={styles.panel}
      aria-label={t("chat.activity.title")}
      data-state={state}
    >
      <button
        type="button"
        className={styles.header}
        aria-expanded={expanded}
        onClick={() => setExpanded((current) => !current)}
      >
        <span className={styles.heading}>
          <GitBranch aria-hidden size={16} />
          <span>{t("chat.activity.title")}</span>
          <span className={styles.count}>{activity.length}</span>
        </span>
        <span className={styles.state}>
          {t(`chat.activity.states.${state}`)}
          {expanded ? (
            <ChevronUp aria-hidden size={15} />
          ) : (
            <ChevronDown aria-hidden size={15} />
          )}
        </span>
      </button>

      {expanded ? (
        <ol className={styles.list} aria-live="polite">
          {activity.map((item) => (
            <li className={styles.item} key={item.observation_id}>
              <span className={styles.icon} data-status={item.status}>
                {categoryIcon(item.category)}
              </span>
              <span className={styles.content}>
                <span className={styles.itemHeader}>
                  <span className={styles.itemTitle}>{item.title}</span>
                  <span className={styles.status}>
                    {t(`chat.activity.statuses.${item.status}`)}
                  </span>
                </span>
                <span
                  className={styles.source}
                  title={`${item.source.source_type} · ${item.source.source_id}`}
                >
                  {shortSource(item)}
                </span>
              </span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}
