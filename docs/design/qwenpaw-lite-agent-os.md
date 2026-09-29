# QwenPaw Lite Agent OS Architecture

Status: Approved — implementation authorized on 2026-09-21
Target branch: `feat/lite-agent-os`
Last updated: 2026-09-21

This document is the detailed Lite slice specification. The cross-product
capability map, module ownership, and Lite / Workstation / Hub composition are
defined in `qwenpaw-agent-os-fused-architecture.md`.

## 1. Objective

Deliver the first runnable Lite vertical slice of QwenPaw using one shared
kernel, Agent OS capability domains, horizontal extension slots, and an
edition profile. Lite is single-user and local-first. It must not fork the
kernel or introduce Hub-specific multi-tenant and remote-runner behavior.

The first slice must make task execution durable and inspectable:

1. Create a local task.
2. Produce a plan and a run.
3. Persist state transitions in an append-only SQLite ledger.
4. Pause for approval when required.
5. Continue, fail, cancel, or resume from a safe checkpoint.
6. Display the timeline, approvals, artifacts, and evidence in the Console.
7. Pin every run to an immutable plugin registry generation.

## 2. Architectural decisions

### 2.1 Capability domains, not call-stack layers

P1-P6 are bounded capability domains rather than directories that must call
one another in a fixed order. They communicate through kernel contracts and
events:

- P1 Capability and Execution
- P2 Scheduling and Contracts
- P3 Agent Runtime
- P4 Memory and Semantics
- P5 Cognition
- P6 Evolution and Governance

Only the domain contracts, state machines, event envelope, policy decisions,
and extension lifecycle belong to the shared kernel. Engines, runners,
sensors, stores, renderers, and strategies are implementations behind ports.

### 2.2 Existing systems become adapters

The implementation may refactor unsuitable code, but it must not make
temporary behavior part of the new public contract.

| Existing system | Lite responsibility |
| --- | --- |
| `TaskTracker` | Live SSE fan-out and reconnect only |
| Console `_bg_tasks` | Compatibility facade over `TaskService` |
| `ApprovalService` | Notification and in-process waiting adapter |
| `checkpoints` | Workspace snapshot implementation referenced by a task checkpoint |
| mutable `PluginRegistry` | Legacy registration adapter |
| frontend Menu/Route/Slot registry | Reused as the UI extension runtime |
| PawTask SSE | Adapted to the system task event stream |

The execution ledger, rather than any of these adapters, is the execution
truth source.

### 2.3 Security invariants

- A proactive component can emit a `Proposal`; it cannot execute a tool.
- An approved proposal becomes a `TaskOrder` and enters the normal runtime.
- Tool execution continues through Tool Guard and Sandbox.
- The current proactive `PermissionMode.BYPASS` path must be removed or
  isolated from Lite.
- The ledger never stores hidden model reasoning or chain-of-thought.
- Sensitive arguments are redacted before persistence.
- Large payloads are persisted as artifacts and referenced by ID and hash.

## 3. Domain model

Public models use Pydantic, `extra="forbid"`, snake_case JSON fields, and UTC
timestamps. Kernel modules cannot import FastAPI, AgentScope, React, a
specific database, or a concrete plugin.

### 3.1 Task

```text
Task
├── task_id: UUID
├── objective: str
├── status: TaskStatus
├── source: TaskSource
├── agent_id: str
├── constraints: tuple[str, ...]
├── acceptance_criteria: tuple[str, ...]
├── active_run_id: UUID | null
├── version: int
├── created_at: datetime
├── updated_at: datetime
└── metadata: JsonObject
```

`TaskStatus` values:

```text
created
planned
running
waiting_approval
suspended
completed
failed
cancelled
```

Allowed transitions:

```text
created          -> planned | cancelled
planned          -> running | waiting_approval | cancelled
running          -> waiting_approval | suspended
                 | completed | failed | cancelled
waiting_approval -> running | failed | cancelled
suspended        -> running | failed | cancelled
failed           -> planned  # explicit resume only
completed        -> terminal
cancelled        -> terminal
```

Illegal transitions raise `InvalidTaskTransition` and are never appended to
the ledger.

### 3.2 Plan and Run

```text
Plan
├── plan_id: UUID
├── task_id: UUID
├── revision: int
├── steps: tuple[PlanStep, ...]
└── acceptance_criteria: tuple[str, ...]

Run
├── run_id: UUID
├── task_id: UUID
├── plan_id: UUID | null
├── attempt: int
├── status: RunStatus
├── registry_generation: int
├── runner_id: str
├── checkpoint_id: UUID | null
├── started_at: datetime | null
└── finished_at: datetime | null
```

A task represents the durable user goal. A run represents one execution
attempt. Resume creates a new run linked to a safe checkpoint; it never
rewrites the failed or suspended run.

### 3.3 Artifact and evidence

```text
ArtifactRef
├── artifact_id: UUID
├── kind: str
├── uri: str
├── media_type: str
├── content_hash: str
├── size_bytes: int
└── metadata: JsonObject

EvidenceRef
├── evidence_id: UUID
├── artifact_id: UUID
├── claim: str
├── producer: str
└── captured_at: datetime
```

### 3.4 Approval and proposal

```text
Proposal
├── proposal_id: UUID
├── source: str
├── objective: str
├── rationale_summary: str
├── risk: RiskLevel
└── requested_capabilities: tuple[str, ...]

ApprovalRequest
├── approval_id: UUID
├── task_id: UUID
├── run_id: UUID | null
├── action: str
├── risk: RiskLevel
├── requester: ActorRef
├── policy: str
├── status: ApprovalStatus
├── expires_at: datetime | null
└── redacted_arguments: JsonObject

ApprovalDecision
├── approval_id: UUID
├── decision: approved | denied | expired | cancelled
├── actor: ActorRef
├── scope: exact | similar
├── reason: str
└── decided_at: datetime
```

The only legal path from proactive cognition to execution is:

```text
Proposal -> ApprovalRequest -> ApprovalDecision -> TaskOrder
```

### 3.5 Capability and plugin contribution

```text
CapabilityDescriptor
├── capability_id: str
├── slot: str
├── provider_plugin_id: str
├── version: str
├── input_schema: JsonObject
├── output_schema: JsonObject
└── restart_policy: hot | scoped | full

PluginContribution
├── contribution_id: str
├── slot: str
├── entrypoint: str
├── config_schema: JsonObject | null
└── metadata: JsonObject
```

Initial slot names:

- `engine`
- `tool`
- `runner`
- `memory`
- `sensor`
- `artifact.renderer`
- `ui.task.toolbar`
- `ui.task.tab`
- `ui.task.inspector`
- `ui.artifact.preview`
- `ui.settings`

The manifest `type` field remains a legacy compatibility hint and is not used
as the v2 capability-discovery source.

## 4. Execution event contract

```json
{
  "schema": "qwenpaw.execution-event.v1",
  "event_id": "uuid",
  "task_id": "uuid",
  "run_id": "uuid-or-null",
  "sequence": 17,
  "event_type": "tool.completed",
  "occurred_at": "2026-09-21T08:00:00Z",
  "registry_generation": 4,
  "actor": {"type": "runner", "id": "local"},
  "payload": {},
  "artifact_refs": []
}
```

Core event types:

- `task.created`
- `task.planned`
- `run.started`
- `run.suspended`
- `run.resumed`
- `approval.requested`
- `approval.decided`
- `tool.requested`
- `tool.completed`
- `artifact.produced`
- `checkpoint.created`
- `run.completed`
- `run.failed`
- `run.cancelled`

Event invariants:

- `event_id` is globally unique; retrying the same ID is an idempotent
  success.
- `(task_id, sequence)` is unique and monotonically increasing.
- Events are immutable after commit.
- Plugin event types use `plugin.<plugin_id>.*`.
- The SSE event ID is the sequence number and supports `Last-Event-ID`.
- Raw prompts, hidden reasoning, credentials, and unbounded payloads are not
  accepted by the ledger writer.

## 5. Kernel ports

```python
class TaskStore(Protocol): ...
class ExecutionLedger(Protocol): ...
class TaskRunner(Protocol): ...
class ApprovalPort(Protocol): ...
class ArtifactStore(Protocol): ...
class CapabilityResolver(Protocol): ...
class CheckpointStore(Protocol): ...
class EventPublisher(Protocol): ...
```

Lite implementations:

- `SQLiteExecutionLedger`
- `LocalAgentRunner`
- `ExistingApprovalServiceAdapter`
- `FilesystemArtifactStore`
- `GitWorkspaceCheckpointAdapter`
- `GenerationCapabilityResolver`

Workstation and Hub may replace these ports without changing kernel models.

## 6. HTTP API

Initial stable routes:

```text
POST   /api/tasks
GET    /api/tasks
GET    /api/tasks/{task_id}
POST   /api/tasks/{task_id}/cancel
POST   /api/tasks/{task_id}/resume
GET    /api/tasks/{task_id}/events
GET    /api/tasks/{task_id}/stream
POST   /api/tasks/{task_id}/approvals/{approval_id}/decision
GET    /api/tasks/{task_id}/artifacts
```

API rules:

- Create, resume, cancel, and approval decisions accept `Idempotency-Key`.
- Task mutations use the domain `version` for optimistic concurrency.
- Errors use `application/problem+json` and stable domain error codes.
- Lists use cursor pagination.
- Internal exception strings are never returned to clients.
- `/console/chat/task` remains available as a compatibility adapter over the
  same `TaskService`.

Initial error codes:

- `invalid_task_transition`
- `approval_required`
- `task_version_conflict`
- `checkpoint_not_resumable`
- `registry_generation_unavailable`
- `idempotency_conflict`

## 7. SQLite ledger

The Lite database uses SQLite WAL mode and contains, at minimum:

- `tasks`
- `plans`
- `runs`
- `execution_events`
- `execution_checkpoints`
- `approval_records`
- `idempotency_keys`

Each schema has an explicit migration version. Domain updates and event append
occur in the same transaction. Checkpoints carry a `safe_to_resume` flag and
opaque runner cursor. A local AgentScope run is resumable only at a declared
safe boundary; the implementation must not pretend to resume arbitrary model
reasoning in the middle of a turn.

## 8. Extension activation

Plugin manifest v2 adds multiple contributions:

```yaml
schema_version: qwenpaw.plugin.v2
id: example
version: 1.0.0
restart_policy: hot
contributions:
  - id: local-runner
    slot: runner
    entrypoint: example.runner:create_runner
  - id: task-inspector
    slot: ui.task.inspector
    entrypoint: frontend
```

Activation transaction:

1. Parse and validate the manifest.
2. Install into an isolated staging location.
3. Load entrypoints without publishing them.
4. Validate each public Slot Port and namespaced capability identity.
5. Validate UI entrypoint descriptors.
6. Build an immutable shadow registry snapshot.
7. Run contribution health checks.
8. Atomically publish a new registry generation.
9. Pin new tasks to the new generation.
10. Keep old generations while a run owns a lease.
11. Reclaim an old generation only after its leases reach zero.
12. Roll back staging and preserve the current generation on any failure.

Restart policies:

| Policy | Use |
| --- | --- |
| `hot` | tools, runners, sensors, renderers, UI contributions |
| `scoped` | channel manager, agent assembly, active memory backend |
| `full` | kernel ABI, core migration, host middleware, native conflict |

The existing frontend registry already supports runtime Menu, Route, and Slot
registration, disposal, and error boundaries. The Lite work adds task host
slots rather than replacing that registry.

## 9. Lite edition profile

```text
edition                  lite
tenant_mode              single_user
runner                   local
ledger                   sqlite_wal
artifact_store           filesystem
approval_mode            strict
remote_runner            disabled
distributed_scheduler    disabled
skill_evolution          disabled
plugin_default_restart   hot
```

Entrypoints:

```bash
qwenpaw app --edition lite
QWENPAW_EDITION=lite qwenpaw app
```

The edition catalog declares immutable `workstation` and `hub` composition
contracts over the same Kernel Slot set. The Lite resolver fails closed for
both because this slice must not start their multi-user or distributed
runtime adapters.

## 10. Task workbench

The Console adds a responsive `/tasks` surface with:

- task list and status filters;
- task details and current run;
- event timeline;
- pending and resolved approvals;
- artifacts and evidence;
- cancel and resume controls;
- explicit failed, cancelled, suspended, and recovered states.

Host extension points:

- `ui.task.toolbar`
- `ui.task.tab`
- `ui.task.inspector`
- `ui.artifact.preview`

The UI reuses Lucide React and the established Menu/Route/Slot registry.

## 10.1 Secondary developer experience

The extension architecture is successful only if a developer can add a useful
capability without learning QwenPaw internals. The Lite developer contract is:

1. One `plugin.json` and one entrypoint are enough for a minimal plugin.
2. A plugin imports only the stable `qwenpaw.plugins.sdk` surface, never
   `Workspace`, application singletons, router assembly, or loader internals.
3. One plugin may contribute several typed slots without custom bootstrap
   code.
4. Manifest validation reports the exact field, slot, compatibility range,
   restart policy, and recovery action.
5. A local validation command and a two-slot example plugin work without
   starting the complete application.
6. Hot activation, health-check failure, rollback, and generation changes are
   visible through structured diagnostics.
7. A ten-minute Quickstart documents create, validate, install, update, and
   uninstall flows.

The stable SDK and manifest schema are versioned independently from loader
internals. Contract tests must reject imports from private application modules
inside the example plugin.

## 11. Planned file changes

New backend modules:

```text
src/qwenpaw/kernel/__init__.py
src/qwenpaw/kernel/models.py
src/qwenpaw/kernel/events.py
src/qwenpaw/kernel/state_machine.py
src/qwenpaw/kernel/ports.py
src/qwenpaw/kernel/slots.py
src/qwenpaw/tasks/__init__.py
src/qwenpaw/tasks/ledger.py
src/qwenpaw/tasks/redaction.py
src/qwenpaw/tasks/service.py
src/qwenpaw/tasks/runner.py
src/qwenpaw/tasks/artifacts.py
src/qwenpaw/editions/__init__.py
src/qwenpaw/editions/models.py
src/qwenpaw/editions/lite.py
src/qwenpaw/editions/workstation.py
src/qwenpaw/editions/hub.py
src/qwenpaw/editions/catalog.py
src/qwenpaw/plugins/contributions.py
src/qwenpaw/plugins/generations.py
src/qwenpaw/app/routers/tasks.py
```

Modified backend modules:

```text
src/qwenpaw/cli/app_cmd.py
src/qwenpaw/app/_app.py
src/qwenpaw/app/routers/__init__.py
src/qwenpaw/app/routers/console.py
src/qwenpaw/app/task_tracker.py
src/qwenpaw/app/approvals/service.py
src/qwenpaw/plugins/architecture.py
src/qwenpaw/plugins/loader.py
src/qwenpaw/plugins/registry.py
src/qwenpaw/app/routers/plugins.py
src/qwenpaw/agents/memory/proactive/proactive_trigger.py
src/qwenpaw/agents/memory/proactive/proactive_responder.py
```

New frontend modules:

```text
console/src/api/modules/tasks.ts
console/src/pages/Tasks/index.tsx
console/src/pages/Tasks/index.module.less
console/src/pages/Tasks/hooks/useTaskData.ts
console/src/pages/Tasks/TasksPage.test.tsx
```

Modified frontend modules:

```text
console/src/layouts/registry/builtinMenu.ts
console/src/layouts/registry/builtinRoutes.tsx
console/src/plugins/registry/slotKeys.ts
```

The pre-existing modifications in the worktree and `docs/strategy/` are out
of scope and must remain untouched.

## 12. Delivery checklist

- [x] Create `feat/lite-agent-os` branch.
- [x] Inventory task, approval, checkpoint, plugin, proactive, and UI systems.
- [x] Define Lite scope and refactoring boundaries.
- [x] Propose domain, event, port, API, and contribution contracts.
- [x] Receive explicit user approval for this design.
- [x] Implement contract tests before runtime implementations.
- [x] Implement kernel models and state machines.
- [x] Implement SQLite WAL ledger, redaction, and checkpoint replay.
- [x] Implement TaskService and LocalAgentRunner.
- [x] Connect approval, Tool Guard, Sandbox, and proactive proposals.
- [x] Implement contribution registry generations and hot activation.
- [x] Add the stable plugin SDK, manifest validator, example, and Quickstart.
- [x] Implement the Lite edition profile.
- [x] Implement the task workbench.
- [x] Run targeted backend and frontend tests.
- [x] Record acceptance evidence, residual risks, and rollback steps.

### Post-acceptance usability increment

- [x] Add a public start operation backed by the Console/Agent runtime.
- [x] Tie background execution cancellation to durable task cancellation.
- [x] Add the task workbench create-and-start interaction.
- [x] Run focused backend, frontend, and live-browser verification.
- [x] Declare Lite, Workstation, and Hub profiles over one Kernel Slot set
  while keeping unavailable runtimes fail-closed.
- [x] Bound runner execution with the shared Console timeout and persist a
  durable failed terminal state when the stream exceeds that budget.

## 13. Targeted verification

Full `npm run build`, `npm run format`, and `npm run test` are prohibited.
Verification will use only affected tests and file-scoped checks, including:

```text
pytest tests/unit/kernel
pytest tests/unit/tasks
pytest tests/unit/plugins/test_plugin_contributions.py
pytest tests/unit/plugins/test_registry_generations.py
pytest tests/integration/test_lite_task_api.py
pytest tests/integration/test_lite_plugin_hot_activation.py
vitest run console/src/api/modules/tasks.test.ts
vitest run console/src/pages/Tasks/TasksPage.test.tsx
pre-commit run --files <changed files>
```

Commands will be adjusted to the repository's actual targeted test entrypoints
without broadening them into a full suite.
