# QwenPaw PawApp / SDK Runtime Refactor

Status: accepted for incremental implementation
Scope: Lite Chat-first infrastructure; Task Workbench UI remains deferred

## 1. Decision

PawApp remains a developer-facing application and UI SDK. It does not own a
second task runtime. New server-side behavior must enter QwenPaw through a
stable Contribution and execute through the same Host-owned Task, Run,
Invocation, Interaction, Action, Artifact, Evidence and Event contracts used
by built-in capabilities and plugins.

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

- replace the current structural Projection fields with generated or shared
  schema types so frontend API modules and PawApp SDK cannot drift;
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

## 10. Explicit non-goals

- Do not build the Task Workbench page during this migration.
- Do not expose `TaskManager`, filesystem stores, registries or AgentScope
  objects through the public SDK.
- Do not create `/v2` routes or a second source of truth.
- Do not serialize Python coroutine stacks as checkpoints.
- Do not claim browser reconnect, restart recovery or hot replacement from
  unit tests alone.
