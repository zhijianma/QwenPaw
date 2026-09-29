import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../request", () => ({ request: vi.fn() }));
vi.mock("../config", () => ({
  getApiUrl: vi.fn((path: string) => `/api${path}`),
}));
vi.mock("../authHeaders", () => ({
  buildAuthHeaders: vi.fn(() => ({ Authorization: "Bearer test-token" })),
}));

import { request } from "../request";
import { tasksApi, type TaskEvent } from "./tasks";

describe("tasksApi", () => {
  beforeEach(() => {
    vi.mocked(request).mockReset();
    vi.restoreAllMocks();
  });

  it("encodes the cursor and uses stable task routes", async () => {
    vi.mocked(request).mockResolvedValue({ items: [], next_cursor: null });

    await tasksApi.list("task/next");
    await tasksApi.cancel("task-id");

    expect(request).toHaveBeenNthCalledWith(1, "/tasks?cursor=task%2Fnext", {
      signal: undefined,
    });
    expect(request).toHaveBeenNthCalledWith(2, "/tasks/task-id/cancel", {
      method: "POST",
    });
  });

  it("sends explicit approval decisions", async () => {
    vi.mocked(request).mockResolvedValue({});

    await tasksApi.decide("task-id", "approval-id", "approved");

    expect(request).toHaveBeenCalledWith(
      "/tasks/task-id/approvals/approval-id/decision",
      {
        method: "POST",
        body: JSON.stringify({
          decision: "approved",
          reason: "Decided in task workbench",
        }),
      },
    );
  });

  it("loads task-scoped pending approval projections", async () => {
    vi.mocked(request).mockResolvedValue({ items: [] });

    await tasksApi.approvals("task/id");

    expect(request).toHaveBeenCalledWith(
      "/tasks/task%2Fid/approvals?status=pending",
      { signal: undefined },
    );
  });

  it("loads the authoritative task workbench projection", async () => {
    vi.mocked(request).mockResolvedValue({});

    await tasksApi.projection("task/id");

    expect(request).toHaveBeenCalledWith("/tasks/task%2Fid/projection", {
      signal: undefined,
    });
  });

  it("discovers strategy contributions from the shared catalog", async () => {
    vi.mocked(request).mockResolvedValue({
      registry_generation: 3,
      items: [],
    });

    await tasksApi.capabilities("strategy");

    expect(request).toHaveBeenCalledWith("/tasks/capabilities?slot=strategy", {
      signal: undefined,
    });
  });

  it("creates and starts a task through stable public routes", async () => {
    vi.mocked(request).mockResolvedValue({});

    await tasksApi.create({
      objective: "Review the plugin boundary",
      acceptance_criteria: ["Produce a plan"],
      project_dir: "/workspace/QwenPawCodex",
      strategy_id: "qwenpaw.system.tasks.goal-strategy",
      approval_level: "strict",
    });
    await tasksApi.start("task-id");

    expect(request).toHaveBeenNthCalledWith(1, "/tasks", {
      method: "POST",
      body: JSON.stringify({
        objective: "Review the plugin boundary",
        acceptance_criteria: ["Produce a plan"],
        project_dir: "/workspace/QwenPawCodex",
        strategy_id: "qwenpaw.system.tasks.goal-strategy",
        approval_level: "strict",
      }),
    });
    expect(request).toHaveBeenNthCalledWith(2, "/tasks/task-id/start", {
      method: "POST",
    });
  });

  it("builds task-scoped artifact preview and download routes", async () => {
    vi.mocked(request).mockResolvedValue("artifact body");

    await tasksApi.artifactContent("task/id", "artifact/id");

    expect(request).toHaveBeenCalledWith(
      "/tasks/task%2Fid/artifacts/artifact%2Fid/content",
      { signal: undefined },
    );
    expect(tasksApi.artifactDownloadUrl("task/id", "artifact/id")).toBe(
      "/api/tasks/task%2Fid/artifacts/artifact%2Fid/content?disposition=attachment",
    );
  });

  it("polls a sensor through the approval-gated task route", async () => {
    vi.mocked(request).mockResolvedValue({ items: [] });

    await tasksApi.pollSensor("task-insights.review");

    expect(request).toHaveBeenCalledWith(
      "/tasks/sensors/task-insights.review/poll",
      { method: "POST" },
    );
  });

  it("streams replayable task events with auth and chunk buffering", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode('id: 4\ndata: {"event_id":"event-4","sequence":'),
        );
        controller.enqueue(
          encoder.encode(
            '4}\n\nid: 5\ndata: {"event_id":"event-5","sequence":5}\n\n',
          ),
        );
        controller.close();
      },
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const received: TaskEvent[] = [];

    await tasksApi.stream(
      "task/id",
      (event) => received.push(event),
      undefined,
      3,
    );

    expect(fetchMock).toHaveBeenCalledWith("/api/tasks/task%2Fid/stream", {
      headers: {
        Accept: "text/event-stream",
        Authorization: "Bearer test-token",
        "Last-Event-ID": "3",
      },
      signal: undefined,
    });
    expect(received.map((event) => event.sequence)).toEqual([4, 5]);
  });
});
