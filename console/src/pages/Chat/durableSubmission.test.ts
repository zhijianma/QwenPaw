import { beforeEach, describe, expect, it, vi } from "vitest";

import { chatApi } from "../../api/modules/chat";
import {
  allocateDurableChat,
  buildDurableComposerRequest,
  buildDurableSubmission,
  canDrainLegacyQueueItems,
  resolveComposerAdmissionOwner,
  selectLegacyQueueItems,
  submitDurableChatRequest,
  waitForDurableAdmission,
} from "./durableSubmission";
import type {
  ConversationRuntimeProjection,
  SubmissionStatus,
  TurnSubmission,
} from "../../api/types";

vi.mock("../../api/modules/chat", () => ({
  chatApi: {
    getQueue: vi.fn(),
    getRuntime: vi.fn(),
    submitTurn: vi.fn(),
  },
}));

function submission(
  submissionId: string,
  status: SubmissionStatus,
  sequence: number,
): TurnSubmission {
  return {
    agent_id: "agent-1",
    chat_id: "chat-1",
    priority: 20,
    content: submissionId,
    artifact_refs: [],
    request_context: {},
    input_envelope: null,
    idempotency_key: submissionId,
    correlation_id: `correlation-${sequence}`,
    submission_id: submissionId,
    sequence,
    queue_position: sequence,
    invocation_id: status === "queued" ? null : `invocation-${sequence}`,
    status,
    revision: status === "queued" ? 1 : 3,
    created_at: "2026-09-28T00:00:00Z",
    updated_at: "2026-09-28T00:00:00Z",
  };
}

function projection(
  submissions: TurnSubmission[],
  activeSubmissionId: string | null,
  revision: number,
): ConversationRuntimeProjection {
  return {
    agent_id: "agent-1",
    chat_id: "chat-1",
    queue: {
      agent_id: "agent-1",
      chat_id: "chat-1",
      revision,
      active_submission_id: activeSubmissionId,
      submissions,
      updated_at: "2026-09-28T00:00:00Z",
    },
    interactions: [],
    cursor: `v1-${revision}-test`,
    observed_at: "2026-09-28T00:00:00Z",
  };
}

describe("durable Chat submission adapter", () => {
  it("keeps browser queue state outside QwenPaw projections", () => {
    const stored = [{ id: "stale-browser-item" }];

    expect(selectLegacyQueueItems(true, stored)).toEqual([]);
    expect(selectLegacyQueueItems(false, stored)).toBe(stored);
  });

  it("never drains a QwenPaw or unknown legacy queue owner", () => {
    const agents = [
      { id: "default", backend: "qwenpaw" as const },
      { id: "codex", backend: "codex" as const },
    ];

    expect(canDrainLegacyQueueItems([], agents)).toBe(false);
    expect(canDrainLegacyQueueItems([{}], agents)).toBe(false);
    expect(canDrainLegacyQueueItems([{ agentId: "missing" }], agents)).toBe(
      false,
    );
    expect(canDrainLegacyQueueItems([{ agentId: "codex" }], agents)).toBe(true);
    expect(
      canDrainLegacyQueueItems(
        [{ agentId: "codex" }, { agentId: "default" }],
        agents,
      ),
    ).toBe(false);
  });

  beforeEach(() => vi.clearAllMocks());

  it("allocates and activates a stable Chat before first submission", async () => {
    const trace: string[] = [];

    await expect(
      allocateDurableChat({
        name: "First message",
        createSession: async (name) => {
          trace.push(`create:${name}`);
          return {
            session: {
              id: "chat-1",
              sessionId: "console:runtime-1",
            },
          };
        },
        activateSession: (chatId) => trace.push(`activate:${chatId}`),
      }),
    ).resolves.toEqual({
      chatId: "chat-1",
      sessionId: "console:runtime-1",
    });
    expect(trace).toEqual(["create:First message", "activate:chat-1"]);
  });

  it("fails closed when allocation has no stable Chat identity", async () => {
    const activateSession = vi.fn();

    await expect(
      allocateDurableChat({
        name: "First message",
        createSession: async () => ({ session: { id: "" } }),
        activateSession,
      }),
    ).rejects.toThrow("no ChatSpec identity");
    expect(activateSession).not.toHaveBeenCalled();
  });

  it("never assigns a QwenPaw first turn to the legacy queue", () => {
    expect(
      resolveComposerAdmissionOwner({
        usesQwenPawBackend: true,
        hasStableChat: false,
        sameVisit: true,
        requiresQueue: true,
      }),
    ).toBe("allocate");
    expect(
      resolveComposerAdmissionOwner({
        usesQwenPawBackend: true,
        hasStableChat: false,
        sameVisit: false,
        requiresQueue: true,
      }),
    ).toBe("reject");
  });

  it("keeps external backends on the compatibility queue", () => {
    expect(
      resolveComposerAdmissionOwner({
        usesQwenPawBackend: false,
        hasStableChat: false,
        sameVisit: true,
        requiresQueue: true,
      }),
    ).toBe("legacy-queue");
  });

  it("keeps a navigated QwenPaw submission on the server queue", () => {
    expect(
      resolveComposerAdmissionOwner({
        usesQwenPawBackend: true,
        hasStableChat: true,
        sameVisit: false,
        requiresQueue: true,
      }),
    ).toBe("server-queue");
  });

  it("builds a transport-neutral composer snapshot", () => {
    expect(
      buildDurableComposerRequest({
        contentParts: [
          { type: "text", text: "Continue" },
          { type: "file", file_url: "report.md" },
        ],
        requestContext: { approval_level: "strict" },
        messageMetadata: { qwenpaw_client_message_id: "message-1" },
        sessionId: "console:chat-1",
        userId: "local-user",
        channel: "console",
      }),
    ).toEqual({
      input: [
        {
          role: "user",
          metadata: { qwenpaw_client_message_id: "message-1" },
          content: [
            { type: "text", text: "Continue" },
            { type: "file", file_url: "report.md" },
          ],
        },
      ],
      session_id: "console:chat-1",
      user_id: "local-user",
      channel: "console",
      stream: true,
      request_context: { approval_level: "strict" },
    });
  });

  it("builds a composer snapshot before compatibility identity is hydrated", () => {
    expect(
      buildDurableComposerRequest({
        contentParts: [{ type: "text", text: "Continue" }],
        requestContext: {},
        messageMetadata: { qwenpaw_client_message_id: "message-2" },
      }),
    ).toEqual({
      input: [
        {
          role: "user",
          metadata: { qwenpaw_client_message_id: "message-2" },
          content: [{ type: "text", text: "Continue" }],
        },
      ],
      stream: true,
      request_context: {},
    });
  });

  it("drops transport identity while preserving execution context", () => {
    expect(
      buildDurableSubmission(
        {
          session_id: "runtime-session",
          user_id: "local-user",
          channel: "console",
          stream: true,
          request_context: {
            approval_level: "strict",
            session_project_dirs: [{ path: "/project", label: null }],
          },
          plugin_option: { format: "brief" },
          input: [
            {
              role: "user",
              metadata: { qwenpaw_client_message_id: "message-1" },
              content: [
                { type: "text", text: "Continue" },
                { type: "file", url: "artifact.txt" },
              ],
            },
          ],
        },
        "message-1",
        7,
      ),
    ).toEqual({
      idempotency_key: "message-1",
      expected_revision: 7,
      content_parts: [
        { type: "text", text: "Continue" },
        { type: "file", url: "artifact.txt" },
      ],
      request_context: {
        approval_level: "strict",
        session_project_dirs: [{ path: "/project", label: null }],
      },
      message_metadata: { qwenpaw_client_message_id: "message-1" },
      request_extensions: {
        plugin_option: { format: "brief" },
      },
    });
  });

  it("lets the server sequence a concurrent enqueue atomically", async () => {
    vi.mocked(chatApi.submitTurn).mockResolvedValue({
      receipt_id: "receipt-1",
      submission_id: "submission-1",
      kind: "enqueue",
      status: "accepted",
      agent_id: "agent-1",
      chat_id: "chat-1",
      revision: 12,
      detail: "",
      recorded_at: "2026-09-28T00:00:01Z",
    });

    const accepted = await submitDurableChatRequest({
      chatId: "chat-1",
      agentId: "agent-1",
      idempotencyKey: "message-1",
      requestBody: {
        input: [{ role: "user", content: "Continue" }],
      },
    });

    expect(chatApi.submitTurn).toHaveBeenCalledWith(
      "chat-1",
      {
        idempotency_key: "message-1",
        content_parts: [{ type: "text", text: "Continue" }],
        message_metadata: {},
        request_context: {},
      },
      "agent-1",
    );
    expect(accepted.receipt.submission_id).toBe("submission-1");
    expect(chatApi.getQueue).not.toHaveBeenCalled();
  });

  it("waits for the exact submission rather than another active turn", async () => {
    vi.mocked(chatApi.getRuntime)
      .mockResolvedValueOnce(
        projection(
          [
            submission("other-submission", "running", 1),
            submission("target-submission", "queued", 2),
          ],
          "other-submission",
          2,
        ),
      )
      .mockResolvedValueOnce(
        projection(
          [submission("target-submission", "running", 2)],
          "target-submission",
          3,
        ),
      );

    await expect(
      waitForDurableAdmission({
        chatId: "chat-1",
        agentId: "agent-1",
        submissionId: "target-submission",
        pollIntervalMs: 0,
      }),
    ).resolves.toBe("active");
    expect(chatApi.getRuntime).toHaveBeenCalledTimes(2);
  });

  it("treats a submission absent after its receipt as terminal", async () => {
    vi.mocked(chatApi.getRuntime).mockResolvedValue(projection([], null, 4));

    await expect(
      waitForDurableAdmission({
        chatId: "chat-1",
        agentId: "agent-1",
        submissionId: "completed-submission",
      }),
    ).resolves.toBe("terminal");
  });
});
