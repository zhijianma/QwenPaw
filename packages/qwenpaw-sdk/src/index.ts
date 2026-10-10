import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";

import {
  createQwenPawClient,
  buildChatSubmission,
  missingRuntimeFeatures,
  QwenPawHttpError,
  RuntimeClientError,
  type ChatControlRequest,
  type ChatForkRequest,
  type ChatHistory,
  type ChatArtifactContentOptions,
  type ChatInput,
  type ChatInteractionDecisionRequest,
  type ChatInteractionResolution,
  type ChatQueueReorderRequest,
  type ChatRequestOptions,
  type ChatListEvidenceOptions,
  type ChatPageEvidenceOptions,
  type ChatSpec,
  type ChatSteerRequest,
  type ChatSubmissionRequest,
  type ChatSubmissionOptions,
  type ControlReceipt,
  type ConversationExecutionChain,
  type ConversationRuntimeProjection,
  type FetchTransportOptions,
  type FollowRuntimeOptions,
  type FollowSubmissionOptions,
  type QwenPawClient,
  type QueueProjection,
  type RuntimeFeature,
  type RuntimeHandshake,
} from "@qwenpaw/client";

const LAUNCH_PREFIX = "QWENPAW_MANAGED_HOST ";
const LAUNCH_SCHEMA = "qwenpaw.managed-host-launch.v1";
const DEFAULT_STARTUP_TIMEOUT_MS = 15_000;
const DEFAULT_SHUTDOWN_TIMEOUT_MS = 10_000;
const MAX_DIAGNOSTIC_CHARS = 8_192;

export type ManagedHostErrorCode =
  | "HOST_PROCESS_ERROR"
  | "HOST_PROCESS_EXITED"
  | "HOST_LAUNCH_INVALID"
  | "HOST_STARTUP_TIMEOUT"
  | "HOST_HANDSHAKE_FAILED"
  | "HOST_FEATURE_MISSING";

export class ManagedHostError extends Error {
  readonly code: ManagedHostErrorCode;
  readonly diagnostics?: string;

  constructor(
    code: ManagedHostErrorCode,
    message: string,
    options: { cause?: unknown; diagnostics?: string } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "ManagedHostError";
    this.code = code;
    this.diagnostics = options.diagnostics;
  }
}

export interface QwenPawRuntimeCommand {
  /** Explicit QwenPaw executable. No PATH discovery is performed. */
  executable: string;
  /** Prefix arguments, for example ["-m", "qwenpaw"] for Python. */
  args?: readonly string[];
}

export interface ManagedHostOutput {
  stream: "stdout" | "stderr";
  line: string;
}

export interface QwenPawHostOptions {
  runtime: QwenPawRuntimeCommand;
  stateDir?: string;
  cwd?: string;
  env?: Readonly<Record<string, string>>;
  token?: string;
  agentId?: string;
  headers?: HeadersInit;
  requiredFeatures?: readonly RuntimeFeature[];
  startupTimeoutMs?: number;
  shutdownTimeoutMs?: number;
  onOutput?: (output: ManagedHostOutput) => void;
}

export interface ManagedHostExit {
  pid: number;
  expected: boolean;
  forced: boolean;
  code: number | null;
  signal: string | null;
}

interface ManagedHostLifecycle {
  closing: boolean;
  forced: boolean;
}

export class QwenPawTurnError extends Error {
  readonly code: "TURN_RECEIPT_INVALID";

  constructor(message: string) {
    super(message);
    this.name = "QwenPawTurnError";
    this.code = "TURN_RECEIPT_INVALID";
  }
}

export class QwenPawTurn {
  readonly chatId: string;
  readonly submissionId: string;
  readonly receipt: ControlReceipt;

  readonly #client: QwenPawClient;

  constructor(client: QwenPawClient, chatId: string, receipt: ControlReceipt) {
    const submissionId = receipt.submission_id?.trim();
    if (!submissionId) {
      throw new QwenPawTurnError(
        "QwenPaw Host accepted a Chat turn without a submission identity",
      );
    }
    this.#client = client;
    this.chatId = chatId;
    this.submissionId = submissionId;
    this.receipt = receipt;
  }

  follow(
    options: FollowSubmissionOptions = {},
  ): AsyncGenerator<ConversationExecutionChain, ConversationExecutionChain> {
    return this.#client.chats.followSubmission(
      this.chatId,
      this.submissionId,
      options,
    );
  }

  async wait(
    options: FollowSubmissionOptions = {},
  ): Promise<ConversationExecutionChain> {
    const stream = this.follow(options);
    while (true) {
      const result = await stream.next();
      if (result.done) return result.value;
    }
  }
}

export class QwenPawChat {
  readonly id: string;
  readonly spec?: ChatSpec;

  readonly #client: QwenPawClient;

  constructor(client: QwenPawClient, chatId: string, spec?: ChatSpec) {
    if (!chatId.trim()) throw new TypeError("chatId must not be empty");
    this.#client = client;
    this.id = chatId;
    this.spec = spec;
  }

  history(
    options: ChatRequestOptions & { includeAppOwned?: boolean } = {},
  ): Promise<ChatHistory> {
    return this.#client.chats.history(this.id, options);
  }

  actions(options: ChatListEvidenceOptions = {}) {
    return this.#client.chats.actions(this.id, options);
  }

  artifacts(options: ChatListEvidenceOptions = {}) {
    return this.#client.chats.artifacts(this.id, options);
  }

  artifactContent(
    artifactId: string,
    options: ChatArtifactContentOptions = {},
  ): Promise<Response> {
    return this.#client.chats.artifactContent(this.id, artifactId, options);
  }

  observations(options: ChatPageEvidenceOptions = {}) {
    return this.#client.chats.observations(this.id, options);
  }

  trajectory(correlationId: string, options: ChatPageEvidenceOptions = {}) {
    return this.#client.chats.trajectory(this.id, correlationId, options);
  }

  runtime(
    options: ChatRequestOptions = {},
  ): Promise<ConversationRuntimeProjection> {
    return this.#client.chats.runtime(this.id, options);
  }

  followRuntime(
    options: FollowRuntimeOptions = {},
  ): AsyncGenerator<ConversationRuntimeProjection, string | undefined> {
    return this.#client.chats.followRuntime(this.id, options);
  }

  submit(
    body: ChatSubmissionRequest,
    options: ChatRequestOptions = {},
  ): Promise<ControlReceipt> {
    return this.#client.chatControls.submit(this.id, body, options);
  }

  send(
    body: ChatSubmissionRequest,
    options?: ChatRequestOptions,
  ): Promise<QwenPawTurn>;
  send(
    input: ChatInput,
    options: ChatRequestOptions & ChatSubmissionOptions,
  ): Promise<QwenPawTurn>;
  async send(
    input: ChatSubmissionRequest | ChatInput,
    options:
      | ChatRequestOptions
      | (ChatRequestOptions & ChatSubmissionOptions) = {},
  ): Promise<QwenPawTurn> {
    const body =
      typeof input === "string" || "kind" in input
        ? buildChatSubmission(input, options as ChatSubmissionOptions)
        : input;
    const receipt = await this.submit(body, options);
    return new QwenPawTurn(this.#client, this.id, receipt);
  }

  queue(options: ChatRequestOptions = {}): Promise<QueueProjection> {
    return this.#client.chatControls.queue(this.id, options);
  }

  steer(
    body: ChatSteerRequest,
    options: ChatRequestOptions = {},
  ): Promise<ControlReceipt> {
    return this.#client.chatControls.steer(this.id, body, options);
  }

  interrupt(
    body: ChatControlRequest,
    options: ChatRequestOptions = {},
  ): Promise<ControlReceipt> {
    return this.#client.chatControls.interrupt(this.id, body, options);
  }

  stopAndClear(
    body: ChatControlRequest,
    options: ChatRequestOptions = {},
  ): Promise<ControlReceipt> {
    return this.#client.chatControls.stopAndClear(this.id, body, options);
  }

  cancelQueued(
    submissionId: string,
    body: ChatControlRequest,
    options: ChatRequestOptions = {},
  ): Promise<ControlReceipt> {
    return this.#client.chatControls.cancelQueued(
      this.id,
      submissionId,
      body,
      options,
    );
  }

  reorderQueue(
    body: ChatQueueReorderRequest,
    options: ChatRequestOptions = {},
  ): Promise<ControlReceipt> {
    return this.#client.chatControls.reorder(this.id, body, options);
  }

  listInteractions(signal?: AbortSignal) {
    return this.#client.interactions.list(this.id, signal);
  }

  respondToInteraction(
    interactionId: string,
    body: ChatInteractionDecisionRequest,
  ): Promise<ChatInteractionResolution> {
    return this.#client.interactions.respond(this.id, interactionId, body);
  }

  async fork(
    body: ChatForkRequest,
    options: ChatRequestOptions = {},
  ): Promise<QwenPawChat> {
    const spec = await this.#client.chats.fork(this.id, body, options);
    return new QwenPawChat(this.#client, spec.id, spec);
  }
}

export class QwenPawChats {
  readonly #client: QwenPawClient;

  constructor(client: QwenPawClient) {
    this.#client = client;
  }

  open(chatId: string): QwenPawChat {
    return new QwenPawChat(this.#client, chatId);
  }

  async create(
    body: Partial<ChatSpec>,
    options: ChatRequestOptions = {},
  ): Promise<QwenPawChat> {
    const spec = await this.#client.chats.create(body, options);
    return new QwenPawChat(this.#client, spec.id, spec);
  }
}

interface ManagedHostLaunch {
  schema: typeof LAUNCH_SCHEMA;
  api_url: string;
  pid: number;
}

function appendDiagnostic(current: string, chunk: string): string {
  const next = `${current}${chunk}`;
  return next.slice(-MAX_DIAGNOSTIC_CHARS);
}

function validateTimeout(name: string, value: number): number {
  if (!Number.isFinite(value) || value <= 0) {
    throw new TypeError(`${name} must be a positive finite number`);
  }
  return value;
}

function parseLaunch(value: string, processId: number | undefined) {
  let candidate: unknown;
  try {
    candidate = JSON.parse(value);
  } catch (error) {
    throw new ManagedHostError(
      "HOST_LAUNCH_INVALID",
      "QwenPaw Host emitted invalid launch JSON",
      { cause: error },
    );
  }
  if (typeof candidate !== "object" || candidate === null) {
    throw new ManagedHostError(
      "HOST_LAUNCH_INVALID",
      "QwenPaw Host emitted an invalid launch record",
    );
  }
  const launch = candidate as Partial<ManagedHostLaunch>;
  if (
    launch.schema !== LAUNCH_SCHEMA ||
    typeof launch.api_url !== "string" ||
    !Number.isSafeInteger(launch.pid) ||
    launch.pid !== processId
  ) {
    throw new ManagedHostError(
      "HOST_LAUNCH_INVALID",
      "QwenPaw Host launch record failed identity validation",
    );
  }
  let url: URL;
  try {
    url = new URL(launch.api_url);
  } catch (error) {
    throw new ManagedHostError(
      "HOST_LAUNCH_INVALID",
      "QwenPaw Host launch record contains an invalid API URL",
      { cause: error },
    );
  }
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    !url.port ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname.replace(/\/+$/, "") !== "/api"
  ) {
    throw new ManagedHostError(
      "HOST_LAUNCH_INVALID",
      "QwenPaw managed Host must advertise a loopback HTTP API root",
    );
  }
  return launch as ManagedHostLaunch;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForExit(
  child: ChildProcessWithoutNullStreams,
  timeoutMs: number,
): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.off("exit", onExit);
      resolve(false);
    }, timeoutMs);
    const onExit = () => {
      clearTimeout(timer);
      resolve(true);
    };
    child.once("exit", onExit);
  });
}

async function terminate(
  child: ChildProcessWithoutNullStreams,
  timeoutMs: number,
  lifecycle?: ManagedHostLifecycle,
): Promise<void> {
  if (child.pid === undefined) return;
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  if (await waitForExit(child, timeoutMs)) return;
  if (lifecycle) lifecycle.forced = true;
  child.kill("SIGKILL");
  await waitForExit(child, Math.min(timeoutMs, 1_000));
}

function observeManagedHostExit(
  child: ChildProcessWithoutNullStreams,
  lifecycle: ManagedHostLifecycle,
): Promise<ManagedHostExit> {
  const exit = (): ManagedHostExit => ({
    pid: child.pid as number,
    expected: lifecycle.closing,
    forced: lifecycle.forced,
    code: child.exitCode,
    signal: child.signalCode,
  });
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve(exit());
  }
  return new Promise((resolve) => {
    child.once("exit", () => resolve(exit()));
  });
}

async function waitForLaunch(
  child: ChildProcessWithoutNullStreams,
  timeoutMs: number,
  onOutput?: (output: ManagedHostOutput) => void,
): Promise<ManagedHostLaunch> {
  return new Promise((resolve, reject) => {
    let stdoutBuffer = "";
    let diagnostics = "";
    let settled = false;

    const finish = (error?: unknown, launch?: ManagedHostLaunch) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.off("error", onError);
      child.off("exit", onExit);
      if (error) reject(error);
      else resolve(launch as ManagedHostLaunch);
    };
    const observeLine = (stream: "stdout" | "stderr", line: string) => {
      onOutput?.({ stream, line });
      diagnostics = appendDiagnostic(diagnostics, `${stream}: ${line}\n`);
      if (stream !== "stdout" || !line.startsWith(LAUNCH_PREFIX)) return;
      try {
        finish(
          undefined,
          parseLaunch(line.slice(LAUNCH_PREFIX.length), child.pid),
        );
      } catch (error) {
        finish(error);
      }
    };
    const onStdout = (chunk: Buffer) => {
      stdoutBuffer += chunk.toString("utf8");
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() ?? "";
      for (const line of lines) observeLine("stdout", line);
      if (stdoutBuffer.length > MAX_DIAGNOSTIC_CHARS) {
        finish(
          new ManagedHostError(
            "HOST_LAUNCH_INVALID",
            "QwenPaw Host launch line exceeded the size limit",
          ),
        );
      }
    };
    const onStderr = (chunk: Buffer) => {
      for (const line of chunk.toString("utf8").split(/\r?\n/)) {
        if (line) observeLine("stderr", line);
      }
    };
    const onError = (error: Error) =>
      finish(
        new ManagedHostError(
          "HOST_PROCESS_ERROR",
          "Failed to start the QwenPaw Host process",
          { cause: error, diagnostics },
        ),
      );
    const onExit = (code: number | null, signal: NodeJS.Signals | null) =>
      finish(
        new ManagedHostError(
          "HOST_PROCESS_EXITED",
          `QwenPaw Host exited before launch (${code ?? signal ?? "unknown"})`,
          { diagnostics },
        ),
      );
    const timer = setTimeout(
      () =>
        finish(
          new ManagedHostError(
            "HOST_STARTUP_TIMEOUT",
            "Timed out waiting for the QwenPaw Host launch record",
            { diagnostics },
          ),
        ),
      timeoutMs,
    );

    child.stdout.on("data", onStdout);
    child.stderr.on("data", onStderr);
    child.once("error", onError);
    child.once("exit", onExit);
  });
}

async function waitForHandshake(
  child: ChildProcessWithoutNullStreams,
  client: QwenPawClient,
  requiredFeatures: readonly RuntimeFeature[],
  timeoutMs: number,
): Promise<RuntimeHandshake> {
  const deadline = Date.now() + timeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new ManagedHostError(
        "HOST_PROCESS_EXITED",
        "QwenPaw Host exited before becoming ready",
      );
    }
    const controller = new AbortController();
    const remaining = Math.max(1, deadline - Date.now());
    const timer = setTimeout(
      () => controller.abort(),
      Math.min(1_000, remaining),
    );
    try {
      const handshake = await client.runtime.handshake(controller.signal);
      const missing = missingRuntimeFeatures(handshake, requiredFeatures);
      if (missing.length > 0) {
        throw new ManagedHostError(
          "HOST_FEATURE_MISSING",
          `QwenPaw Host is missing required features: ${missing.join(", ")}`,
        );
      }
      return handshake;
    } catch (error) {
      if (
        error instanceof ManagedHostError ||
        error instanceof RuntimeClientError ||
        error instanceof QwenPawHttpError
      ) {
        throw error;
      }
      lastError = error;
    } finally {
      clearTimeout(timer);
    }
    await delay(Math.min(100, Math.max(1, deadline - Date.now())));
  }
  throw new ManagedHostError(
    "HOST_HANDSHAKE_FAILED",
    "Timed out waiting for a compatible QwenPaw Host handshake",
    { cause: lastError },
  );
}

export class QwenPawHost {
  readonly client: QwenPawClient;
  readonly chats: QwenPawChats;
  readonly handshake: RuntimeHandshake;
  readonly apiUrl: string;
  readonly pid: number;
  readonly exited: Promise<ManagedHostExit>;

  readonly #child: ChildProcessWithoutNullStreams;
  readonly #lifecycle: ManagedHostLifecycle;
  readonly #shutdownTimeoutMs: number;
  #closePromise: Promise<void> | undefined;

  private constructor(
    child: ChildProcessWithoutNullStreams,
    launch: ManagedHostLaunch,
    client: QwenPawClient,
    handshake: RuntimeHandshake,
    shutdownTimeoutMs: number,
  ) {
    this.#child = child;
    this.#lifecycle = { closing: false, forced: false };
    this.#shutdownTimeoutMs = shutdownTimeoutMs;
    this.client = client;
    this.chats = new QwenPawChats(client);
    this.handshake = handshake;
    this.apiUrl = launch.api_url;
    this.pid = launch.pid;
    this.exited = observeManagedHostExit(child, this.#lifecycle);
  }

  static async create(options: QwenPawHostOptions): Promise<QwenPawHost> {
    if (!options.runtime.executable.trim()) {
      throw new TypeError("runtime.executable must not be empty");
    }
    const startupTimeoutMs = validateTimeout(
      "startupTimeoutMs",
      options.startupTimeoutMs ?? DEFAULT_STARTUP_TIMEOUT_MS,
    );
    const shutdownTimeoutMs = validateTimeout(
      "shutdownTimeoutMs",
      options.shutdownTimeoutMs ?? DEFAULT_SHUTDOWN_TIMEOUT_MS,
    );
    const startupDeadline = Date.now() + startupTimeoutMs;
    const args = [
      ...(options.runtime.args ?? []),
      "app",
      "--managed",
      "--host",
      "127.0.0.1",
      "--port",
      "0",
      "--log-level",
      "warning",
    ];
    const env: NodeJS.ProcessEnv = { ...process.env, ...options.env };
    if (options.stateDir) env.QWENPAW_WORKING_DIR = options.stateDir;
    const child = spawn(options.runtime.executable, args, {
      cwd: options.cwd,
      env,
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    try {
      const launch = await waitForLaunch(
        child,
        startupTimeoutMs,
        options.onOutput,
      );
      const clientOptions: FetchTransportOptions = {
        baseUrl: launch.api_url,
        token: options.token,
        agentId: options.agentId,
        headers: options.headers,
      };
      const client = createQwenPawClient(clientOptions);
      const handshake = await waitForHandshake(
        child,
        client,
        options.requiredFeatures ?? [],
        Math.max(1, startupDeadline - Date.now()),
      );
      return new QwenPawHost(
        child,
        launch,
        client,
        handshake,
        shutdownTimeoutMs,
      );
    } catch (error) {
      await terminate(child, shutdownTimeoutMs);
      throw error;
    }
  }

  close(): Promise<void> {
    this.#lifecycle.closing = true;
    this.#closePromise ??= terminate(
      this.#child,
      this.#shutdownTimeoutMs,
      this.#lifecycle,
    );
    return this.#closePromise;
  }

  async [Symbol.asyncDispose](): Promise<void> {
    await this.close();
  }
}
