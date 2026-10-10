# QwenPaw PawApp / SDK Runtime Refactor

Status: accepted for incremental implementation
Scope: Lite Chat-first infrastructure; Task Workbench UI remains deferred

## 1. Decision

PawApp remains a developer-facing application and UI SDK. It does not own a
second task runtime. New server-side behavior must enter QwenPaw through a
stable Contribution and execute through the same Host-owned Task, Run,
Invocation, Interaction, Action, Artifact, Evidence and Event contracts used
by built-in capabilities and plugins.

The public SDK is a thin, transport-neutral facade over those existing Host
capabilities. It must be reusable by the Console, PawApps and later npm or
Python consumers instead of reimplementing routing, lifecycle or domain
semantics in each surface.

The existing `@app.task` API becomes a compatibility adapter. It remains
available while applications migrate, but its in-memory `TaskManager`,
`TaskRecord` and queue-backed `SSEChannel` are not promoted into Kernel
contracts and receive no new product semantics.

## 2. Current-state findings

| Concern             | Current PawApp path                                  | Problem                                                                 |
| ------------------- | ---------------------------------------------------- | ----------------------------------------------------------------------- |
| execution identity  | random `task_id` also used as Invocation UUID        | identity is local to a process and does not bind a Task/Run             |
| lifecycle           | module singleton `TaskManager._tasks`                | restart loses running and terminal facts                                |
| event delivery      | one in-memory `asyncio.Queue`                        | no durable cursor, replay or multi-consumer contract                    |
| frontend completion | closed stream without terminal event resolves `null` | transport loss can be mistaken for successful completion                |
| user input          | durable Interaction plus PawApp SSE event            | durable fact exists, but the delivery adapter was incorrectly mandatory |
| cancellation        | direct cancellation of one Python task               | no common control receipt, checkpoint or recovery decision              |
| artifacts           | arbitrary event payloads                             | no ownership, digest, renderer or Evidence contract                     |
| plugin replacement  | live handler closure                                 | no explicit capability generation pin or fallback evidence              |

## 3. Target ownership

```text
PawApp frontend
  -> PawApp SDK facade
    -> Task Application / Chat Application HTTP adapters
      -> Kernel ports and stable domain models
        -> generation-pinned system or plugin Contribution
          -> unified Runtime execution and durable facts

Compatibility only:
legacy @app.task
  -> Legacy PawApp Task Adapter
    -> the same application use cases above
```

The SDK may simplify calls, subscribe to projections and render extension UI.
It must not decide whether a Task succeeded, whether an Interaction is open,
whether an Action can retry, or whether an Artifact is trusted.

## 4. Public capability model

New PawApp development uses existing public slots rather than PawApp-specific
backend interfaces:

| Developer intent           | Stable slot / contract                          |
| -------------------------- | ----------------------------------------------- |
| model-facing operation     | `tool.provider`                                 |
| external system operation  | `driver.provider`                               |
| long-running task strategy | `runner`                                        |
| external agent harness     | `harness.runner`                                |
| stored memory              | `memory.provider`                               |
| scheduled trigger          | `scheduler`                                     |
| outbound notification      | `delivery.adapter`                              |
| artifact preview           | `artifact.renderer`                             |
| frontend panel or card     | `ui.page`, `ui.panel`, `ui.card`, `ui.settings` |

The Host creates and owns Task, Run, Invocation, Action, Interaction,
Artifact, Evidence and Verification identities. A plugin never invents a
parallel task identifier or persists an authoritative lifecycle in its UI.

## 5. SDK behavior

### 5.1 SDK layers

```text
@qwenpaw/contracts (generated when schema publication is available)
  <- @qwenpaw/client (transport-injected REST + event stream client)
    <- Console API facade
    <- @qwenpaw/pawapp-sdk (Host-scoped application/UI conveniences)
    <- future external TypeScript SDK

QwenPaw Kernel / Runtime remains the only execution authority.
```

The browser-only PawApp facade is not published as the whole QwenPaw SDK.
Reusable clients own path construction, request/response mapping and event
decoding. Surface-specific layers own only authentication transport,
ChatSpec defaults, capability policy, retry ergonomics and UI integration.

This boundary follows the useful parts of current coding-agent SDKs:

- Codex TypeScript SDK starts the packaged Codex CLI and exchanges JSONL over
  stdin/stdout; its `Thread` owns repeated turns and exposes buffered or async
  event-stream execution without recreating the Rust harness;
- GitHub Copilot SDK describes itself as a transport to the CLI over JSON-RPC,
  while the CLI retains orchestration and durable session behavior;
- Claude distinguishes low-level API clients from the Agent SDK/Claude Code
  runtime, where the latter owns the agent loop and tool execution;
- Gemini CLI SDK embeds the same configurable Agent core and offers Session,
  resume, custom tool and skill primitives instead of shelling out to a second
  implementation;
- OpenCode separates a generated remote client from an embedded SDK Host; both
  share the same promises, declared errors and async event streams.

QwenPaw therefore keeps two explicit products instead of calling every layer
an SDK:

- `@qwenpaw/client` connects to a running Lite, Workstation or Hub Host and is
  safe for Console, PawApp and third-party integrations;
- a future `@qwenpaw/sdk` may own the lifecycle of a compatible local Host and
  expose thread-like ergonomic handles, but it must compose the same Host and
  client package rather than implement another Agent loop.

This borrows the lifecycle lesson from Codex and the remote/embedded boundary
from OpenCode. It does not reuse either product's SDK or runtime. A missing or
mismatched QwenPaw executable must be detected through Host handshake and
package compatibility, not discovered only after the first developer call.

Primary references used for this decision:

- [OpenAI Codex SDK](https://github.com/openai/codex/tree/main/sdk/typescript)
- [OpenAI Codex Python SDK](https://github.com/openai/codex/tree/main/sdk/python)
- [Gemini CLI SDK design](https://github.com/google-gemini/gemini-cli/blob/main/packages/sdk/SDK_DESIGN.md)
- [OpenCode remote client](https://docs.opencode.ai/docs/sdk/)
- [OpenCode embedded SDK](https://opencode.ai/v2/docs/build/sdk)
- [Claude SDK and agent-runtime distinction](https://platform.claude.com/docs/en/cli-sdks-libraries/overview)

#### 5.1.1 Coding-agent SDK comparison update

The 2026-10-10 review distinguishes an API client from an agent-runtime SDK.
Calling both products an SDK hides lifecycle, compatibility and trust
boundaries that developers need to understand.

| Product | Runtime ownership | Public working object | Reusable lesson | Boundary not to copy |
| --- | --- | --- | --- | --- |
| Codex TypeScript | SDK spawns packaged CLI and reads JSONL | `Thread` / turn | Buffered and streamed calls share one thread identity | Early wrapper exposes only the subset supported by `exec` |
| Codex Python | SDK owns an exactly pinned CLI runtime and app-server client | `Thread`, `TurnHandle`, notifications | Explicit start/resume/fork, sync/async parity, steer/interrupt, approvals and close | Runtime and SDK version drift must not be left to first-call failure |
| GitHub Copilot | CLI owns the loop; SDK is JSON-RPC transport | Session | SDK forwards typed events and does not reimplement orchestration | Mechanical idle must not be presented as semantic task completion |
| OpenCode | Remote client or embedded Host share one generated API | Session and event groups | Embedded and remote modes return the same values, errors and streams | Embedded mode must not create a second domain model |
| Gemini CLI | SDK composes the existing configurable Agent core | Agent / Session | Session context, resume, tools and skills are composable primitives | Unfinished approval and policy surfaces are not a stable template |
| Claude | Agent SDK owns the loop; API SDK only calls the model | Query / session stream | Product naming makes runtime ownership explicit | A model client is not an agent SDK |

The comparison changes the QwenPaw recommendation in four concrete ways:

1. `@qwenpaw/client` remains the remote, zero-runtime client. It connects to an
   already running Lite, Workstation or Hub and contains no process manager,
   Agent Loop, Registry or authoritative queue.
2. `qwenpaw.plugins.sdk` remains the Python extension-author contract. It
   declares Contributions and capability-scoped Host services; it is not the
   API for embedding or remotely controlling QwenPaw.
3. A local-host SDK is useful, but its first implementation should reuse the
   installed QwenPaw distribution and existing Host application. A future
   `@qwenpaw/sdk` owns a managed local Host process and delegates every call to
   `@qwenpaw/client`; a Python `qwenpaw.sdk` facade may own the same Host
   lifecycle without exporting App, Registry, Store or AgentScope internals.
4. Exact runtime compatibility is an install/startup concern. A managed SDK
   must either ship an exactly pinned runtime or require an explicit executable
   and reject an incompatible Host during handshake before accepting work.
   Silently selecting any executable on `PATH` is not acceptable.

The public developer vocabulary is therefore:

```text
Extension authoring: qwenpaw.plugins.sdk -> Contribution / Capability
Remote integration: @qwenpaw/client     -> QwenPawClient
Managed local use:  @qwenpaw/sdk        -> QwenPawHost + same QwenPawClient
Application UI:     PawApp facade       -> host-scoped UI conveniences
```

The managed SDK should expose explicit lifecycle rather than another global
singleton:

```ts
await using host = await QwenPawHost.create({ workspace, runtime })
const chat = await host.chats.open({ chatId })
for await (const event of chat.sendStream(input, { signal })) {
  // The same versioned Host events returned by @qwenpaw/client.
}
```

This is an ergonomic handle, not a new source of truth. `chatId` remains
`ChatSpec.id`; Queue, Interaction, Action, Artifact and token-usage facts remain
Host-owned. `send()` may buffer the same stream, but stream EOF is never a
successful turn without an authoritative terminal receipt. Fork, resume,
steer, interrupt and approval are thin methods over existing Host commands.

The minimum viable managed SDK is intentionally small:

- discover or start one compatible local Host and wait for readiness;
- negotiate protocol and required features before creating a Chat handle;
- return the existing `QwenPawClient` surface plus scoped Chat handles;
- propagate cancellation and close child resources deterministically;
- preserve typed Host errors and events without translating them into a
  second SDK-only lifecycle;
- accept untrusted external messages through an explicitly lower-trust input
  type rather than treating every string as a user instruction.

The Host-side launch contract is now versioned independently from the HTTP
protocol. `qwenpaw app --managed --host 127.0.0.1 --port 0` pre-binds a
loopback socket and emits exactly one machine-readable line:

```text
QWENPAW_MANAGED_HOST {"schema":"qwenpaw.managed-host-launch.v1","api_url":"http://127.0.0.1:<port>/api","pid":<pid>}
```

This record proves process ownership and the actual bound address, not
application readiness. The SDK must continue polling the advertised
`/api/version` endpoint and validate `qwenpaw.host-handshake.v1` before it
returns a client. Managed mode rejects non-loopback binds and reload, does not
overwrite the user's last-connected API address, and retains the normal Host
graceful-shutdown path. These rules avoid the unsafe "find a free port, close
it, then spawn" race without introducing another transport protocol.

Deferred until those guarantees pass conformance tests: runtime bundling for
every platform, in-process Node embedding, SDK-only tool loops, SDK-owned
checkpoints and SDK-specific Task state. The existing Python application has
substantial process-global lifecycle, so an isolated managed Host process is
the safer first implementation than pretending it is already an embeddable
library.

#### 5.1.2 Codex reference boundary

Codex is a comparison target, not an implementation dependency for the
QwenPaw SDK. Its thread handle, buffered/streamed parity, explicit lifecycle,
fork, steer, interrupt and typed event design are evidence used to evaluate
QwenPaw's public ergonomics. They do not authorize replacing QwenPaw Host,
Kernel, execution contracts or client transport with the Codex SDK.

The optional `openai-codex` package belongs only to the Codex Harness adapter.
That adapter translates one external engine into QwenPaw's provider-neutral
`harness.runner` contract. It must not leak Codex Thread, Turn, approval or
event types into `@qwenpaw/client`, `@qwenpaw/sdk`, `qwenpaw.plugins.sdk` or
Kernel contracts.

The resulting rules are:

- QwenPaw SDK work starts from QwenPaw's existing Host APIs and generated
  contracts; it does not adopt the Codex SDK as its transport or runtime;
- Codex-specific process and protocol code stays behind the Harness adapter;
- useful Codex behavior is copied only as a product requirement and is proved
  independently against QwenPaw's own domain models and conformance fixtures;
- replacing the internal Codex Harness transport, if ever desired, is a
  separate adapter decision and is not part of the OS 3.0 SDK roadmap.

### 5.2 Contract publication

Kernel Pydantic models are the contract source of truth. The repository
exports deterministic JSON Schema under `schemas/sdk` and generates frontend
TypeScript declarations under `packages/qwenpaw-client/src/generated`. Stable
frontend names are compatibility aliases over those generated declarations;
they are not parallel handwritten models.

The publication boundary currently includes Host negotiation, Interaction
requests and resolutions, Chat submission/control requests, authoritative
Queue and Control receipts, and the complete Task projection. CI-compatible
checks fail when either the committed JSON Schema or generated TypeScript is
stale. Capability identifiers in the Host handshake remain open strings so an
older SDK accepts optional features introduced by a newer Host; protocol
version and required feature checks still provide the compatibility gate.

The publishable `@qwenpaw/client` package owns the generated declarations,
transport-neutral Runtime, Task and Interaction clients, and a zero-dependency
Fetch transport. Console and PawApp retain their existing import paths as
compatibility re-exports of that package source. An external consumer can use
`createQwenPawClient()` against a Lite, Workstation or Hub `/api` endpoint;
the package never starts a second runtime or stores authoritative Task state.

`paw.chatControls` publishes submission, queue, steer, interrupt,
stop-and-clear, queued cancellation and reorder over `ChatSpec.id`. Console
uses this same client implementation. Queue order and command receipts remain
Host facts; the SDK carries optimistic revisions and idempotency keys but owns
no queue state. Steer acceptance is distinct from application, and the applied
receipt records the Runtime safe point (`before/after reasoning` or
`before/after tool batch`).

### 5.3 Developer-facing execution

New applications use `paw.tasks.run()` and receive a projection-backed
handle. `paw.api` remains the PawApp-private HTTP namespace, so developers do
not have to guess whether `task()` means a private endpoint or a Kernel Task.

The projection-backed handle guarantees:

- `taskId` is the real Kernel Task identity;
- `runId` and `invocationId` are distinct and exposed only when present;
- `result` resolves only from a durable terminal Task/Run projection;
- an interrupted stream reconnects from a server cursor and never resolves
  `null` merely because transport ended;
- `cancel()` submits an idempotent application command and returns its durable
  receipt;
- `respond()` writes an `InteractionResponse` with expected revision;
- progress is derived from Execution Events;
- output files and previews are Artifact/Evidence references, not arbitrary
  trusted URLs or inline bytes.

The eventual external client should provide both ergonomic buffered calls and
an async event stream. Completion must come from an explicit terminal runtime
fact, never from stream closure alone. Mechanical idleness and semantic task
completion remain separate concepts.

The existing `paw.api.task()` spelling remains a deprecated compatibility
adapter with its original PawApp endpoint semantics. It will not silently
change meaning. Migration is explicit from `paw.api.task()` to
`paw.tasks.run()`.

## 6. Interaction rule

`ctx.ui.confirm()` is an Interaction producer, not an SSE primitive.

1. The Host persists a `USER_INPUT` Interaction owned by
   `agent_id + ChatSpec.id + invocation_id`.
2. Chat runtime projection and Interaction HTTP APIs are authoritative.
3. PawApp SSE may send an immediate convenience notification.
4. Missing or disconnected SSE never prevents creation, query, response or
   continuation.
5. Invocation cancellation closes its open Interactions exactly once.

This rule is implemented first because it removes a false transport
dependency without changing legacy handler execution.

## 7. Migration slices

### P0: detach Interaction from SSE

- allow confirmation creation without a delivery channel;
- prove query and response through `InteractionService` resumes the handler;
- retain the existing event envelope when SSE is available.

### P1: freeze compatibility and diagnostics

- classify `@app.task`, `TaskManager`, `TaskRecord` and `SSEChannel` as legacy;
- expose a structured, deduplicated `PawApp.task -> runner` diagnostic through
  the existing plugin management projection, including a v2 manifest skeleton;
- reject new PawApp APIs that attempt to create another approval, artifact or
  lifecycle store.

### P2: projection-backed frontend handle

- add Host capability negotiation;
- create or bind a real Task through the application service;
- subscribe with cursor replay and terminal projection reconciliation;
- make transport EOF an indeterminate state, never success; the legacy SDK
  now raises `PawTaskTransportError(PAW_TASK_STREAM_INTERRUPTED)` while cursor
  replay remains unavailable;
- route Interaction responses and cancellation through shared commands.

#### P2 implementation status

Implemented in the frontend SDK:

- `/api/version` now returns the versioned
  `qwenpaw.host-handshake.v1` contract while preserving the existing product
  `version`; clients negotiate protocol version and stable feature IDs before
  starting a Task, while dynamic capability availability remains in the
  generation-aware catalog;
- `paw.tasks.run()` creates and starts a real Kernel Task;
- explicitly requested `runner` and `strategy` capabilities are checked
  against the current Host registry before Task creation;
- Task creation and cancellation carry stable idempotency keys;
- the event monitor starts lazily on the first subscription or `result`
  access, preventing early replay events from racing listener registration;
- committed sequence numbers are deduplicated and reconnect through
  `Last-Event-ID` with bounded retries;
- every stream EOF or transport error is reconciled with the authoritative
  Projection; only `completed` resolves successfully;
- `failed` and `cancelled` reject with a typed error that retains the terminal
  Projection;
- repeated `cancel()` calls share one command Promise and return the
  Host-acknowledged Task plus the idempotency key.
- `paw.interactions` and `paw.host.interactions` expose one ChatSpec-scoped
  list/respond contract for approval, required user input and non-blocking
  suggestions;
- Interaction responses require the expected revision, reuse one idempotency
  key across transport retries and reject conflicting local responses before
  a second request is sent;
- a Chat-delivered Task approval is submitted only through the Interaction
  response route; the Host bridge reconciles its linked Approval fact, so SDK
  clients never dual-write both records.
- Task-only clients use `handle.decideApproval()` against the authoritative
  Task approval route; repeated identical decisions share one Promise and
  idempotency key, while conflicting local decisions are rejected before a
  second request is sent.

Still pending in P2:

- add a browser disconnect test against the running Host rather than only
  server replay plus SDK unit tests.

### P3: backend Contribution adapter

- adapt registered legacy task handlers to generation-pinned runner
  Contributions during application activation;
- pin the handler generation for the whole Run;
- publish progress, result and failure through Execution Events;
- capture declared outputs as Artifact/Evidence references;
- preserve an explicit compatibility fallback until conformance passes.

### P4: recovery and replacement

- recover safe checkpoints after process restart;
- prove hot replacement does not move an active Run to a new generation;
- prove failed replacement keeps the previous generation executable;
- remove the in-memory manager only after repository and installed-app usage
  reach the documented deletion threshold.

## 8. Acceptance gates

The PawApp runtime migration is complete only when all of the following have
real evidence:

1. Browser disconnect and reconnect resumes from a durable cursor without
   duplicate events or false success.
2. An open confirmation is visible and answerable without the original SSE
   connection; the original handler continues once.
3. Restart restores a safe long-running operation or reports an explicit
   recovery-required terminal state.
4. Cancel produces one durable receipt and closes owned Interactions.
5. Artifact and Evidence output survives refresh and passes ownership/hash
   checks.
6. Hot install, replacement, failed replacement and unload preserve pinned
   generation behavior.
7. Built-in and plugin runners pass the same behavioral contract suite.
8. Legacy applications receive actionable migration diagnostics and continue
   to run until the published removal threshold is met.

Component existence, mocked UI cards and an open SSE connection are not
completion evidence.

## 9. Verification evidence

Verified on 2026-10-10 against the running Lite Host on port 8004:

- registry generation 11 exposed
  `qwenpaw.system.tasks.console-agent` in the `runner` slot;
- a real Task created through `POST /api/tasks`, started through the shared
  application route and reached the durable `completed` status;
- its terminal Projection reported `last_sequence = 7` and pinned both the
  runner and default strategy to registry generation 11;
- reconnecting with `Last-Event-ID: 5` replayed exactly sequence 6 and 7;
- querying the current ChatSpec through the real Interaction route returned an
  authoritative empty open-interaction projection rather than a transport or
  PawApp-local queue;
- PawApp SDK targeted tests cover terminal reconciliation, cursor reconnect,
  missing runner rejection, listener timing, idempotent cancellation,
  revision conflicts, direct Task approval conflicts and idempotent
  Interaction transport retries. No existing Task was awaiting approval at
  verification time, so a real direct-approval mutation was intentionally not
  performed or claimed.
- Console `tasksApi`, Tasks UI and PawApp SDK now import Task, Run, Plan,
  Event, Approval, Artifact, Evidence and Projection contracts from the same
  neutral `console/src/contracts` source; their legacy import paths remain
  compatibility re-exports.
- Console `tasksApi` and `paw.tasks` now reuse one transport-injected
  `createTaskClient()` for Task routes, ID encoding, Artifact paths, approval
  commands and SSE cursor parsing. The SDK layer retains only developer-facing
  capability checks, event listeners, reconnect policy and terminal
  Projection semantics.
- Console Chat APIs and `paw.interactions` now reuse one transport-injected
  `createInteractionClient()` for ChatSpec-scoped routes, ID encoding and
  decision payloads. The PawApp facade retains ChatSpec defaulting, typed
  errors and idempotent retry/conflict behavior.
- Console `rootApi` and `paw.runtime` now reuse one transport-injected runtime
  client for Host negotiation. PawApps can inspect or require stable Host
  features without treating plugin capability discovery as protocol
  compatibility.
- The complete Task Workbench projection now has a strict public Pydantic
  response model and an OpenAPI component. This freezes the server-side source
  needed for generated SDK contracts and validates every projection before it
  leaves the Host.
- Host, Interaction and Task projection contracts are exported from Kernel
  models as deterministic JSON Schema and compiled into TypeScript. Console
  and PawApp SDK compatibility modules now alias those generated types, while
  drift checks cover both publication stages.
- The same contracts and transport-neutral clients now live in the publishable
  `@qwenpaw/client` package. A prepack-built tarball was installed into an
  empty temporary npm consumer, imported by its public package name and used
  to negotiate successfully with the running Lite Host on port 8004. The
  package contained only declarations, ESM output, metadata and the developer
  README.
- The initial `@qwenpaw/sdk` package now owns only managed local Host
  lifecycle. Callers provide an explicit executable; the SDK starts
  `app --managed`, validates launch identity and loopback URL, polls the same
  Host handshake, checks required features and exposes the existing
  `QwenPawClient`. Startup failure always terminates the owned process and
  `close()` is idempotent.
- `@qwenpaw/client` now exposes ChatSpec creation, history, message-scoped fork,
  runtime snapshots and cursor-aware runtime streaming. `@qwenpaw/sdk` adds a
  `host.chats.open(ChatSpec.id)` convenience handle whose submission, queue,
  steer, interrupt, stop-and-clear, Interaction response and fork methods all
  delegate to that shared client. The handle owns no queue or completion state;
  stream EOF returns only the last cursor and never means task success.
- Submission tracking now stays in the shared client: it locates the durable
  execution chain by `submission_id`, emits `waiting_user` without inventing
  completion, reconciles every EOF or transient disconnect against the latest
  Host projection, and reconnects from its cursor. The managed SDK adds only a
  thin `QwenPawTurn` wrapper. Mechanical `inactive` remains distinct from the
  verified Outcome states `achieved`, `partial`, `not_achieved` and
  `abandoned`; a truncated projection window fails closed instead of guessing.
- Managed Host lifecycle is now observable after readiness through one
  immutable `host.exited` Promise. It distinguishes SDK-initiated shutdown from
  an unexpected runtime exit using the owned PID, exit code/signal and explicit
  forced-kill flag; it does not introduce SDK-side restart state. AbortSignal
  cancellation also reaches an active submission stream and reconnect delay
  without being swallowed. The default graceful window is ten seconds, while
  post-SIGKILL observation is capped at one second.
- Nine real child-process tests cover ready, missing feature, incompatible or
  malformed handshake, malformed launch or API root, early exit, spawn failure
  and launch timeout.
  A real `qwenpaw-codex` executable was also started against an isolated state
  directory: it advertised a random loopback API, protocol 1 and five Host
  features, completed the handshake, and exited through SDK ownership.
- Packed `@qwenpaw/client` and `@qwenpaw/sdk` tarballs were installed together
  in an empty npm consumer. Both package entry points imported by public name;
  the SDK tarball contained only declarations, ESM output, metadata and its
  README.
- `chat.send()` now accepts either the stable low-level submission contract,
  direct text or an explicit lower-trust external input. The Host, rather than
  the SDK, validates the trust/source pair, overwrites spoofable metadata,
  protects model-visible external content and records it as `EXTERNAL` in the
  Context Manifest. Issue, PR, email, webhook and agent content therefore do
  not silently gain user authority.
- Chat result inspection now reuses generated Kernel contracts for Actions,
  Artifact/Evidence records, Observation pages and correlation Trajectories.
  Both remote and managed clients read those Host-owned facts through
  `ChatSpec.id`; Artifact bytes still pass through the shared safe renderer.
  Forked Chats list only receipts visible in their persisted message snapshot,
  so a parent Artifact created after the fork cannot leak into the child.
- Recovery observability now exposes generated `WaitCondition` and
  `ModelCallRecord` contracts on the same Chat handle. Consumers can distinguish
  approval/user-input, timer, external-event and resource blockers, inspect the
  continuation mode and correlate provider attempts without reading private
  checkpoint storage. The SDK deliberately has no generic replay operation:
  Interaction decisions, resource release and checkpoint/outbox dispatch remain
  Host-owned so an integration cannot accidentally repeat a side effect.
- Remote/managed conformance now exercises both public entry points against the
  same managed Host fixture. Chat history, runtime snapshots, snapshot-stream
  cursor termination and typed HTTP failures remain deeply equal at the public
  contract boundary.
  This intentionally verifies transport parity without relabelling the
  latest-state stream as a replayable execution-event log.
- Ending runtime iteration early now cancels the underlying response body before
  releasing its reader. A consumer can stop after one snapshot without leaving
  a Node HTTP connection alive; cancellation failure cannot hide the original
  transport error.
- AbortSignal propagation is covered through a managed child Host and a live
  HTTP/SSE turn. Aborting `Turn.follow()` closes observation promptly while the
  authoritative execution chain remains `running`; callers must use the
  explicit `interrupt()` command to stop execution.
- A managed Host crash is also exercised after a turn stream is active. The
  immutable `host.exited` promise reports an unexpected exit independently,
  while the stream fails rather than manufacturing an inactive or successful
  terminal state.
- On POSIX, the managed Host is now started in an SDK-owned process group.
  Graceful shutdown targets the group and timeout escalation sends `SIGKILL`
  to that same ownership boundary. A real descendant that ignores `SIGTERM`
  proves close does not leave an orphan even when the parent exits cleanly
  first. During SDK-owned close, `host.exited` waits for group cleanup before
  reporting the final `forced` flag; unexpected parent exits still notify
  immediately. Windows tree cleanup remains an explicit platform-validation
  gap rather than an inferred guarantee.

## 10. Explicit non-goals

- Do not build the Task Workbench page during this migration.
- Do not expose `TaskManager`, filesystem stores, registries or AgentScope
  objects through the public SDK.
- Do not create `/v2` routes or a second source of truth.
- Do not serialize Python coroutine stacks as checkpoints.
- Do not claim browser reconnect, restart recovery or hot replacement from
  unit tests alone.

## 11. Primary references

- [OpenAI Codex app-server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)
- [OpenAI Codex as a platform](https://developers.openai.com/blog/codex-as-a-platform)
- [OpenAI Codex TypeScript SDK](https://github.com/openai/codex/blob/main/sdk/typescript/README.md)
- [OpenAI Codex Python SDK](https://github.com/openai/codex/blob/main/sdk/python/README.md)
- [OpenAI Codex Python SDK FAQ](https://github.com/openai/codex/blob/main/sdk/python/docs/faq.md)
- [Codex Python SDK deferred approval request](https://github.com/openai/codex/issues/42219)
- [Codex Python SDK approval default report](https://github.com/openai/codex/issues/27277)
- [Codex Python SDK async cancellation report](https://github.com/openai/codex/issues/51542)
- [GitHub Copilot SDK agent loop](https://github.com/github/copilot-sdk/blob/main/docs/features/agent-loop.md)
- [GitHub Copilot SDK streaming events](https://docs.github.com/en/copilot/how-tos/copilot-sdk/use-copilot-sdk/streaming-events)
- [Claude Agent SDK TypeScript](https://github.com/anthropics/claude-agent-sdk-typescript)
- [Claude Agent SDK migration example](https://platform.claude.com/cookbook/claude-agent-sdk-04-migrating-from-openai-agents-sdk)
- [Gemini CLI SDK design](https://github.com/google-gemini/gemini-cli/blob/main/packages/sdk/SDK_DESIGN.md)
- [Gemini CLI extension reference](https://github.com/google-gemini/gemini-cli/blob/main/docs/extensions/reference.md)
