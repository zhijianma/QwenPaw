# QwenPaw Task Runtime Contract

Status: Proposed contract for R0

Date: 2026-09-22

Related design: `qwenpaw-unified-task-runtime.md`

## 1. Compatibility policy

The public Task API uses additive evolution within the current Lite migration.
Existing v1 records and endpoints remain readable while the unified runtime is
introduced.

- Kernel records continue to accept `schema: qwenpaw.kernel-model.v1`.
- Execution events continue to use `schema: qwenpaw.execution-event.v1`.
- New optional model fields must have defaults.
- Execution events may add optional `invocation_id`, `step_id`, `source`,
  `cause_event_id`, and `correlation_id`; legacy rows that omit them remain
  valid.
- A new required field needs a new schema identifier and an explicit reader
  migration before any writer emits it.
- Event meanings are immutable. A changed meaning requires a new event type.
- Unknown event types and optional fields must be preserved or ignored safely.
- HTTP mutation retries use `Idempotency-Key` and bind the key to a canonical
  request hash.
- Errors use `application/problem+json` with a stable machine-readable `code`.

### 1.1 Application boundary

The public `/api/tasks` route is a transport adapter. It parses HTTP input,
maps stable errors, serializes public models, and applies response security
headers. It does not read the Ledger, Artifact Store, Capability Resolver, or
`TaskService` directly.

The process-local `TaskApplicationHost` composes one Edition Runtime and owns
the Task use cases for aggregate reads, execution, approvals, artifacts,
events, sensors, capability catalog queries, and side-effect recovery. Artifact
authorization, compatible Store resolution, preview budgets, and generation-
pinned Renderer selection belong to the Artifact application service.
Capability leases are acquired and released inside the capability application
service. Side-effect retry authorization is represented by a transport-neutral
command carrying Task, record, actor, and reason identity.

## 2. Identifiers and ordering

| Value | Rule |
|---|---|
| task, run, plan, message, approval, artifact, evidence IDs | UUID |
| capability and contribution IDs | lower-case namespaced ID |
| event ordering | unique, contiguous `sequence` within one task |
| registry generation | positive integer pinned for the entire run |
| invocation identity | one UUID per Run attempt |
| correlation identity | one root UUID retained across resumed attempts |
| timestamps | timezone-aware UTC ISO 8601 |
| event inline payload | at most 32 KiB encoded UTF-8 JSON |
| artifact preview | at most the server-configured preview limit |

Artifact bytes are immutable and content-addressed. `artifact.renderer` only
produces a derived view: each response identifies the renderer, registry
generation, and source hash. Inline output is restricted to the host safe MIME
set and receives `Content-Security-Policy: default-src 'none'; sandbox` plus
`X-Content-Type-Options: nosniff`. Attachment responses preserve the verified
source bytes and declared media type; a plugin cannot transform downloads.

## 3. Public models

### 3.1 Execution selection

```json
{
  "planner_id": "qwenpaw.system.tasks.basic-planner",
  "runner_id": "qwenpaw.system.tasks.console-agent",
  "strategy_id": "qwenpaw.system.tasks.default-strategy",
  "approval_level": "smart",
  "project_dir": "/absolute/project/path"
}
```

Rules:

- `runner_id` is required when an order is dispatched.
- `strategy_id` is resolved and prepared before the runner starts.
- strategy parameters are bounded public JSON stored in a typed
  `RuntimeStrategyDirective`; only a concrete Runner may bridge an explicitly
  validated subset into another runtime.
- `approval_level` is copied into the immutable runtime context.
- `project_dir` is normalized and authorized before Task creation.
- Capability IDs are resolved only from the run's pinned generation.
- The server never silently substitutes a missing requested capability.
- Resume inherits the previous Run's `runner_id` and `strategy_id` by default.
  An explicit `RuntimeLaunchConfig.strategy_id` may replace the strategy, but
  the replacement is resolved from the new attempt's pinned generation before
  execution.

### 3.2 Execution contract

`ExecutionContract` is the single reliability contract accepted by Task API,
Proposal/Sensor, compatibility Chat Task, `TaskOrder`, and `RuntimeContext`.
It is stored on `Task`; runtime launch metadata is not a second source of
truth.

```json
{
  "goal": "Prepare and verify the release report",
  "acceptance": ["The report artifact passes verification"],
  "autonomy_level": "l2",
  "permission_scope": "release.report",
  "side_effect_policy": {
    "require_idempotency": true,
    "automatic_retry": false,
    "uncertain_outcome": "block"
  },
  "budget": {
    "max_duration_seconds": 600,
    "max_tokens": 20000,
    "max_cost_micros": 500000,
    "max_tool_calls": 50,
    "max_retries": 2,
    "max_concurrency": 1
  },
  "retry_policy": {
    "backoff_seconds": 2,
    "retryable_error_codes": ["provider.timeout"],
    "retry_side_effects": false
  },
  "timeout_policy": {
    "attempt_seconds": 300,
    "idle_seconds": 60,
    "approval_seconds": 600
  },
  "escalation_policy": {
    "on_budget_exhausted": "pause",
    "on_permission_denied": "pause",
    "on_recovery_required": "inbox"
  },
  "exit_conditions": [
    {"condition_id": "verified", "kind": "acceptance_met"}
  ],
  "required_artifacts": [
    {"kind": "report", "media_types": ["text/markdown"], "min_count": 1}
  ],
  "verification_policy": {
    "verifier_ids": ["release.report-verifier"],
    "require_all_acceptance": true,
    "require_evidence": true,
    "fail_on_unverified": true
  }
}
```

Rules:

- Task and TaskOrder goal/acceptance must exactly match the contract.
- L1 and L2 remain backward compatible: the contract is optional while old
  producers migrate.
- L3 requires Acceptance, Permission Scope, duration/token/cost/tool-call
  ceilings, at least one Exit Condition, and Verification Policy.
- automatic side-effect retry requires idempotency and both retry policy
  declarations must agree.
- duplicate Exit Condition IDs and required Artifact kinds are rejected.
- Built-in Exit Conditions are executable contracts, not descriptive labels:
  `acceptance_met` checks passed acceptance verification,
  `artifact_emitted` checks the ready Artifact projection, and
  `explicit_signal` requires a durable `exit_condition.met` event carrying
  the declared `condition_id`. Required conditions block `run.completed` with
  `exit_condition_unmet:<id>`.
- Optional result-bound conditions are evaluated after durable result-changing
  signals. The host stops the Runner only when an optional condition is met
  and the full completion gate already passes. It then writes the host-owned
  `exit_condition.triggered` event, causally linked to the signal that made
  safe completion possible. Optional conditions cannot bypass required
  Artifacts, Verification Policy, Evidence, or required Exit Conditions.
- `max_iterations` requires a positive integer `parameters.limit`. Console
  tasks compile the strictest declared limit into the ReAct runtime; plugin
  and Harness runners emit `runner.iteration` so the host can fail the Run if
  they cross the same limit.
- duration budget is enforced as the strictest of host timeout, contract
  attempt timeout, and `max_duration_seconds`; exhaustion fails the Run with
  `TaskExecutionBudgetExceededError`.
- `UsageDelta`, `UsageSnapshot`, and the run-scoped `UsageMeter` form the
  canonical accounting contract. `usage.recorded` events are the durable fact
  source, and recovery folds usage across all attempts.
- `BudgetLease` is the runtime admission capability layered over that ledger;
  it never replaces durable accounting. Lite root leases are reconstructed
  from the cumulative `UsageSnapshot`, and derived child leases reserve local
  ceilings, release unused reservations, and reject work after cascade revoke.
- Console `turn_usage` and function-call boundaries feed the same meter used
  by plugin runners. Token, reported cost, tool-call and retry ceilings fail
  the Run with `TaskExecutionBudgetExceededError` after actual usage is
  durably recorded.
- A Runner declares `CostAccountingMode.REPORTED`, `ZERO`, or `UNKNOWN`.
  Missing Console price telemetry is persisted as `cost_unknown=true`, never
  coerced into a trusted zero. When `max_cost_micros` is configured, an
  unknown Runner records that evidence and fails before its execution callback
  or provider request starts. Legacy Runners without a declaration are
  treated as unknown.
- `max_concurrency` is held at the Tool Coordinator's real handler boundary.
  Foreground and offloaded tools keep the slot until execution finishes;
  accounting failure prevents the handler from starting. Console timeline
  events mark already-accounted calls to prevent double counting.
- Lite subagent and git-fork HTTP dispatch carries only a host-issued opaque
  usage-scope ID. After checking Agent identity, the receiving adapter derives
  an independent child `BudgetLease`; reference-counted scopes keep its
  accounting path alive while the root request closes. Descendant model and
  tool usage is recorded in the root Ledger, and the root Run rechecks the
  shared snapshot before completion.
- Scope IDs are transport capabilities, not budget values. Public requests
  cannot supply a meter, limits, counters, or Agent identity. Workstation/Hub
  must replace the Lite in-process scope registry with a signed or durable
  distributed lease adapter without changing this opaque transport contract.
- Price snapshot calculation remains separate work. Required result-bound
  conditions, optional safe early-stop, maximum iterations, and completion
  Verification are enforced today.

### 3.3 Causal event identity

Every newly-created Run owns an `invocation_id` and a root `correlation_id`.
A resumed attempt creates a new Invocation while retaining the prior root
correlation. `RuntimeContext` carries both values into a native, plugin, or
Harness Runner. The Console compatibility Runner passes them only through its
internal Task broker bridge; external request payloads cannot select either
identity. Runner, Approval and Side Effect events write Invocation,
correlation, and source into the top-level event envelope, not only into
payload JSON.

`cause_event_id` is a direct factual edge. The Ledger rejects a missing cause,
a cause owned by another Task, and a cause whose sequence is not earlier than
its effect. A writer must leave the field null when it cannot identify the
actual cause; temporal adjacency is not enough evidence.

Artifact, Evidence, and Verification provenance is host-owned. Plugins submit
typed references and optional causal hints through `RunnerSignal`; the host
persists the immutable event envelope and derives `ArtifactRecord`,
`EvidenceRecord`, and `VerificationRecord` from that envelope. Each record
therefore exposes its producing `event_id` plus the effective `step_id`,
`source`, `cause_event_id`, and `correlation_id`. Plugin-controlled Artifact
metadata is descriptive only and must never be treated as audit provenance.
Compatibility projections keep the existing `evidence` and `verifications`
arrays while adding `evidence_registry` and `verification_registry` records.

### 3.4 Conversation message

```json
{
  "schema": "qwenpaw.kernel-model.v1",
  "message_id": "00000000-0000-0000-0000-000000000001",
  "task_id": "00000000-0000-0000-0000-000000000002",
  "run_id": "00000000-0000-0000-0000-000000000003",
  "invocation_id": "00000000-0000-0000-0000-000000000005",
  "correlation_id": "00000000-0000-0000-0000-000000000005",
  "role": "assistant",
  "content": [
    {"type": "text", "text": "Completed the requested translation."}
  ],
  "status": "completed",
  "parent_message_id": null,
  "created_at": "2026-09-22T08:00:00Z",
  "completed_at": "2026-09-22T08:00:04Z"
}
```

Allowed roles are `user`, `assistant`, `system`, and `tool`. Public content
parts are bounded, displayable values. Provider reasoning and internal prompt
content are never message parts.

Compatibility Chat messages may additionally expose top-level
`artifact_refs` and `evidence_refs`. Attachment content parts carry the same
references plus an opaque `artifact_receipt`; clients must treat that receipt
as a capability token and must not derive or modify it.

The server, not the client, establishes ownership:

1. `/console/upload` writes immutable bytes to the common Artifact Store and
   returns the legacy media URL plus `ArtifactRef`, `EvidenceRef`, and receipt.
2. The first `/console/chat` or `/console/chat/task` send atomically binds the
   receipt to the resolved Chat and replaces client objects with canonical
   stored references.
3. Same-Chat retries are idempotent. Partial, forged, unknown, or cross-Chat
   references return HTTP 400 before the run starts.
4. `/chats/{chat_id}/artifacts/{artifact_id}/content` resolves only references
   present in that Chat history and renders through `artifact.renderer`.

### 3.5 Tool activity

```json
{
  "tool_call_id": "call-1",
  "task_id": "00000000-0000-0000-0000-000000000002",
  "run_id": "00000000-0000-0000-0000-000000000003",
  "capability_id": "qwenpaw.system.files",
  "tool_name": "read_text_file",
  "status": "completed",
  "summary": "Read README.md",
  "started_at": "2026-09-22T08:00:01Z",
  "finished_at": "2026-09-22T08:00:02Z",
  "approval_id": null,
  "artifact_refs": []
}
```

Allowed states are `requested`, `waiting_approval`, `running`, `completed`,
`failed`, and `cancelled`. Arguments stored in events or projections are
redacted before persistence.

### 3.6 Approval display

```json
{
  "schema": "qwenpaw.kernel-model.v1",
  "approval_id": "00000000-0000-0000-0000-000000000004",
  "task_id": "00000000-0000-0000-0000-000000000002",
  "run_id": "00000000-0000-0000-0000-000000000003",
  "source": "tool",
  "action": "tool.execute",
  "risk": "high",
  "requester": {"type": "agent", "id": "default"},
  "policy": "tool_guard",
  "continuation": "resume_on_decision",
  "checkpoint_id": "00000000-0000-0000-0000-000000000006",
  "status": "pending",
  "display": {
    "title": "Shell execution requires approval",
    "summary": "The task wants to execute a local command.",
    "target": "README.md",
    "provider": "qwenpaw.system.tasks.console-agent"
  },
  "redacted_arguments": {
    "tool_name": "execute_shell_command",
    "input": {"command": "python <redacted>"}
  },
  "expires_at": "2026-09-22T08:05:00Z",
  "created_at": "2026-09-22T08:00:00Z",
  "decision": null
}
```

`source` is one of `tool`, `driver`, `harness`, `proposal`, or `system`.
`decision`, when present, is the immutable `ApprovalDecision` record.
`invocation_id` and `correlation_id` are Runtime-owned identities. External
request payloads cannot override them. Policy evaluation, sandbox escalation,
durable Approval and Audit reuse the same values; non-runtime proposals may
leave them null.

Every durable approval is committed atomically with a safe checkpoint. The
checkpoint is referenced by `checkpoint_id`; the Approval record does not
embed a second mutable checkpoint copy.

System and plugin Runners receive the same run-scoped `CheckpointBroker`
through `RuntimeContext`. `save()` accepts only a bounded runner cursor, an
optional opaque workspace reference, and an optional idempotency key. The host
owns Task/Run identity, sequence, `safe_to_resume`, persistence, and the
`checkpoint.created` event. Cursor plus workspace reference must fit within
32 KiB. Saving a checkpoint does not suspend the Run; only the orchestrator may
commit `run.suspended` after stopping the owned execution.

### 3.7 Side-effect record

A declared `local_write`, `external_write`, or `process` tool reserves an
independent Task-scoped idempotency key before execution. Its durable status is
`prepared`, `succeeded`, `failed`, or `uncertain`. The record links the Policy
decision, Approval, Invocation, Correlation and execution events. API command
idempotency keys are not reused for this purpose.

An orphaned `prepared` record becomes `uncertain`. Resume is rejected until a
user explicitly authorizes retry, which records the actor, reason and recovery
timestamp and changes the record to `failed`/retryable. This authorization is
an assertion that the external operation was not applied; it is never inferred
from process termination.

### 3.8 Capability descriptor

The compatibility field `provider_plugin_id` remains readable. New writers
use provider-neutral ownership:

```json
{
  "capability_id": "qwenpaw.system.tasks.console-agent",
  "slot": "runner",
  "provider_id": "qwenpaw.system",
  "provider_kind": "system",
  "version": "1.0.0",
  "restart_policy": "hot",
  "input_schema": {},
  "output_schema": {},
  "metadata": {}
}
```

The legacy field maps to `provider_id` during deserialization. Public SDK
callers must not branch dispatch behavior on `provider_kind`.

## 4. Task projection

`GET /api/tasks/{task_id}/projection` is the authoritative Workbench read.

```json
{
  "task": {},
  "active_run": {},
  "runs": [],
  "latest_plan": {},
  "conversation_messages": [],
  "tool_activities": [],
  "pending_approvals": [],
  "recent_decisions": [],
  "artifacts": [],
  "evidence": [],
  "checkpoint": null,
  "capabilities": [
    {
      "capability_id": "qwenpaw.system.tasks.console-agent",
      "slot": "runner",
      "registry_generation": 2
    },
    {
      "capability_id": "qwenpaw.system.tasks.default-strategy",
      "slot": "strategy",
      "registry_generation": 2
    }
  ],
  "last_sequence": 42
}
```

Projection invariants:

- `active_run` is null exactly when `task.active_run_id` is null.
- every returned approval belongs to the requested task;
- `pending_approvals` contains only unresolved requests;
- messages are ordered by creation event and assembled from committed deltas;
- artifacts and evidence are deduplicated by ID;
- `last_sequence` equals the newest event included in the projection;
- a projection is built from one consistent Ledger read transaction;
- plugin implementations and internal runtime handles never appear.

## 5. Commands and queries

### 5.1 Create

`POST /api/tasks`

```json
{
  "objective": "Check README installation and translate it to Italian",
  "constraints": [],
  "acceptance_criteria": ["Produce an Italian Markdown artifact"],
  "source": "user",
  "project_dir": "/absolute/project/path",
  "runner_id": "qwenpaw.system.tasks.console-agent",
  "strategy_id": "qwenpaw.system.tasks.default-strategy",
  "approval_level": "smart"
}
```

Returns `201` and the Task. Creation never starts execution implicitly.

### 5.2 Start

`POST /api/tasks/{task_id}/start`

Returns `202` with the accepted Task and Run. The orchestrator pins the
generation before resolving the planner, strategy, or runner. Repeating the same
idempotency key returns the original response.

### 5.3 Cancel

`POST /api/tasks/{task_id}/cancel`

The orchestrator signals the active runtime, cancels its pending approvals,
waits for bounded cleanup, commits terminal state, and releases the generation
lease. The HTTP router does not cancel an `asyncio.Task` directly.

Pending approval decisions and their `approval.decided` events commit in the
same transaction as the terminal Task/Run projections. `run.cancelled` is the
final event, so a projection cannot expose a cancelled task with an actionable
approval.

For a durable Task, Tool, Governance Tool, Driver, Codex/Qoder Harness and ReMe
approval waiters use
`ExecutionContract.timeout_policy.approval_seconds`. The same value controls
both the process waiter and the durable request's `expires_at`, calculated from
the broker-created `created_at`; projections therefore never advertise a longer
approval window than the executing waiter. The compatibility request context is
trusted only when it also carries the Host-injected Task Approval Broker;
transport callers cannot extend or shorten the waiter by posting a look-alike
contract. Without a trusted Task contract, the process-level Tool Guard timeout
remains the default.

Every durable path attaches the pending request to both the Task Ledger and the
blocking Interaction projection before waiting. Either bridge failing denies
the in-memory request and closes the operation; execution cannot continue with
an invisible approval. PawApp `UIBridge.confirm()` remains a separate legacy UI
protocol and is not covered by this Task approval contract.

### 5.4 Resume

`POST /api/tasks/{task_id}/resume`

Resume requires a safe checkpoint and creates a new Run. The orchestrator
resolves the recorded runner capability from a newly pinned generation. The
checkpoint payload is passed to the runner adapter; it is not treated as proof
that the external runtime actually restored until the adapter confirms it.
The latest host-persisted plugin checkpoint is eligible after a failed Run,
subject to retry budget and unresolved Side Effect recovery gates.

The idempotency boundary includes both the durable Run commit and execution
startup. Concurrent retries with the same key, and retries after the Run has
completed, return the authoritative stored Run without starting its runner a
second time. Replay lookup occurs before capability resolution, so uninstalling
the original runner cannot invalidate an already committed response. A
genuinely new recovery attempt requires a new idempotency key and still fails
closed when the selected runner is unavailable.

### 5.5 Approval list

`GET /api/tasks/{task_id}/approvals?status=pending&run_id={run_id}`

Returns an ordered page of approval records and decisions. The query is backed
by approval projections, not an event scan or global in-memory waiter list.

### 5.6 Approval decision

`POST /api/tasks/{task_id}/approvals/{approval_id}/decision`

If the durable request has lost its process-local waiter, the decision is still
committed before continuation. While sibling approvals remain pending, the
Task stays at the same durable wait boundary. After the last blocker resolves,
the application fences the exact orphaned Run and resumes through the approval
checkpoint as a new Run with a stable continuation idempotency key. A repeated
decision cannot fail the newly attached Run because orphan recovery is guarded
by the approval request's original `run_id`.

If runner resolution or startup fails, the endpoint returns
`task_continuation_failed`; the decision remains committed and the Task remains
`failed` with its safe checkpoint available for explicit retry. A denial or
other terminal decision never creates a continuation Run.

The Chat Interaction response adapter follows the same state machine. A
Task-bound Interaction carries `task_id`, approval `source_id`, and the safe
checkpoint pointer. It reconciles the Task decision before closing the
Interaction projection. The Interaction owns delivery to the live waiter, so
the application does not redeliver the primary decision and create an
idempotency loop; cancelled sibling approvals are still drained through the
runtime bridge. After restart, the same adapter commits the decision and uses
checkpoint continuation even when the terminal hook no longer exists.

### 5.7 Side-effect recovery

- `GET /api/tasks/{task_id}/side-effects`
- `POST /api/tasks/{task_id}/side-effects/{record_id}/retry`

The retry endpoint requires a non-empty human reason. Until every prepared or
uncertain record is resolved, `/resume` returns
`side_effect_recovery_required`.

```json
{
  "decision": "approved",
  "reason": "The command is limited to this workspace.",
  "scope": "exact"
}
```

Only `approved`, `denied`, and `cancelled` are accepted from a user. `expired`
is a system decision. The server verifies task ownership and actor policy,
commits first, and releases the waiter second.

## 6. Event catalog

All events use the existing `ExecutionEvent` envelope.

| Event type | Required payload | Projection effect |
|---|---|---|
| `task.created` | source | create Task |
| `task.planned` | plan ID, revision | update latest plan |
| `run.started` | attempt, runner ID | set active run |
| `run.suspended` | checkpoint ID | suspend run/task |
| `run.resumed` | approval/checkpoint ID as applicable | audit boundary only |
| `run.completed` | optional summary | terminal success |
| `run.failed` | bounded error code/summary | terminal failure |
| `run.cancelled` | reason code | terminal cancellation |
| `exit_condition.met` | condition ID | record runner-declared fact |
| `exit_condition.triggered` | condition IDs | audit host early-stop decision |
| `conversation.message.started` | message ID, role, parent | create streaming message |
| `conversation.message.delta` | message ID, bounded content delta | append public content |
| `conversation.message.completed` | message ID | complete message |
| `conversation.message.failed` | message ID, error code | fail message |
| `tool.requested` | call ID, capability ID, tool name | create tool activity |
| `tool.waiting_approval` | call ID, approval ID | link approval |
| `tool.started` | call ID | mark running |
| `tool.completed` | call ID, summary | complete activity |
| `tool.failed` | call ID, error code | fail activity |
| `tool.cancelled` | call ID, reason code | cancel activity |
| `approval.requested` | approval ID, source, action, risk | add pending approval |
| `approval.decided` | approval ID, decision, scope | resolve approval |
| `artifact.created` | artifact ID, kind | attach ArtifactRef |
| `evidence.recorded` | evidence ID, artifact ID | attach EvidenceRef |
| `checkpoint.created` | checkpoint ID, safe flag | update checkpoint |
| `capability.resolved` | capability ID, slot, generation | audit selection |

Compatibility readers continue to accept current `conversation.user`,
`conversation.assistant.delta`, `conversation.assistant.completed`, and
`runner.*` signals. The orchestrator adapter emits the canonical message
events and may additionally emit compatibility events during migration; a
projection must not double-count them.

Plugin-defined events use `plugin.<provider_id>.<name>` and cannot mutate core
projections unless a registered projector explicitly validates them.

## 7. Multiple-approval transaction rules

Let `P(run_id)` be the number of unresolved blocking approvals for the active
run.

### Request

```text
before: run=running, P=0
after : run=waiting_approval, P=1, emit approval.requested

before: run=waiting_approval, P=N where N >= 1
after : run=waiting_approval, P=N+1, emit approval.requested
```

The second case is valid and does not emit another run-state transition.

### Approve or resume-on-decision

```text
before: P=N where N > 1
after : P=N-1, remain waiting_approval, emit approval.decided

before: P=1
after : P=0, transition to running,
        emit approval.decided then run.resumed
```

### Reject

- `resume_on_decision`: use the same pending-count rule as approval.
- `fail_on_rejection`: atomically resolve the request, cancel remaining
  requests for the run, and transition the run/task to failed.
- cancellation: atomically resolve/cancel remaining requests and transition
  the run/task to cancelled.

Concurrent decisions use optimistic Task versioning plus an idempotency key.
A conflict retries the whole aggregate decision against the latest pending
count; it never releases a waiter before the retry commits.

## 8. SSE contract

`GET /api/tasks/{task_id}/stream?after_sequence=N`

- Events are sent only after their transaction commits.
- Each SSE record uses the event sequence as `id`.
- The client reconnects with the last committed sequence.
- Browsers may instead send the standard `Last-Event-ID` header. If both
  cursors are supplied they must match, otherwise the API fails closed.
- A heartbeat contains no domain event and cannot advance the sequence.
- Gaps trigger a projection refresh followed by replay from `last_sequence`.
- A terminal event closes the stream after it has been delivered.
- The existing polling path remains a temporary fallback.

## 9. Error codes

| Code | HTTP | Meaning |
|---|---:|---|
| `task_not_found` | 404 | task does not exist |
| `approval_not_found` | 404 | request is not owned by the task |
| `approval_already_resolved` | 409 | immutable decision already exists |
| `invalid_task_transition` | 409 | command is illegal for current state |
| `task_version_conflict` | 409 | optimistic concurrency conflict |
| `idempotency_conflict` | 409 | key was reused for different input |
| `capability_unavailable` | 409 | capability absent from pinned generation |
| `runtime_unavailable` | 503 | selected runtime cannot start safely |
| `approval_delivery_failed` | 503 | request persisted but no allowed delivery path |
| `task_runtime_lost` | 409 | durable approval lost its executing process |
| `task_runtime_attached` | 409 | this process still owns the active execution |
| `checkpoint_not_resumable` | 409 | no safe checkpoint can create a new attempt |
| `execution_budget_exhausted` | 409 | retry or execution budget is exhausted |
| `side_effect_recovery_required` | 409 | uncertain external outcome blocks resume |
| `side_effect_not_found` | 404 | side effect is not owned by the Task |
| `side_effect_not_recoverable` | 409 | retry authorization targets a settled record |
| `sensor_unavailable` | 409 | requested Sensor is absent or incompatible |
| `sensor_proposal_limit` | 422 | one poll exceeded the bounded proposal batch |
| `runner_unavailable` | 409 | requested Runner is absent or incompatible |
| `planner_unavailable` | 409 | requested Planner is absent or incompatible |
| `strategy_unavailable` | 409 | requested Strategy is absent or incompatible |
| `proposal_not_available` | 409 | approval lacks valid Proposal metadata |
| `artifact_not_found` | 404 | artifact is not owned by the Task |
| `artifact_content_not_found` | 404 | referenced artifact bytes are unavailable |
| `artifact_integrity_error` | 409 | stored bytes fail digest verification |
| `artifact_preview_too_large` | 413 | content exceeds inline preview limit |
| `artifact_preview_unsupported` | 415 | no safe renderer accepts the artifact |
| `artifact_renderer_failed` | 503 | all matching renderers failed validation or execution |
| `invalid_project_dir` | 400 | project path is invalid or unauthorized |
| `invalid_last_event_id` | 400 | Last-Event-ID is not an integer |
| `invalid_event_cursor` | 400 | query and header event cursors conflict |

Errors expose bounded summaries. Tracebacks, credentials, raw tool arguments,
and host paths outside the authorized project are not returned.

### 9.1 Pagination and idempotency compatibility

| Surface | Input | Continuation output | Stability rule |
|---|---|---|---|
| Task list | `cursor`, `limit` 1–200 | `next_cursor` | opaque cursor; no duplicate Task across continuation |
| Event page | `after_sequence`, `limit` 1–1000 | `next_sequence` | exclusive sequence cursor; ascending committed events |
| Event SSE | query `after_sequence` or `Last-Event-ID` | SSE `id` | both inputs must match when supplied together |
| Mutations | `Idempotency-Key` header | original response | same key/input replays; changed input returns `idempotency_conflict` |

Existing `/api/tasks` paths remain the only public Task HTTP surface during
the internal Route/Application migration. New response fields are additive;
existing fields and meanings cannot be removed without an explicit contract
version and compatibility adapter.

## 10. Contract-test inventory

| Contract | System implementation | Plugin implementation | Compatibility adapter |
|---|---|---|---|
| planner | basic planner | example planner | hard-coded plan adapter |
| runner | Console Agent | example runner | legacy Console task route |
| approval broker | Tool/Driver/Harness/ReMe adapters | example guarded tool | ApprovalService waiter |
| artifact emitter | final response emitter | example artifact tool | filesystem artifact reader |
| artifact renderer | safe text/download renderer | Task Insights renderer | direct raw endpoint removed |
| sensor | proactive responder | example sensor | current proposal ingress |
| runtime context | native Agent adapter | example runner | legacy request-context map |

Each row must run the same behavioral suite. Separate tests that merely assert
class presence or protocol conformance are insufficient.

The Artifact Renderer row is enforced by
`tests/contract/os/test_artifact_renderer_contract.py`. The suite drives the
system safe renderer and the public-SDK `task-insights` renderer through the
same `ArtifactRenderService`; it verifies generation pinning, preview
selection, content lineage, bounded safe output, and the system attachment
fallback. Protocol checks alone do not satisfy this row.

## 11. Deprecation sequence

1. Add new models, projection queries, and adapters without redirecting calls.
2. Move Task execution to the orchestrator and system catalog.
3. Route task-owned approvals through the broker.
4. Switch the Task Workbench to projection and SSE.
5. Route Driver and Harness task approvals through the broker.
6. Warn when a task-owned request reaches global `/approval/*` directly.
7. Remove raw-event approval inference and router-owned execution handles.
8. Retain non-task Chat compatibility until its separate migration completes.

No step removes a legacy path before its parity test and replacement runtime
trace pass.
