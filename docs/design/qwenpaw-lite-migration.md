# QwenPaw Lite Migration Plan

Status: Approved — implementation authorized on 2026-09-21

## Migration principles

1. Freeze domain and API contracts before moving implementation code.
2. Introduce adapters before replacing existing call sites.
3. Keep chat, channels, tools, and plugin manifests compatible throughout.
4. Make the durable ledger authoritative before exposing the task workbench.
5. Activate plugin contributions atomically before letting tasks pin a
   generation.
6. Remove the proactive execution bypass before enabling proactive proposals
   in Lite.

## Sequence

### M1: Contracts and tests

- Add kernel models, state machines, ports, and event schemas.
- Add contract tests for serialization, illegal transitions, event ordering,
  redaction, and public API compatibility.
- No existing runtime is redirected in this step.

Rollback: delete the isolated kernel package and its tests.

### M2: Ledger and TaskService

- Add SQLite WAL storage and schema migrations.
- Add append, replay, idempotency, checkpoint, and restart tests.
- Add TaskService behind internal dependency injection.
- Keep `TaskTracker` as the live delivery adapter.

Rollback: disable TaskService bootstrap and retain existing console behavior;
the append-only database can remain unused.

### M3: Console task compatibility

- Route `/console/chat/task` through TaskService while preserving its response
  shape.
- Expose the new `/api/tasks` API.
- Compare old and new completion/cancellation outcomes in targeted tests.

Rollback: restore the compatibility facade to its old in-memory path without
changing the public endpoint.

### M4: Durable approvals and proactive safety

- Persist approval requests and decisions as execution events.
- Keep the existing ApprovalService for delivery and active waiters.
- Convert proactive output to Proposal records.
- Remove or isolate `PermissionMode.BYPASS` execution from Lite.

Rollback: disable proactive proposal creation. Do not restore automatic
bypass execution in the Lite profile.

### M5: Extension runtime generations

- Parse v2 multi-contribution manifests while adapting legacy `type`.
- Stage and health-check contributions before publication.
- Atomically publish immutable registry generations.
- Pin each new run to one generation and retain leased generations.
- Validate one plugin contributing at least two slots.
- Add a stable SDK facade, manifest schema, local validator, two-slot example,
  and a ten-minute developer Quickstart.

Rollback: keep the last healthy generation active and remove the failed
staging directory.

### M6: Lite profile

- Add edition resolution and the `lite` profile.
- Wire local storage, local runner, strict approval, and supported slots.
- Assert that Hub and remote-runner services are not loaded.

Rollback: remove the edition override and start the legacy app path.

### M7: Task workbench

- Add Tasks menu and route.
- Add list, details, timeline, approvals, artifacts, and resume/cancel actions.
- Add task and artifact host slots.
- Validate desktop, tablet, and mobile layouts with targeted component tests.

Rollback: remove the route and menu entry; backend task execution remains
available through its API.

## Compatibility gates

- Existing chat creation and streaming continue to work.
- At least one existing tool plugin loads and executes through the legacy
  adapter.
- Legacy plugin manifests remain valid.
- Existing frontend plugins keep their Menu/Route/Slot registrations.
- Existing checkpoint commands and APIs continue to operate independently.
- Existing configuration files require no destructive migration.

## Data migration

The Lite execution ledger starts empty. Existing chat history and checkpoints
are referenced when a new Lite task originates from an existing chat, but old
chat turns are not rewritten as synthetic execution events. This avoids
inventing historical state that did not previously exist.

SQLite migrations are forward-only within the feature branch. Before a schema
migration, the service records the current schema version and fails closed if
the installed binary cannot understand it. No automatic destructive downgrade
is performed.
