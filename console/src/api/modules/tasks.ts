import { createTaskClient } from "../../clients/taskClient";
import type { TaskEvent } from "../../contracts/tasks";
import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import { request } from "../request";

export type LiteTask = import("../../contracts/tasks").KernelTask;
export type {
  TaskApproval,
  TaskArtifact,
  TaskArtifactRef,
  TaskCapability,
  TaskCapabilityDescriptor,
  TaskConversationMessage,
  TaskEvent,
  TaskEvidence,
  TaskPlan,
  TaskPlanStep,
  TaskProjection,
  TaskRun,
  TaskStatus,
} from "../../contracts/tasks";

const client = createTaskClient({
  request,
  openStream: (path, init) => {
    const headers = {
      ...buildAuthHeaders(),
      ...((init?.headers as Record<string, string>) ?? {}),
    };
    return fetch(getApiUrl(path), { ...init, headers });
  },
  toUrl: getApiUrl,
});

export const tasksApi = {
  ...client,
  async stream(
    taskId: string,
    onEvent: (event: TaskEvent) => void,
    signal?: AbortSignal,
    afterSequence = 0,
  ) {
    for await (const event of client.follow(taskId, afterSequence, signal)) {
      onEvent(event);
    }
  },
  decide: (
    taskId: string,
    approvalId: string,
    decision: "approved" | "denied",
  ) =>
    client.decideApproval(taskId, approvalId, {
      decision,
      reason: "Decided in task workbench",
    }),
};
