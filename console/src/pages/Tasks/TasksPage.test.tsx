import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { tasksApi } from "../../api/modules/tasks";
import TasksPage from ".";

vi.mock("../../api/modules/tasks", async (loadOriginal) => {
  const original = await loadOriginal<
    typeof import("../../api/modules/tasks")
  >();
  return {
    ...original,
    tasksApi: {
      create: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      projection: vi.fn(),
      events: vi.fn(),
      stream: vi.fn(),
      approvals: vi.fn(),
      artifacts: vi.fn(),
      cancel: vi.fn(),
      start: vi.fn(),
      resume: vi.fn(),
      decide: vi.fn(),
      artifactContent: vi.fn(),
      artifactDownloadUrl: vi.fn(),
      capabilities: vi.fn(),
    },
  };
});

describe("TasksPage", () => {
  beforeEach(() => {
    vi.mocked(tasksApi.list).mockResolvedValue({
      items: [
        {
          task_id: "task-1",
          objective: "Prepare release evidence",
          status: "completed",
          source: "user",
          agent_id: "default",
          version: 4,
          active_run_id: "run-1",
          created_at: "2026-09-21T00:00:00Z",
          updated_at: "2026-09-21T00:01:00Z",
          metadata: { workspace_dir: "/workspace/QwenPawCodex" },
        },
      ],
      next_cursor: null,
    });
    vi.mocked(tasksApi.projection).mockResolvedValue({
      task: {} as never,
      active_run: null,
      runs: [
        {
          run_id: "run-1",
          attempt: 1,
          status: "succeeded",
          runner_id: "runner.local",
          registry_generation: 2,
          checkpoint_id: null,
          started_at: "2026-09-21T00:00:05Z",
          finished_at: "2026-09-21T00:01:00Z",
        },
      ],
      latest_plan: {
        plan_id: "plan-1",
        revision: 1,
        steps: [
          {
            step_id: "step-1",
            title: "执行任务",
            objective: "Prepare release evidence",
            depends_on: [],
          },
        ],
        acceptance_criteria: [],
      },
      conversation_messages: [
        {
          role: "user",
          text: "Prepare release evidence",
          run_id: "run-1",
          created_at: "2026-09-21T00:00:05Z",
          completed_at: "2026-09-21T00:00:05Z",
        },
        {
          role: "assistant",
          text: "Release is ready",
          run_id: "run-1",
          created_at: "2026-09-21T00:00:10Z",
          completed_at: "2026-09-21T00:00:10Z",
        },
      ],
      tool_activities: [],
      pending_approvals: [],
      recent_decisions: [],
      artifacts: [],
      evidence: [],
      checkpoint: null,
      capabilities: [
        {
          capability_id: "qwenpaw.system.tasks.default-strategy",
          slot: "strategy",
          registry_generation: 2,
        },
      ],
      last_sequence: 3,
    });
    vi.mocked(tasksApi.stream).mockResolvedValue();
    vi.mocked(tasksApi.events).mockResolvedValue({
      items: [
        {
          event_id: "event-1",
          task_id: "task-1",
          run_id: null,
          sequence: 1,
          event_type: "task.created",
          occurred_at: "2026-09-21T00:00:00Z",
          payload: {},
          artifact_refs: [],
          evidence_refs: [],
        },
        {
          event_id: "event-2",
          task_id: "task-1",
          run_id: "run-1",
          sequence: 2,
          event_type: "conversation.user",
          occurred_at: "2026-09-21T00:00:05Z",
          payload: { role: "user", text: "Prepare release evidence" },
          artifact_refs: [],
          evidence_refs: [],
        },
        {
          event_id: "event-3",
          task_id: "task-1",
          run_id: "run-1",
          sequence: 3,
          event_type: "conversation.assistant.delta",
          occurred_at: "2026-09-21T00:00:10Z",
          payload: { role: "assistant", text: "Release is ready" },
          artifact_refs: [],
          evidence_refs: [],
        },
      ],
    });
    vi.mocked(tasksApi.artifactContent).mockResolvedValue("# Task result");
    vi.mocked(tasksApi.artifactDownloadUrl).mockReturnValue(
      "/api/tasks/task-1/artifacts/artifact-1/content?disposition=attachment",
    );
    vi.mocked(tasksApi.capabilities).mockResolvedValue({
      registry_generation: 2,
      items: [
        {
          capability_id: "qwenpaw.system.tasks.default-strategy",
          slot: "strategy",
          provider_id: "qwenpaw.system",
          provider_kind: "system",
          version: "1.2.0",
          metadata: { label: "Default" },
        },
        {
          capability_id: "qwenpaw.system.tasks.goal-strategy",
          slot: "strategy",
          provider_id: "qwenpaw.system",
          provider_kind: "system",
          version: "1.2.0",
          metadata: { label: "Goal" },
        },
      ],
    });
  });

  it("shows task state and its durable timeline", async () => {
    render(<TasksPage />);

    expect(
      await screen.findByRole("heading", {
        name: "Prepare release evidence",
      }),
    ).toBeVisible();
    await waitFor(() => expect(screen.getByText("任务已创建")).toBeVisible());
    expect(screen.getByText("执行计划")).toBeVisible();
    expect(screen.getByText("Revision 1 · 1 个步骤")).toBeVisible();
    expect(screen.getByText("任务状态")).toBeVisible();
    expect(screen.getByText("运行策略")).toBeVisible();
    expect(
      screen.getByText("qwenpaw.system.tasks.default-strategy"),
    ).toBeVisible();
    expect(screen.getByText("证据覆盖")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "对话" }));
    expect(screen.getByText("Release is ready")).toBeVisible();
  });

  it("offers recovery instead of cancellation for failed tasks", async () => {
    const failedTask = {
      ...(await tasksApi.list()).items[0],
      status: "failed" as const,
    };
    vi.mocked(tasksApi.list).mockResolvedValue({
      items: [failedTask],
      next_cursor: null,
    });

    render(<TasksPage />);

    expect(
      await screen.findByRole("button", { name: "继续任务" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "取消" }),
    ).not.toBeInTheDocument();
  });

  it("renders and decides every task-scoped pending approval", async () => {
    vi.mocked(tasksApi.list).mockResolvedValue({
      items: [
        {
          ...(await tasksApi.list()).items[0],
          status: "waiting_approval",
        },
      ],
      next_cursor: null,
    });
    const projection = await tasksApi.projection("task-1");
    vi.mocked(tasksApi.projection).mockResolvedValue({
      ...projection,
      pending_approvals: [
        {
          approval_id: "approval-1",
          task_id: "task-1",
          run_id: "run-1",
          source: "tool",
          action: "tool.execute",
          risk: "high",
          policy: "tool_guard",
          continuation: "resume_on_decision",
          status: "pending",
          redacted_arguments: {
            tool_name: "execute_shell_command",
            input: { command: "python <redacted>" },
          },
          display: {
            title: "Approve execute_shell_command",
            summary: "The task is waiting to execute a protected tool.",
            target: "execute_shell_command",
            provider: "tool_guard",
          },
          expires_at: null,
          created_at: "2026-09-21T00:00:05Z",
          decision: null,
        },
        {
          approval_id: "approval-2",
          task_id: "task-1",
          run_id: "run-1",
          source: "driver",
          action: "driver.execute",
          risk: "medium",
          policy: "driver_policy",
          continuation: "resume_on_decision",
          status: "pending",
          redacted_arguments: {},
          display: null,
          expires_at: null,
          created_at: "2026-09-21T00:00:06Z",
          decision: null,
        },
      ],
    });
    vi.mocked(tasksApi.decide).mockResolvedValue({});

    render(<TasksPage />);

    expect(
      await screen.findByText("2 个受保护动作正在等待审批。"),
    ).toBeVisible();
    expect(screen.getByText("Approve execute_shell_command")).toBeVisible();
    expect(screen.getByText("driver.execute")).toBeVisible();
    const approveButtons = screen.getAllByRole("button", {
      name: "批准并继续",
    });
    fireEvent.click(approveButtons[0]);
    await waitFor(() =>
      expect(tasksApi.decide).toHaveBeenCalledWith(
        "task-1",
        "approval-1",
        "approved",
      ),
    );
  });

  it("creates and starts a task from the workbench", async () => {
    const createdTask = {
      ...(await tasksApi.list()).items[0],
      task_id: "task-2",
      objective: "Review the plugin boundary",
      status: "created" as const,
      active_run_id: null,
    };
    vi.mocked(tasksApi.create).mockResolvedValue(createdTask);
    vi.mocked(tasksApi.start).mockResolvedValue({
      task: { ...createdTask, status: "running", active_run_id: "run-2" },
      run: {
        run_id: "run-2",
        attempt: 1,
        status: "running",
        runner_id: "runner.local",
        registry_generation: 2,
        checkpoint_id: null,
        started_at: "2026-09-21T00:02:00Z",
        finished_at: null,
      },
    });

    render(<TasksPage />);

    fireEvent.click(await screen.findByRole("button", { name: "新建任务" }));
    fireEvent.change(screen.getByLabelText("任务目标"), {
      target: { value: "Review the plugin boundary" },
    });
    fireEvent.change(screen.getByLabelText(/验收标准/), {
      target: { value: "Produce a plan\nList the risks" },
    });
    fireEvent.change(screen.getByLabelText(/工作目录/), {
      target: { value: "/workspace/QwenPawCodex" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建并开始" }));

    await waitFor(() =>
      expect(tasksApi.create).toHaveBeenCalledWith({
        objective: "Review the plugin boundary",
        acceptance_criteria: ["Produce a plan", "List the risks"],
        project_dir: "/workspace/QwenPawCodex",
        strategy_id: "qwenpaw.system.tasks.default-strategy",
      }),
    );
    expect(tasksApi.start).toHaveBeenCalledWith("task-2");
  });

  it("previews and exposes downloads for task artifacts", async () => {
    const projection = await tasksApi.projection("task-1");
    vi.mocked(tasksApi.projection).mockResolvedValue({
      ...projection,
      artifacts: [
        {
          artifact_id: "artifact-1",
          kind: "document",
          uri: "qwenpaw-artifact://sha256/example",
          media_type: "text/markdown",
          content_hash:
            "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
          size_bytes: 13,
          metadata: { name: "task-result.md" },
          preview: {
            available: true,
            registry_generation: 3,
            renderer_id: "qwenpaw.system.tasks.safe-artifact-renderer",
            reason: "",
          },
        },
      ],
    });

    render(<TasksPage />);

    const previewButtons = await screen.findAllByRole("button", {
      name: "预览",
    });
    fireEvent.click(previewButtons[0]);

    expect(await screen.findByText("# Task result")).toBeInTheDocument();
    expect(tasksApi.artifactContent).toHaveBeenCalledWith(
      "task-1",
      "artifact-1",
    );
    const downloads = screen.getAllByRole("link", { name: "下载" });
    expect(downloads[0]).toHaveAttribute(
      "href",
      "/api/tasks/task-1/artifacts/artifact-1/content?disposition=attachment",
    );
    expect(downloads[0]).toHaveAttribute("download");
  });

  it("uses server renderer availability instead of guessing MIME support", async () => {
    const projection = await tasksApi.projection("task-1");
    vi.mocked(tasksApi.projection).mockResolvedValue({
      ...projection,
      artifacts: [
        {
          artifact_id: "artifact-1",
          kind: "document",
          uri: "qwenpaw-artifact://sha256/example",
          media_type: "text/plain",
          content_hash:
            "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
          size_bytes: 13,
          metadata: { name: "blocked.txt" },
          preview: {
            available: false,
            registry_generation: 3,
            renderer_id: null,
            reason: "unsupported",
          },
        },
      ],
    });

    render(<TasksPage />);

    const previewButtons = await screen.findAllByRole("button", {
      name: "预览",
    });
    expect(previewButtons[0]).toBeDisabled();
    expect(previewButtons[0]).toHaveAttribute(
      "title",
      "当前没有安全 Renderer 支持此类型",
    );
  });
});
