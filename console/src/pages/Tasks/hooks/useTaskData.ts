import { useCallback, useEffect, useState } from "react";
import {
  tasksApi,
  type LiteTask,
  type TaskApproval,
  type TaskArtifact,
  type TaskCapability,
  type TaskConversationMessage,
  type TaskEvent,
  type TaskEvidence,
  type TaskPlan,
  type TaskRun,
} from "../../../api/modules/tasks";

export function useTaskData(selectedId: string | null) {
  const [tasks, setTasks] = useState<LiteTask[]>([]);
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [plan, setPlan] = useState<TaskPlan | null>(null);
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [approvals, setApprovals] = useState<TaskApproval[]>([]);
  const [conversation, setConversation] = useState<TaskConversationMessage[]>(
    [],
  );
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([]);
  const [evidence, setEvidence] = useState<TaskEvidence[]>([]);
  const [capabilities, setCapabilities] = useState<TaskCapability[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refreshList = useCallback(async (signal?: AbortSignal) => {
    const page = await tasksApi.list(undefined, signal);
    setTasks(page.items);
    return page.items;
  }, []);

  const refreshDetail = useCallback(
    async (signal?: AbortSignal) => {
      if (!selectedId) return;
      const [projection, timeline] = await Promise.all([
        tasksApi.projection(selectedId, signal),
        tasksApi.events(selectedId, signal),
      ]);
      setRuns(projection.runs);
      setPlan(projection.latest_plan);
      setEvents(timeline.items);
      setApprovals(projection.pending_approvals);
      setConversation(projection.conversation_messages);
      setArtifacts(projection.artifacts);
      setEvidence(projection.evidence);
      setCapabilities(projection.capabilities);
    },
    [selectedId],
  );

  const refresh = useCallback(async () => {
    setError(null);
    try {
      await Promise.all([refreshList(), refreshDetail()]);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Unable to load tasks",
      );
    } finally {
      setLoading(false);
    }
  }, [refreshDetail, refreshList]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    Promise.all([
      refreshList(controller.signal),
      refreshDetail(controller.signal),
    ])
      .catch((reason) => {
        if (reason instanceof DOMException && reason.name === "AbortError") {
          return;
        }
        setError(
          reason instanceof Error ? reason.message : "Unable to load tasks",
        );
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [refreshDetail, refreshList]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    let refreshTimer: number | undefined;
    const refreshProjection = () => {
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        void Promise.all([refreshList(), refreshDetail()]);
      }, 100);
    };
    void tasksApi
      .stream(
        selectedId,
        (event) => {
          setEvents((current) => {
            if (current.some((item) => item.event_id === event.event_id)) {
              return current;
            }
            return [...current, event].sort(
              (left, right) => left.sequence - right.sequence,
            );
          });
          refreshProjection();
        },
        controller.signal,
      )
      .catch((reason) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          refreshProjection();
        }
      });
    return () => {
      controller.abort();
      window.clearTimeout(refreshTimer);
    };
  }, [refreshDetail, refreshList, selectedId]);

  return {
    tasks,
    runs,
    plan,
    events,
    approvals,
    conversation,
    artifacts,
    evidence,
    capabilities,
    loading,
    error,
    refresh,
  };
}
