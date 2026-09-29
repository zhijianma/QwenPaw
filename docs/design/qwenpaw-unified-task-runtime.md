# QwenPaw Unified Task Runtime Redesign

Status: In progress — R0/R1/R3 core paths implemented

Date: 2026-09-22

Target branch: `feat/lite-agent-os`

Normative API and event details are defined in
`qwenpaw-task-runtime-contract.md`.

Supersedes the runtime-completion claims in
`qwenpaw-lite-acceptance.md`. It does not supersede the shared Kernel,
Ledger, artifact store, or generation-registry code that remains valid.

## 1. Outcome

QwenPaw Lite will have one task runtime for built-in and third-party
capabilities. A built-in implementation is a trusted, system-owned
contribution; it is not a second execution path. Plugins use the same
contribution contract, capability registry, generation lease, runtime context,
approval broker, event sink, artifact sink, and failure semantics.

The Task Workbench consumes an explicit task projection. It must not infer
pending approvals, conversation state, or current execution state by joining
unrelated frontend stores.

This redesign preserves existing Chat, Driver, Harness, Tool Guard, Sandbox,
MCP, and plugin behavior through adapters while their call sites migrate.

## 2. Evidence-backed current-state matrix

The following matrix describes the current branch, not the intended design.

| Capability | Current owner / path | Durable Task integration | Built-in/plugin parity | Finding |
|---|---|---:|---:|---|
| Task lifecycle | `TaskRuntimeOrchestrator`, `TaskService`, SQLite Ledger | Yes | Not applicable | Start/cancel/suspend/resume are orchestrator-owned |
| Native Agent execution | system Console runner contribution | Yes | Yes | Uses the same pinned catalog and typed context as plugins |
| Contributed runner | `GenerationRegistry` + `TaskExecutionCoordinator` | Yes | Yes | System and plugin runners share dispatch semantics |
| Planning | system planner contribution | Yes | Yes | Planner is resolved from the pinned generation |
| Conversation | `ConsoleStreamCapture` plus Task projection | Yes | Yes | Public projection assembles user and assistant messages |
| Tool execution | AgentScope tool registry and Tool Guard | Partial | No | Tool contribution slot is declared but not the common dispatch path |
| Tool approval | typed `ApprovalBroker` plus legacy delivery adapter | Yes | Yes | the run-scoped broker is injected by the coordinator; legacy reconstruction is fallback-only |
| Parallel approvals | `TaskService.request_approval` | Yes | Yes | concurrent requests retain distinct projections and events |
| Driver approval | `QwenPawDriverApprovalGate` plus Task bridge | Yes | Yes | durable Task approval is attached when task context exists |
| Harness approval | Codex/Qoder adapters plus Task bridge | Yes | Yes | both harnesses attach the same durable approval boundary |
| Approval query | Task approval projection | Yes | Yes | task-scoped pending and decided records are explicit |
| Approval decision | Task endpoint and legacy endpoints | Partial | No | duplicated decision surfaces with different ownership checks |
| Artifact storage | task-scoped `ArtifactEmitter` | Yes | Yes | system and plugin runners share content-addressed emission |
| Evidence | `ArtifactEmitter` evidence binding | Yes | Yes | evidence is bound to its artifact in one runner signal |
| Checkpoint | orchestrator checkpoint resume | Yes | Yes | a new run receives the validated checkpoint in typed context |
| Cancellation | orchestrator plus typed `CancellationToken` | Yes | Yes | cooperative cleanup precedes force-cancel; pending approvals resolve atomically |
| Failure/resume | orchestrator plus state machine | Yes | Yes | resume resolves the recorded runner and creates a new attempt |
| Sensors | `SensorContributionHost` | Yes | Plugin path | proactive built-in uses a separate adapter |
| Memory | existing Agent memory assembly | No | No | declared slot is not resolved through the task generation |
| Modes / strategy | `RuntimeStrategy` plus system default contribution | Yes | Yes for the default strategy | Coding/Goal/Mission adapters still need contribution wrappers |
| Plugin hot activation | `GenerationRegistry` | Yes for contributed runner/sensor | Plugin only | atomic publish and leases exist and should be retained |
| Built-in activation | system capability bundle | Yes | Yes | built-ins and plugins publish into immutable generations |
| Task UI live updates | authenticated replayable SSE | Yes | Not applicable | committed events drive debounced projection refresh |
| Task UI approval details | explicit Task approval DTO | Yes | Not applicable | source, risk, target, status, and decision are rendered |
| Task UI artifacts | artifact endpoint, preview, and evidence cards | Yes | Yes | content digest and task ownership are verified |

Typed runs now receive explicit artifact, approval, and cancellation services.
The remaining architectural gaps are the common Tool dispatch path, Memory
contribution migration, and wrappers for the existing non-default Agent modes.

## 3. Non-negotiable invariants

1. Every task run pins exactly one immutable capability generation before the
   first executable capability is resolved.
2. Built-ins and plugins are registered as `CapabilityContribution` records
   and resolved through the same lease.
3. The runtime orchestrator is the only component allowed to start, complete,
   fail, cancel, suspend, or resume a run.
4. An approval is committed to the Task Ledger before it is exposed to a user
   or awaited by a capability.
5. A task remains `waiting_approval` while at least one blocking approval is
   pending. Resolving one of several approvals must not resume the run early.
6. Approval decisions are idempotent and authorized against the task and
   request identity before the runtime waiter is released.
7. Tool, Driver, Harness, sensor, and system approvals use one broker.
8. Conversation messages, tool activity, approvals, artifacts, and evidence
   are bounded public records. Hidden reasoning and raw secrets are forbidden.
9. Large content is stored through `ArtifactStore`; events contain references.
10. A failed activation never mutates the published generation. Existing runs
    retain the old generation until their leases drain.
11. HTTP and UI layers can issue commands and read projections; they cannot
    own runtime lifecycle state.
12. Compatibility adapters may translate old contracts, but may not create a
    second source of truth.

## 4. Stable domain contract

Existing versioned models remain valid unless explicitly extended below.
New fields are added in a backward-compatible schema revision; persisted v1
records continue to load.

### 4.1 Task and run

`Task` remains the durable goal and `Run` remains one execution attempt.
Runtime selection moves out of untyped metadata into an explicit order:

```text
TaskOrder
├── task_id
├── objective
├── constraints
├── acceptance_criteria
├── requested_capabilities
├── approval_ids
└── execution
    ├── planner_id: str | null
    ├── runner_id: str
    ├── strategy_id: str | null
    ├── approval_level: strict | smart | auto | off
    └── project_dir: str
```

`Run.registry_generation` is mandatory for every execution, including the
native Agent runner. The generation is chosen at start and never rewritten.

### 4.2 Conversation

```text
ConversationMessage
├── message_id: UUID
├── task_id: UUID
├── run_id: UUID
├── role: user | assistant | system | tool
├── content: tuple[ContentPart, ...]
├── status: streaming | completed | failed
├── parent_message_id: UUID | null
├── created_at: datetime
└── completed_at: datetime | null
```

Streaming deltas are events; the read model returns assembled messages. Raw
provider events remain adapter-internal unless represented as bounded tool
activity.

### 4.3 Approval

`ApprovalRequest` gains public display and lifecycle fields rather than
depending on legacy `PendingApproval`:

```text
ApprovalRequest
├── approval_id
├── task_id
├── run_id
├── source: tool | driver | harness | proposal | system
├── action
├── risk
├── requester
├── policy
├── continuation
├── status: pending | approved | denied | expired | cancelled
├── display
│   ├── title
│   ├── summary
│   ├── target
│   └── provider
├── redacted_arguments
├── expires_at
└── created_at
```

The task/run status is a projection over pending blocking approvals:

```text
0 pending approvals  -> run may be running
1..N pending         -> task and run are waiting_approval
resolve one of N     -> remain waiting_approval while N - 1 > 0
resolve final allow  -> resume exactly once
deny/cancel          -> apply that request's continuation policy
```

The Ledger must support listing approvals by `task_id`, `run_id`, and status.
Raw event scans are not the public query contract.

### 4.4 Artifact and evidence

Every runtime capability receives an `ArtifactEmitter`:

```text
emit(content, kind, media_type, metadata) -> ArtifactRef
attest(artifact_id, claim, producer) -> EvidenceRef
```

The emitter stores content, commits an event containing the references, and
returns only after both operations succeed. Tools and plugins do not construct
filesystem URIs directly.

Chat attachments use the same references and content store. Because an upload
can finish before a Chat ID exists, the upload endpoint also returns an opaque
receipt. The first accepted send atomically claims that receipt for one Chat;
retries from that Chat are idempotent, while another Chat cannot claim or read
it. Persisted Chat messages expose typed `artifact_refs` and `evidence_refs`,
and their content endpoint resolves the same generation-pinned
`artifact.renderer` used by Task artifacts.

### 4.5 Capability and contribution

Rename the conceptual ownership from plugin-only to provider-neutral:

```text
CapabilityContribution
├── contribution_id
├── capability_id
├── slot
├── provider_id
├── provider_kind: system | plugin
├── version
├── restart_policy
├── input_schema
├── output_schema
├── config_schema
└── metadata
```

`PluginContribution` remains a manifest DTO and is converted into the common
record. Built-ins are declared by a system manifest and go through the same
validation and staging pipeline.

Initial runtime slots:

| Slot | Runtime protocol | Initial system implementation |
|---|---|---|
| `planner` | `TaskPlanner.plan(context, order)` | `qwenpaw.system.tasks.basic-planner` |
| `runner` | `TaskRunner.execute(order, run, context)` | `qwenpaw.system.tasks.console-agent` |
| `strategy` | `RuntimeStrategy.prepare(context)` | current Default/Coding/Goal adapters |
| `tool.provider` | `ToolProvider.list_tools(context)` | current AgentScope tool assembly |
| `driver.provider` | `DriverProvider.bind(context)` | current DriverManager adapter |
| `harness.runner` | `TaskRunner` | Codex and Qoder adapters |
| `memory.provider` | `MemoryProvider.open(context)` | current configured memory backend |
| `sensor` | `ProposalSensor.propose(context)` | proactive responder adapter |
| `artifact.renderer` | `ArtifactRenderer` | built-in safe text/download renderer |

Existing manifest slots are accepted through aliases during migration:
`tool -> tool.provider`, `memory -> memory.provider`, and
`engine -> strategy` where the implementation satisfies the target protocol.

## 5. Runtime ports

```text
RuntimeOrchestrator
├── CapabilityCatalog
├── TaskRepository
├── RuntimeSessionFactory
├── ApprovalBroker
├── ArtifactEmitterFactory
├── CheckpointProvider
└── EventPublisher
```

`RuntimeContext` is immutable and is the only request context exposed to a
runtime capability:

```text
RuntimeContext
├── task_id
├── run_id
├── invocation_id
├── correlation_id
├── agent_id
├── conversation_id
├── session_id
├── project_dir
├── registry_generation
├── approval_level
├── capability_ids
├── strategy: RuntimeStrategyDirective | null
│   ├── strategy_id
│   └── parameters
├── approval_broker
├── artifact_emitter
├── event_sink
└── cancellation
```

It replaces untyped `dict[str, Any]` propagation at the Task boundary. Generic
compatibility adapters do not copy Strategy parameters into Chat. A concrete
Runner may expose a validated, explicitly supported subset to its downstream
runtime.

## 6. Orchestrator lifecycle

```text
create task
  -> plan command
  -> pin capability generation
  -> resolve planner + runner + strategy + providers
  -> persist plan
  -> create run with pinned generation
  -> create RuntimeContext
  -> dispatch runner
       -> messages/tool activity/events
       -> ApprovalBroker.request (0..N)
       -> ArtifactEmitter.emit (0..N)
       -> CheckpointProvider.capture
  -> terminal signal
  -> commit completed/failed/cancelled
  -> close runtime session
  -> release generation lease
```

The orchestrator owns the execution handle. FastAPI stores no authoritative
`asyncio.Task` mapping; a Lite in-process supervisor is an orchestrator
adapter and can later be replaced by the Workstation durable scheduler.

## 7. Approval broker

`ApprovalBroker` replaces direct calls to the global ApprovalService inside
task executions.

```text
request(context, request) -> await decision
list(task_id, status?) -> approvals
decide(task_id, approval_id, command) -> decision
cancel_run(run_id) -> count
```

Implementation order:

1. validate and redact;
2. atomically persist request plus pending-count projection;
3. publish `approval.requested`;
4. expose the request to delivery adapters;
5. wait without owning the durable truth;
6. authorize and persist a decision;
7. update pending count and continuation result atomically;
8. release the matching waiter after commit.

The existing ApprovalService becomes a delivery/waiter adapter. Tool Guard,
Driver, Codex, Qoder, ACP, channels, and Console use the broker when a
`RuntimeContext` exists. Non-task chat continues through the legacy adapter
until migrated.

## 8. Read API and Task Workbench contract

The Task Workbench uses the following stable endpoints:

```text
POST /api/tasks
POST /api/tasks/{task_id}/start
POST /api/tasks/{task_id}/cancel
POST /api/tasks/{task_id}/resume
GET  /api/tasks/{task_id}/projection
GET  /api/tasks/{task_id}/events
GET  /api/tasks/{task_id}/stream
GET  /api/tasks/{task_id}/approvals?status=pending
POST /api/tasks/{task_id}/approvals/{approval_id}/decision
GET  /api/tasks/{task_id}/artifacts
GET  /api/tasks/{task_id}/artifacts/{artifact_id}/content
GET  /api/chats/{chat_id}/artifacts/{artifact_id}/content
```

`TaskProjection` contains:

```text
task, active_run, runs, latest_plan,
conversation_messages, tool_activities,
pending_approvals, recent_decisions,
artifacts, evidence, checkpoint, capabilities,
last_sequence
```

The SSE endpoint publishes committed sequence numbers. Reconnection supplies
`after_sequence`; the client then refreshes the projection. Polling remains a
fallback, not the primary consistency mechanism.

The approval card must show source, action, risk, redacted arguments, exact
target, provider, creation time, expiry, and continuation consequence. All
pending cards remain visible together.

## 9. Built-in and plugin registration

Startup activates a system bundle through the same catalog transaction used
for a plugin bundle:

```text
stage -> validate protocol -> health check -> build shadow generation
      -> atomic publish -> new runs pin new generation
```

Differences are limited to trust and lifecycle policy:

| Property | System contribution | Plugin contribution |
|---|---|---|
| Provider kind | `system` | `plugin` |
| Signature source | application release | plugin package policy |
| Disable permission | edition policy | user/plugin policy |
| Resolution/dispatch | identical | identical |
| Runtime context | identical | identical |
| Events/artifacts/approvals | identical | identical |

Hot replacement publishes one complete catalog generation. A run cannot mix
implementations from two generations, including tools and renderers.

## 10. Compatibility and migration

| Existing path | Decision | Migration adapter |
|---|---|---|
| `/console/chat/task` | Keep during Lite migration | translate to Task commands and projection |
| global `/approval/*` | Keep for non-task Chat | task requests delegate to ApprovalBroker |
| `ApprovalService` | Keep as delivery/waiter | remove task truth and authorization ownership |
| Console channel stream | Keep | system `TaskRunner` adapter |
| Tool Guard | Keep | use broker from `RuntimeContext` |
| Driver gate | Keep | use broker from `RuntimeContext` |
| Codex/Qoder adapters | Keep | implement common `TaskRunner` and broker interfaces |
| Agent modes | Keep | system strategy contributions |
| plugin manifest v1 | Keep | translate to common contribution records |
| frontend Slot registry | Keep | task projection passed as bounded slot context |
| process-local task map in router | Remove | Lite runtime supervisor owns handles |
| raw Task-event approval join in UI | Remove | use approval/projection API |

## 11. Implementation sequence and gates

### R0 — Contract and parity tests

- Add the capability matrix as an executable test inventory.
- Add domain models and protocols without redirecting runtime traffic.
- Specify multiple-pending approval state rules.

Gate: serialization, dependency, transition, and compatibility tests pass.

### R1 — Unified catalog

- Generalize the generation registry to system and plugin bundles.
- Register the native Console runner and basic planner as system
  contributions.
- Make Task start resolve the native runner from a pinned lease.

Gate: the same runner contract suite passes for one system runner and one
plugin runner; hot replacement does not alter an active run.

### R2 — Runtime orchestrator

- Move planning, execution supervision, cancellation, completion, failure,
  and lease release from the HTTP router into the orchestrator.
- Introduce typed `RuntimeContext` and the legacy context adapter.

Gate: API, direct application service, and sensor entry points produce the
same event sequence and terminal projection.

### R3 — Unified approvals

- Implement task approval queries and pending-count projection.
- Support multiple concurrent approval requests.
- Route Tool Guard and Driver approvals through the broker first, then
  Harness approvals.
- Replace Task UI raw-event inference with approval DTOs.

Gate: a real strict-mode task exposes two simultaneous approvals; approving
one keeps the task waiting; deciding the final request applies the declared
continuation and wakes only matching waiters.

### R4 — Conversation, artifacts, evidence, and checkpoint

- Add assembled conversation and tool-activity projections.
- Give every capability the common artifact/evidence emitter.
- Connect safe runtime-provider checkpoints and resume.

Gate: a README translation task shows its full conversation and produces a
previewable Italian artifact with evidence; a forced interruption resumes
from a safe checkpoint in a new run.

### R5 — Workbench and compatibility closure

- Switch the Task page to projection plus SSE reconnection.
- Render plans, messages, approvals, decisions, artifacts, evidence, runtime
  capabilities, errors, and recovery actions.
- Run targeted parity suites for Chat, Driver, Harness, MCP, plugins, and
  existing Console behavior.

Gate: browser execution proves the complete path. Mocked component tests are
supporting evidence only.

### R6 — Developer contract

- Publish SDK types, a system/plugin parity example, API/event reference,
  migration guide, slot catalog, and hot-reload diagnostics.

Gate: an example third-party capability installs without restart, executes in
a new task, requests approval, emits an artifact, and survives replacement of
its package while the old run completes on its pinned generation.

## 12. Acceptance matrix

Completion requires all rows to carry direct evidence.

| ID | Requirement | Required evidence | Current |
|---|---|---|---|
| U1 | system and plugin use one catalog | parity contract test plus runtime trace | Passed |
| U2 | one orchestrator owns lifecycle | source boundary test and API integration | Passed |
| U3 | typed runtime context | model tests and legacy adapter tests | Passed |
| U4 | strict real approval visible | browser/API runtime trace | Passed |
| U5 | multiple pending approvals | concurrency integration test | Passed |
| U6 | all approval producers converge | Tool/Driver/Harness contract tests | Passed |
| U7 | explicit approval projection | API and frontend tests | Passed |
| U8 | complete conversation projection | stream assembly integration test | Passed |
| U9 | common artifact/evidence emission | system/plugin parity tests | Passed |
| U10 | cancellation and checkpoint resume | interrupted runtime integration test | Passed |
| U11 | hot activation and pinned generation | replacement/rollback integration test | Passed |
| U12 | Task Workbench real interaction | browser walkthrough with recorded IDs | Passed |
| U13 | backward compatibility | targeted Chat/Driver/Harness/MCP suites | Partial |
| U14 | developer documentation | clean-room example walkthrough | Passed |
| U15 | strategy executes from pinned catalog | system/plugin contract tests and real runtime trace | Passed |
| U16 | Chat and Task share artifact semantics | receipt security tests and real Chat trace | Passed |

### Implementation evidence — 2026-09-22

- Strict approval browser run: task
  `c8d1c640-4dbf-4189-998d-5023906eabe6`; the Task page displayed the
  durable `Read` approval, resumed after the decision, and produced artifact
  `673bb003-86b5-425e-82dd-6e442de04fdc` with one evidence record.
- Typed-broker browser run: task
  `e66f5f08-279f-4b2d-b0fa-ee7e2c4f9a0f` received an injected run-scoped
  `ApprovalBroker`, displayed the exact `Read` arguments, resumed after the
  decision, and produced artifact
  `2704a5e3-ede1-4060-80a1-b83a4da860a7` with one evidence record.
- Cancellation browser run: task
  `420a8fe2-29dc-483b-bd28-edead70c0f47` was cancelled while its `Read`
  approval was pending. The projection returned no pending approvals and its
  terminal event suffix was `approval.requested`, `approval.decided`, then
  `run.cancelled`; the approval card disappeared from the Workbench.
- Strategy browser run: task
  `681ac448-c179-478d-a80f-3739b4642c47` persisted and resolved
  `qwenpaw.system.tasks.default-strategy` from the same pinned generation as the
  Console runner, exposed it in the Workbench, returned the expected dialogue
  result, and produced artifact
  `1174b07c-2ac2-4dfe-889e-240754cba632` with one evidence record. Legacy
  runs without a persisted strategy display `未分配` rather than being
  retroactively mislabeled.
- The active governance `PolicyGuardedTool`, legacy Tool Guard, Driver gate,
  Codex harness, and Qoder harness now attach the same durable Task bridge.
- Separate TaskService instances can create approvals concurrently; retries
  preserve both pending records and contiguous event sequences.
- The Workbench now reads plans, assembled conversation, approvals,
  artifacts, evidence, checkpoint markers, capabilities, and last sequence
  through `GET /api/tasks/{task_id}/projection`.
- `TaskRuntimeOrchestrator` now owns start, cancellation, suspension, and
  checkpoint resume. Resume creates a new run while retaining the resolved
  runner generation and passes the checkpoint through the runtime boundary.
- System and plugin runners receive the same immutable `RuntimeContext`.
  The example plugin is executed through the manifest, generation registry,
  and coordinator in a focused regression test; the two-argument runner form
  remains a compatibility adapter.
- The Task Workbench consumes the replayable authenticated SSE endpoint as
  its primary update path, merges committed events by ID, and refreshes the
  bounded projection after a short debounce. Ten-second polling remains only
  as a degraded-mode safety net. A browser check confirmed that an existing
  task still exposes its complete user and assistant conversation.
- `RuntimeContext.artifact_emitter` is now the shared system/plugin artifact
  boundary. It writes content-addressed bytes and returns a canonical signal
  that binds the `ArtifactRef` and optional `EvidenceRef`; both the Console
  runner and public SDK example exercise it.
- `ArtifactRenderer` is now the shared system/plugin projection boundary. A
  preview pins one registry generation, routes by stable priority, rejects
  unsafe inline media or inconsistent source hashes, and falls back after a
  plugin failure. Projection responses publish availability, renderer ID, and
  generation; downloads preserve the verified original bytes.
- `RuntimeContext.approval_broker` and `RuntimeContext.cancellation` are typed,
  non-serializable, run-scoped services. The coordinator injects both, the
  broker rejects stale run access, and cancellation gives the capability one
  event-loop boundary for cooperative cleanup before force-cancel. The Console
  compatibility adapter also drains its legacy approval waiter records.
- `RuntimeStrategy` is now a public SDK port. Start and resume fail closed on
  a missing, wrong-slot, protocol-mismatched, or ID-mismatched strategy; a
  strategy exception becomes a durable run failure. Its bounded parameters
  must be JSON and at most 32 KiB before they are injected into the same
  immutable context used by system and plugin runners. The Task projection
  reports the selected strategy generation.
- `GET /projection` now reads task, runs, plan, approvals, events, checkpoint,
  and sequence from one SQLite read transaction. The response contract is
  unchanged, but the Workbench can no longer combine projections from
  different commit versions.
- The clean-room plugin walkthrough now defines capability-ID matching,
  public-SDK-only imports, validation, hot install/update/uninstall, Task
  execution, Artifact/Evidence checks, rollback, and generation pinning. The
  reference plugin validates with seven contributions and its focused lifecycle
  matrix passes.
- Coding, Goal, and Mission are now system Task Strategy contributions. Their
  bounded directives enter the Console compatibility Runner without changing
  Workspace configuration, and resume preserves the previous Run's strategy
  unless an explicit replacement is supplied. Remaining architecture work for
  the broader goal includes moving Cron/Heartbeat onto the shared Scheduler
  port.

No previous aggregate test count can mark these rows complete. Each row needs
evidence that exercises its named runtime boundary.

## 13. Explicit non-goals for Lite

- Multi-tenant RBAC and tenant data isolation.
- Distributed schedulers and remote worker recovery.
- Workstation runner pools and Hub object storage.
- Hot replacement of Kernel ABI, native libraries, or database migrations.
- Persisting model chain-of-thought.

These exclusions affect deployment scale, not the shared task-domain or
contribution contracts.
