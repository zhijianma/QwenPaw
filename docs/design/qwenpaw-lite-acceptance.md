# QwenPaw Lite Agent OS Acceptance Record

> **Superseded on 2026-09-22.** A live Task Workbench check showed that the
> approval path was not functionally aligned: the page derived approvals only
> from durable events, several runtime approval producers remained outside the
> Task Ledger, and built-in execution did not use the same capability path as
> contributed runners. The historical evidence below is retained for
> traceability, but its Pass results must not be used as acceptance of the
> unified runtime. The replacement requirements and current gap matrix are in
> `qwenpaw-unified-task-runtime.md`.

Date: 2026-09-21

Branch: `feat/lite-agent-os`

This record evaluates the first Lite vertical slice against the approved
architecture in `qwenpaw-lite-agent-os.md`. It records observed evidence, not
planned behavior.

This is not acceptance of the complete Lite / Workstation / Hub fusion goal.
The broader requirement-by-requirement status and remaining gaps are tracked
in `qwenpaw-agent-os-fused-architecture.md`.

## Acceptance Matrix

| ID | Result | Evidence |
|---|---|---|
| A | Pass | `TaskService` tests exercise created, planned, running, waiting approval, resumed, completed, failed, cancelled, suspended, and checkpoint resume transitions. The Task workbench exposes the same states. |
| B | Pass | SQLite runs in WAL mode; events and projections commit atomically; duplicate event and request idempotency tests pass; state and safe checkpoints survive service reconstruction; resume creates exactly one new attempt for a retried request. |
| C | Pass | Public Kernel models serialize `qwenpaw.kernel-model.v1` and load legacy payloads without an explicit version; `ExecutionEvent` uses the specialized `qwenpaw.execution-event.v1` envelope. Events reject hidden reasoning/raw prompts and oversized inline payloads. Redaction tests cover secrets and tool arguments. Artifacts and evidence use external references. |
| D | Pass | Lite proactive cognition has no executable tools. Proposals become sensor Tasks in `waiting_approval`. Only an exact approved decision creates a `TaskOrder`; it is dispatched once through the existing Console/Agent runtime. Runtime Tool Guard approvals now share an ID with the durable Task approval, commit approve/deny/timeout before waking the tool, preserve proposal rejection semantics, and fail closed when Ledger persistence fails. |
| E | Pass | The example plugin contributes a runner, an approval-gated sensor, and four contextual UI Slots through the real `window.QwenPaw.slot.fill` API. Production `PluginLoader` activates all six contributions in one immutable registry generation. Tasks select the runner from the same pinned lease; sensor proposals use the same durable approval ingress as proactive cognition. Missing or mismatched capabilities fail closed. Pinned leases retain the old generation; unload creates a new generation; failed staging preserves the current generation. |
| F | Pass | `LITE_PROFILE` selects single-user, local runner, SQLite WAL, filesystem artifacts, and strict approval. Its deployment adapter composes the real Task service, capability registry, and artifact store. Workstation/Hub use the same factory but fail closed with exact missing-adapter requirements. Kernel dependency tests reject imports from editions, application, plugins, runtime, AgentScope, and FastAPI. |
| G | Pass | The task workbench renders a workspace task rail, create-and-start flow, plan timeline, runtime events, approvals, artifacts, evidence, status summary, cancel, and failed/suspended resume. It provides verified text/Markdown preview, named download, a 256 KiB inline limit, and preserves toolbar, tab, inspector, and artifact preview slots. |
| H | Pass | The final fused backend matrix reports 287 passing tests and the Tasks/API Vitest slice reports 34 passing tests. The compatibility matrix for Chat/Cron/Memory/MCP/Harness/Plugin reports 458 passing tests, and the PluginApi/Tool Governance slice reports 98. Production CLI validation reports six contributions. Prettier, ESLint, and targeted pre-commit checks pass. |
| I | Pass | The architecture, migration guide, plugin Quickstart, example plugin, validation commands, residual risks, and rollback guidance are present locally. No commit, push, PR, or DingTalk publication was performed. |

## Verification Evidence

Final fused matrix after public model versioning and runner/sensor dispatch:

```text
backend focused matrix
287 passed in 22.23s

Tasks/API Vitest matrix
Test Files  3 passed (3)
Tests      34 passed (34)
```

Backend focused matrix:

```text
python -m pytest \
  tests/unit/kernel tests/unit/tasks tests/unit/editions \
  tests/integration/test_lite_task_api.py \
  tests/integration/test_lite_plugin_hot_activation.py \
  tests/unit/plugins/test_plugin_contributions.py \
  tests/unit/plugins/test_registry_generations.py \
  tests/unit/plugins/test_plugin_sdk_example.py \
  tests/unit/plugins/test_loader_helpers.py \
  tests/unit/plugins/test_plugin_api_extensions.py \
  tests/unit/app/routers/test_console_chat_task.py \
  tests/unit/app/routers/test_console_chat_task_timeout.py \
  tests/unit/agents/memory/test_proactive_responder_tasks.py \
  tests/unit/agents/memory/proactive/test_proactive_context_propagation.py \
  tests/unit/runtime/test_tool_guard_async_offload.py \
  tests/unit/security/tool_guard/test_engine.py \
  tests/unit/governance/test_off_mode_sandbox.py -q

271 passed in 23.39s
```

Frontend focused matrix:

```text
vitest run \
  src/pages/Tasks/TasksPage.test.tsx \
  src/api/modules/tasks.test.ts \
  src/api/request.test.ts

Test Files  3 passed (3)
Tests      31 passed (31)
```

After adding artifact consumption, the narrower changed-surface run reports:

```text
pytest tests/integration/test_lite_task_api.py \
  tests/unit/tasks/test_artifacts.py -q
12 passed in 3.74s

vitest run src/api/modules/tasks.test.ts \
  src/pages/Tasks/TasksPage.test.tsx
Test Files  2 passed (2)
Tests       8 passed (8)
```

After bridging Tool Guard decisions into the Task Ledger:

```text
pytest tests/unit/app/approvals \
  tests/unit/app/test_approval_scope.py \
  tests/unit/runtime/test_approval_commands.py \
  tests/unit/runtime/test_approval_command_handler.py \
  tests/unit/runtime/test_tool_guard_async_offload.py \
  tests/unit/security/tool_guard/test_approval.py \
  tests/unit/security/tool_guard/test_models.py \
  tests/unit/security/tool_guard/test_engine.py -q

185 passed in 3.11s
```

```text
prettier --check <changed task workbench files>
All matched files use Prettier code style!

eslint <changed task workbench and registry files>
0 errors, 2 pre-existing Fast Refresh warnings in builtinRoutes.tsx
```

Python checks were run with targeted `pre-commit run --files ...`. The final
set passed AST, encoding, secret detection, whitespace, trailing commas,
mypy, black, flake8, pylint, and applicable manifest checks.

The final shared-Kernel and compatibility slices report:

```text
kernel/tasks/editions/plugin/proactive focused matrix
185 passed in 2.87s

Chat/Cron/Memory/MCP/Harness/Plugin compatibility matrix
458 passed in 20.52s

PluginApi/Tool Governance compatibility matrix
98 passed in 2.96s
```

Runtime smoke checks:

```text
from qwenpaw.app._app import app
app-import-ok 638 routes

qwenpaw plugin validate examples/plugins/task-insights
Plugin validation passed; Contributions: 6
```

The already-running Console at `http://localhost:5176/tasks` completed a real
README installation-section translation task against the QwenPawCodex project.
The run displayed its generated plan and Console Agent progress, rendered the
final Italian response in the dialogue view, persisted a 7,190-byte Markdown
artifact with one evidence reference, and exposed both in the outcomes view.
The page was also visually checked against the approved task-workbench
prototype. The same 7,190-byte artifact was then opened through the new
preview interaction in the running Console; its Italian Markdown content was
read through the verified legacy-location fallback and rendered in the modal.

No prohibited full `npm run build`, `npm run format`, or `npm run test`
command was run.

## Delivered Boundaries

- Stable Kernel models, events, ports, state machines, and proposal gate.
- SQLite WAL execution Ledger with atomic projections, redaction,
  idempotency, checkpoint replay, and durable artifacts.
- Task application service, local runner adapter, API, SSE replay, stable
  problem details, and legacy chat compatibility bridge.
- Multi-contribution plugin manifests, reserved slots, immutable registry
  generations, hot activation, rollback, SDK, example, and Quickstart.
- Lite edition profile without a forked Kernel.
- Lite deployment adapter using shared Kernel ports, with explicit fail-closed
  adapter requirements for Workstation and Hub.
- Responsive task workbench with create/start controls, extension slots,
  verified artifact preview, and named download.
- Contextual Task Slot contract and a hot-loadable six-contribution example
  using the same public Host API available to third-party developers.
- Task-level contributed-runner selection with generation-consistent dispatch
  and a stable `runner_unavailable` failure contract.
- Approval-gated contributed-sensor polling that shares the proactive Proposal
  ingress, bounds batches, and deduplicates retried proposal identities.
- Proactive Proposal to approval to guarded Agent runtime path.
- Bidirectional Tool Guard approval projection with distinct proposal and
  tool-rejection continuation semantics.

## Residual Risks and Deliberate Limits

1. Workstation and Hub have concrete Profile and deployment-adapter boundaries,
   but their distributed and multi-tenant adapters deliberately fail closed and
   remain outside the Lite milestone.
2. Hot activation covers ordinary Python and UI contributions. Kernel ABI
   changes, native dependency conflicts, and core migrations still require
   the manifest-declared scoped or full restart.
3. The Lite runner stores the final Agent response as a content-addressed
   Markdown artifact. It now has verified text/Markdown preview and named
   download; rich renderers and artifacts emitted directly by tools remain
   future extensions.
4. Task workbench execution and approved proactive execution use the existing
   Console/Agent runtime. In-process cancellation and absolute execution
   timeout are supervised; recovery of a running stream after process shutdown
   still requires a future durable scheduler rather than the current local
   supervisor.
5. Cross-platform behavior is statically designed with `pathlib` and SQLite;
   this acceptance run executed on macOS, not on Windows or Linux CI hosts.

## Rollback

No rollback was executed. The worktree contained unrelated user changes, so
a broad checkout or reset would be unsafe. Before rollback, export only this
feature's diff as a patch. Then reverse its existing-file hunks and remove
only the new Kernel, task, edition, contribution-generation, task workbench,
example, and documentation files listed by that patch. Preserve the unrelated
dirty files called out in the handoff. Runtime data can be disabled by
starting without the Lite edition; the local Ledger is isolated under
`<workspace>/.qwenpaw/lite/tasks.db`.
