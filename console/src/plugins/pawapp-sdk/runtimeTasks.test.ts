import { beforeEach, describe, expect, it, vi } from "vitest";

import { hostFetch } from "../hostSdk/fetch";
import {
  createRuntimeTasksNamespace,
  PawRuntimeTaskError,
} from "./runtimeTasks";
import type {
  PawRuntimeTask,
  PawRuntimeTaskEvent,
  PawRuntimeTaskProjection,
  PawRuntimeTaskStatus,
} from "./types";

vi.mock("../hostSdk/fetch", () => ({ hostFetch: vi.fn() }));

const mockedHostFetch = vi.mocked(hostFetch);

function task(status: PawRuntimeTaskStatus = "running"): PawRuntimeTask {
  return {
    task_id: "task-1",
    objective: "Review the project",
    status,
    source: "user",
    agent_id: "default",
    constraints: [],
    acceptance_criteria: [],
    execution_contract: null,
    version: 2,
    active_run_id: "run-1",
    created_at: "2026-10-10T00:00:00Z",
    updated_at: "2026-10-10T00:00:01Z",
    metadata: {},
  };
}

function projection(
  status: PawRuntimeTaskStatus,
  lastSequence: number,
): PawRuntimeTaskProjection {
  return {
    task: task(status),
    active_run: {},
    runs: [],
    latest_plan: null,
    conversation_messages: [],
    pending_approvals: [],
    artifacts: [],
    evidence: [],
    capabilities: [],
    last_sequence: lastSequence,
  };
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function sseResponse(events: PawRuntimeTaskEvent[]): Response {
  const body = events
    .map((event) => `id: ${event.sequence}\ndata: ${JSON.stringify(event)}\n\n`)
    .join("");
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

function event(sequence: number): PawRuntimeTaskEvent {
  return {
    event_id: `event-${sequence}`,
    task_id: "task-1",
    run_id: "run-1",
    sequence,
    event_type: "task.progressed",
    occurred_at: "2026-10-10T00:00:01Z",
    payload: { sequence },
    artifact_refs: [],
    evidence_refs: [],
  };
}

describe("PawApp runtime tasks", () => {
  beforeEach(() => {
    mockedHostFetch.mockReset();
  });

  it("negotiates the runner and resolves from a terminal projection", async () => {
    mockedHostFetch
      .mockResolvedValueOnce(
        jsonResponse({
          registry_generation: 4,
          items: [{ capability_id: "review.runner" }],
        }),
      )
      .mockResolvedValueOnce(jsonResponse(task("created")))
      .mockResolvedValueOnce(jsonResponse({ task: task(), run: {} }))
      .mockResolvedValueOnce(sseResponse([event(1), event(2)]))
      .mockResolvedValueOnce(jsonResponse(projection("completed", 2)));

    const handle = await createRuntimeTasksNamespace().run(
      {
        objective: "Review the project",
        acceptanceCriteria: ["Report is complete"],
        runnerId: "review.runner",
      },
      { idempotencyKey: "create-once" },
    );
    const received = vi.fn();
    handle.on("task.progressed", received);

    await expect(handle.result).resolves.toMatchObject({
      task: { status: "completed" },
      last_sequence: 2,
    });
    expect(handle.lastSequence).toBe(2);
    expect(received).toHaveBeenCalledTimes(2);
    expect(mockedHostFetch).toHaveBeenNthCalledWith(
      1,
      "/tasks/capabilities?slot=runner",
    );
    expect(mockedHostFetch).toHaveBeenNthCalledWith(
      2,
      "/tasks",
      expect.objectContaining({
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": "create-once",
        },
        body: expect.stringContaining('"runner_id":"review.runner"'),
      }),
    );
  });

  it("reconnects from the committed sequence after a non-terminal EOF", async () => {
    mockedHostFetch
      .mockResolvedValueOnce(jsonResponse(task("created")))
      .mockResolvedValueOnce(jsonResponse({ task: task(), run: {} }))
      .mockResolvedValueOnce(sseResponse([event(3)]))
      .mockResolvedValueOnce(jsonResponse(projection("running", 3)))
      .mockResolvedValueOnce(sseResponse([event(4)]))
      .mockResolvedValueOnce(jsonResponse(projection("completed", 4)));

    const handle = await createRuntimeTasksNamespace().run(
      { objective: "Review the project" },
      { reconnectDelayMs: 0 },
    );
    await expect(handle.result).resolves.toMatchObject({ last_sequence: 4 });
    expect(mockedHostFetch).toHaveBeenNthCalledWith(
      5,
      "/tasks/task-1/stream",
      expect.objectContaining({
        headers: {
          Accept: "text/event-stream",
          "Last-Event-ID": "3",
        },
      }),
    );
  });

  it("rejects before creation when a requested runner is not active", async () => {
    mockedHostFetch.mockResolvedValueOnce(
      jsonResponse({ registry_generation: 4, items: [] }),
    );

    await expect(
      createRuntimeTasksNamespace().run({
        objective: "Review the project",
        runnerId: "missing.runner",
      }),
    ).rejects.toMatchObject({
      code: "RUNNER_CAPABILITY_UNAVAILABLE",
    });
    expect(mockedHostFetch).toHaveBeenCalledTimes(1);
  });

  it("returns the durable cancellation command receipt", async () => {
    mockedHostFetch.mockImplementation(async (path, init) => {
      if (path === "/tasks" && init?.method === "POST") {
        return jsonResponse(task("created"));
      }
      if (path === "/tasks/task-1/start") {
        return jsonResponse({ task: task(), run: {} });
      }
      if (path === "/tasks/task-1/stream") return sseResponse([]);
      if (path === "/tasks/task-1/projection") {
        return jsonResponse(projection("cancelled", 5));
      }
      if (path === "/tasks/task-1/cancel") {
        return jsonResponse(task("cancelled"));
      }
      throw new Error(`Unexpected request: ${path}`);
    });

    const handle = await createRuntimeTasksNamespace().run({
      objective: "Review the project",
    });
    const cancelCommand = handle.cancel();
    expect(handle.cancel()).toBe(cancelCommand);
    const receipt = await cancelCommand;

    expect(receipt).toMatchObject({
      taskId: "task-1",
      task: { status: "cancelled" },
    });
    expect(receipt.idempotencyKey).toBeTruthy();
    await expect(handle.result).rejects.toBeInstanceOf(PawRuntimeTaskError);
    expect(mockedHostFetch).toHaveBeenCalledWith(
      "/tasks/task-1/cancel",
      expect.objectContaining({
        method: "POST",
        headers: { "Idempotency-Key": receipt.idempotencyKey },
      }),
    );
  });

  it("submits one idempotent direct approval decision", async () => {
    mockedHostFetch.mockImplementation(async (path, init) => {
      if (path === "/tasks" && init?.method === "POST") {
        return jsonResponse(task("created"));
      }
      if (path === "/tasks/task-1/start") {
        return jsonResponse({ task: task(), run: {} });
      }
      if (path === "/tasks/task-1/approvals/approval%2F1/decision") {
        return jsonResponse({
          approval_id: "approval/1",
          decision: "approved",
          actor: { type: "user", id: "local-user" },
          scope: "exact",
          reason: "Reviewed",
          decided_at: "2026-10-10T00:00:02Z",
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    const handle = await createRuntimeTasksNamespace().run({
      objective: "Review the project",
    });
    const command = {
      decision: "approved" as const,
      reason: "Reviewed",
      idempotencyKey: "approve-once",
    };

    const first = handle.decideApproval("approval/1", command);
    expect(handle.decideApproval("approval/1", command)).toBe(first);
    await expect(first).resolves.toMatchObject({ decision: "approved" });
    expect(mockedHostFetch).toHaveBeenCalledWith(
      "/tasks/task-1/approvals/approval%2F1/decision",
      expect.objectContaining({
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": "approve-once",
        },
        body: JSON.stringify({
          decision: "approved",
          reason: "Reviewed",
          scope: "exact",
        }),
      }),
    );
    await expect(
      handle.decideApproval("approval/1", {
        decision: "denied",
        reason: "Changed my mind",
      }),
    ).rejects.toMatchObject({ code: "APPROVAL_COMMAND_CONFLICT" });
  });
});
