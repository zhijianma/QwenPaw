# QwenPaw plugin quickstart

This guide adds Chat tools, a backend runner, invocation-scoped Memory, Prompt,
and Command Providers, a Harness Runner, an approval-gated sensor, and
contextual Task UI contributions without importing QwenPaw application
internals. Complete
references are in `examples/plugins/chat-tool-provider`,
`examples/plugins/runtime-provider-kit`, and
`examples/plugins/task-insights`.

## 1. Declare slots

Create `plugin.json` with `schema_version` set to `qwenpaw.plugin.v2`. Add
one item per capability under `contributions`. Backend entrypoints use
`module:attribute`; UI contributions point to the frontend JavaScript entry.
One frontend entry may register several slots atomically.

## 2. Implement against the stable SDK

Import public contracts from `qwenpaw.plugins.sdk`. A runner only needs an
async generator that yields `RunnerSignal` values. Wrap it with
`LocalAgentRunner`; the host owns sequencing, persistence, retries, and task
state transitions.

Do not import from `qwenpaw.kernel` or application packages in plugin code.
The Kernel is intentionally limited to standard-library, Pydantic, and
Kernel-local dependencies; `qwenpaw.plugins.sdk` is the stable public surface
that prevents plugins from coupling to Host implementation modules.

Use the typed, immutable `RuntimeContext` for task-scoped configuration. It
contains the Run's `invocation_id`, root `correlation_id`, pinned registry
generation, effective approval level, Workspace paths, an optional resume
checkpoint, and host-owned Artifact, Checkpoint, Approval, Side Effect, Usage,
and Cancellation services. Plugin code does not read FastAPI requests or
QwenPaw application globals:

```python
from collections.abc import AsyncIterator

from qwenpaw.plugins.sdk import (
    CostAccountingMode,
    LocalAgentRunner,
    Run,
    RunnerSignal,
    RuntimeContext,
    TaskOrder,
)


async def execute(
    order: TaskOrder,
    run: Run,
    context: RuntimeContext,
) -> AsyncIterator[RunnerSignal]:
    if context.checkpoint_broker is not None:
        await context.checkpoint_broker.save(
            runner_cursor={"phase": "started"},
            idempotency_key=f"{run.run_id}:started",
        )
    yield RunnerSignal(
        event_type="plugin.my-plugin.completed",
        payload={
            "attempt": run.attempt,
            "generation": context.registry_generation,
        },
    )
    yield await context.artifact_emitter.emit(
        kind="report",
        media_type="text/markdown",
        content=b"# Result\n",
        name="result.md",
        evidence_claim="Generated report",
    )


def create_runner() -> LocalAgentRunner:
    return LocalAgentRunner(
        "my-plugin.runner",
        execute_context=execute,
        cost_accounting=CostAccountingMode.ZERO,
    )
```

The two-argument callback remains supported as a compatibility adapter. New
plugins should use `execute_context` so resume and policy data stay typed.
The task-scoped `artifact_emitter` stores bytes outside the event ledger and
returns one canonical signal that binds the artifact and optional evidence.
Yield that signal exactly like any other runner output.

`checkpoint_broker.save()` records a safe recovery boundary without pausing
the active Run. The host supplies Task/Run identity and event sequence; a
plugin supplies only a JSON cursor, optional opaque workspace reference, and
an idempotency key. Keep the combined payload under 32 KiB. On resume, inspect
`context.resume_checkpoint`; do not assume an external provider session was
restored merely because the cursor exists.

When the runner knows the exact causal boundary, pass `step_id`,
`cause_event_id`, and `correlation_id` to the emitter or to
`verification_signal`. The host validates and persists those values in the
event envelope, then derives the Artifact/Evidence/Verification registry
provenance. Do not write audit identities into Artifact metadata: metadata is
plugin-controlled and is not a trusted provenance source. Omitting correlation
uses the Task Run root correlation supplied by the host.

Do not invent an Invocation ID. One Run attempt owns one
`context.invocation_id`; a resumed attempt gets a new value while retaining
`context.correlation_id`. The host adds both identities to every persisted
Runner event and carries them through Delivery and Inbox projections.

Every runner must classify monetary accounting. Use `ZERO` only when the
implementation cannot incur provider or external-service charges. Use
`REPORTED` when every billable operation emits `usage.recorded` with a trusted
`cost_micros`; otherwise use `UNKNOWN`. A Task with a hard cost ceiling fails
before an unknown runner starts, so L3 never treats missing price telemetry as
free execution. Legacy runners without the declaration are also unknown.

`LocalAgentRunner` also implements the optional `PreflightTaskRunner`
contract. Before publishing either `runner` or `harness.runner`, Lite sends a
`RunnerPreflightRequest` with `external_io_allowed=False`. The result must echo
the exact capability ID, Slot, candidate generation, contextual support, and
cost-accounting mode. The Host compares those claims with the staged Runner
object under a five-second deadline; it never calls `execute()` or
`execute_context()`. A legacy Runner without preflight records
`not_applicable`, while a malformed or inconsistent result blocks publication.

Custom Runner classes may implement the same protocol, but preflight must be a
pure contract probe: do not resolve a Workspace, open a provider session,
spawn a process, access the network, read credentials, or inspect user data.
This is a Plugin SDK capability boundary, not an OS sandbox; arbitrary local
Python can bypass it. Runtime Environment resolution, Sandbox, Policy,
Approval, cancellation, and budget enforcement remain the security boundary
for real execution.

`RuntimeContext.execution_contract` is also executable. If a Task declares a
`max_iterations` Exit Condition, an iteration-aware runner must emit
`RunnerSignal(event_type="runner.iteration", payload={"iteration": n})` before
each iteration; the host fails the Run when the declared limit is crossed.
To satisfy a required `explicit_signal` condition, emit
`exit_condition.met` with the exact `condition_id`. Required
`acceptance_met` and `artifact_emitted` conditions are evaluated from durable
Verification and Artifact projections, so a plugin cannot claim them through
an arbitrary success boolean.

When one of those conditions is declared with `required=False`, the host may
close the Runner stream after the condition becomes true, but only after every
required completion rule already passes. The host writes
`exit_condition.triggered`; plugins must not emit that event themselves.
Async-generator runners should release provider sessions and temporary
resources in `finally`, because safe early-stop closes the generator.

For a Chat tool, declare the `tool.provider` slot and return typed
`ToolDefinition` values:

```python
from qwenpaw.plugins.sdk import (
    ActionIdempotencyMode,
    InvocationScope,
    ToolDefinition,
    ToolHost,
    ToolSelection,
    current_action_execution,
)


async def project_summary() -> str:
    execution = current_action_execution()
    if execution is None:
        raise RuntimeError("project_summary requires the Action Host")
    # execution.chat_id is the owning ChatSpec.id when this action is scoped.
    # Forward execution.idempotency_key only when the real executor stores
    # and deduplicates it durably across retries.
    return "Project summary"


class ProjectTools:
    provider_id = "my-plugin.project-tools"

    async def health_check(self) -> bool:
        return True

    async def list_tools(
        self,
        scope: InvocationScope,
        selection: ToolSelection,
        host: ToolHost,
    ) -> tuple[ToolDefinition, ...]:
        return (
            ToolDefinition(
                function=project_summary,
                name="project_summary",
                tool_type="internal",
                idempotency_mode=ActionIdempotencyMode.HOST_GUARDED,
            ),
        )


def create_provider() -> ProjectTools:
    return ProjectTools()
```

The provider ID must equal `<plugin-id>.<contribution-id>`. New Chat
invocations automatically select installed `tool.provider` contributions from
their pinned generation. Existing invocations keep the generation they
started with. The host registers every `ToolDefinition` with Tool Guard before
building the Toolkit; raw callables are compatibility-only and new plugins
should not use them. Use `file`, `network`, `shell`, or `internal` as the
governance type, and set `target_param` when policy must inspect a path, URL,
command, or other target.

`action_kind` is an executor-family declaration, separate from the governance
type and effect. Ordinary tools may omit it and resolve to `tool`. Tools with a
`process` effect should declare it whenever the executor is known: an operating
system command uses `ActionKind.SHELL`, while a workflow orchestrator remains
`ActionKind.TOOL`. The legacy host still maps an undeclared process effect to
`shell` for older plugins, but new code must not rely on effect as a type
discriminator. A provider that owns a real browser execution boundary may set
`action_kind=ActionKind.BROWSER`; the same applies to future executor families
exposed by the SDK. Never use `action_kind` merely to change approval behavior,
and never infer it from a tool or policy name. The host still owns Policy,
Approval, Action Request/Result, and environment evidence for both system and
plugin providers.

`current_action_execution().chat_id` is the canonical owning `ChatSpec.id`.
`conversation_id` remains a deprecated read-only Python alias so existing
plugins can roll forward without rewriting durable records; new plugin code and
serialized contracts should use `chat_id`.

`idempotency_mode` is an execution guarantee, not a retry preference. Keep
the default `UNDECLARED` when the executor ignores Host keys. Use
`HOST_GUARDED` when only QwenPaw prevents duplicate admission. Declare
`EXECUTOR_ENFORCED` only when the real external executor receives
`current_action_execution().idempotency_key`, persists it, and returns the
same logical outcome without repeating the side effect. On an admitted retry,
`current_action_execution()` exposes a new `action_id`, the stable executor
key, the incremented attempt, and root/previous Action IDs. Falsely declaring
executor enforcement can duplicate external writes and is a contract defect.
The Host pins a bounded retry policy to the first Action and carries it across
attempts. A retry decision includes the next attempt and durable backoff, but
it is not proof that a dispatcher has executed the retry.
Lite stores exact retry input in an owner-only Host checkpoint only after
admission. Plugins receive the Action execution context, never the checkpoint
store or another provider's raw arguments.

The process-wide governance registry is used for discovery, conflict checks,
and deferred unload cleanup. Each guarded invocation tool also captures its
own governance snapshot. A hot replacement may change the same tool's type,
target extraction, sandbox requirement, or side-effect class for new
invocations without changing the policy behavior of an invocation already in
flight.

Declare non-secret settings in the contribution's `config_schema`, then read
the validated detached value with `host.config_snapshot()`. Bind credentials
in the Agent Profile as alias-to-reference mappings and request only a declared
alias with `host.credential(alias)`. The returned handle exposes detached
public values and named secret reads; it never exposes the credential store or
accepts an arbitrary reference. Invalid configuration fails before
`list_tools()` runs. An unbound alias returns `None`, while a bound alias whose
record or secret is unavailable fails closed.

`ToolHost` deliberately contains only configuration, credential, and
interaction services. Filesystem, Workspace, request-context, governor, and
application objects are not part of the plugin contract. The built-in
Workspace Tool Provider uses a private compatibility Host to migrate existing
tools through the same `ToolDefinition` and Tool Guard pipeline; plugins must
not depend on that adapter.

Before publishing a `tool.provider`, Lite runs a bounded catalog-discovery
scenario against the staged implementation. It supplies an inert Invocation,
empty non-secret configuration, no credential handles, no interaction broker,
and never invokes a returned tool. The gate rejects non-sequence catalogs,
more than 128 tools, duplicate or oversized names, invalid result objects,
governance target/pattern parameters absent from the callable, catalogs over
64 KiB, and discovery taking over five seconds. A valid schema that requires
runtime configuration produces `not_applicable`, not a false pass or failed
install; an invalid schema fails publication. System and plugin providers use
the same scenario and Evidence Bundle.

MCP is a concrete Driver protocol, not a second Tool Provider namespace. MCP
servers are discovered by the selected `driver.provider`, translated to
provider-neutral Driver tool definitions, and admitted through the same Tool
Guard, Approval, credential, and invocation lifecycle. Do not register the
same MCP server again as a `tool.provider`, because that would create duplicate
tool identities and competing lifecycle owners.

Tool Providers can request structured user input or publish a non-blocking
suggestion without importing an application router or accessing private
request-context keys. Obtain the invocation-bound broker from `ToolHost` and
close over it in the contributed tool:

```python
from qwenpaw.plugins.sdk import (
    InteractionOption,
    ToolDefinition,
    UserInputReason,
)


async def list_tools(self, scope, selection, host):
    del scope, selection
    interactions = host.interaction_broker()

    async def choose_output_format() -> str:
        if interactions is None:
            return "Structured user interaction is unavailable."
        request = await interactions.defer_user_input(
            reason=UserInputReason.MATERIAL_PREFERENCE,
            title="Choose output format",
            prompt="Which format should be generated?",
            options=(
                InteractionOption(option_id="md", label="Markdown"),
                InteractionOption(option_id="html", label="HTML"),
            ),
        )
        return (
            "Input requested; end this invocation and continue from the "
            f"durable response. Interaction: {request.interaction_id}."
        )

    return (
        ToolDefinition(
            function=choose_output_format,
            name="choose_output_format",
            tool_type="internal",
        ),
    )
```

`defer_user_input()` is the default for long-running work: it persists the
request, ends the current Invocation at its safe point, and creates a durable
continuation Submission after the response. `ask_user()` is only the live
short-path when the caller intentionally keeps the current Invocation waiting;
it is released by timeout or cancellation and must not be used for cross-process
continuation. `suggest()` persists and returns immediately; it can never pause
the Runtime. All three are owned by `ChatSpec.id + invocation_id`, so plugins
must not introduce their own session identity, waiter, or frontend queue.

Every blocking user-input request must declare one `UserInputReason`:
`MISSING_REQUIRED_FACT`, `MATERIAL_PREFERENCE`, `SCOPE_AUTHORIZATION`, or
`HIGH_IMPACT_DECISION`. Use Approval for policy-governed actions and use
Suggestion for optional guidance. Do not use Ask User as a step-by-step
"continue?" gate or as a substitute for Steer, Interrupt, resource recovery,
budget policy, or side-effect reconciliation.

For invocation-scoped memory, implement the public `MemoryProvider` shape and
return a `MemorySession`:

```python
from qwenpaw.plugins.sdk import InvocationScope, MemoryHost, ToolDefinition


class ProjectMemorySession:
    def get_prompt(self) -> str:
        return "Remember the project's established terminology."

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        return ()

    async def close(self) -> None:
        return None


class ProjectMemory:
    provider_id = "my-plugin.project-memory"

    async def health_check(self) -> bool:
        return True

    async def open(
        self,
        scope: InvocationScope,
        host: MemoryHost,
    ) -> ProjectMemorySession:
        del scope, host
        return ProjectMemorySession()
```

Declare it with the `memory.provider` slot. A session belongs to exactly one
pinned invocation, so `close()` must release only session-owned resources.
Third-party providers never receive the Workspace's private memory backend.
Use `host.config_snapshot()` for schema-validated non-secret configuration and
`host.state(scope)` for durable JSON state. `MemoryStateScope.AGENT` shares a
namespace across that Agent's conversations; `MemoryStateScope.CONVERSATION`
is owned by the stable `ChatSpec.id` and is unavailable when a transport has no
Conversation identity. It never falls back to `session_id`.

State mutations are compare-and-swap operations: create with
`expected_revision=0`, then update or delete using the revision returned by
`read()`/`write()`. A stale writer receives `MemoryStateConflictError`. The
Lite adapter stores at most 256 KiB of JSON per key in SQLite and isolates rows
by provider ID, scope and owner. Provider sessions do not close this Host-owned
store. Any typed memory tools use the same `ToolDefinition` governance contract
as `tool.provider` tools. The built-in Workspace Memory Provider is selected by
default; selecting a third-party memory provider is an explicit Agent Profile
capability override.

The system adapter receives a private compatibility Host; third-party
providers receive a distinct minimal Host object, not merely a broader object
typed as `MemoryHost`. Runtime attribute probing therefore cannot recover the
legacy Workspace memory manager.

Lite also opens the staged `MemoryProvider` once before publishing its
generation. The promotion Host uses empty non-secret configuration and a
process-local revisioned State Store; it never points at the user's durable
memory database. The host validates a 32 KiB prompt budget, applies the same
bounded tool-catalog rules as `tool.provider`, and requires the Session to
close within five seconds. A valid schema requiring Agent Profile configuration
is recorded as `not_applicable`; malformed schema, invalid Session output, or
failed cleanup blocks publication. The scenario stores only the outcome, never
prompt text or state values. This is SDK capability isolation, not an OS sandbox:
locally installed Python remains trusted code and can bypass the Host if it
imports filesystem or network libraries directly.

Workspace-scoped memory backend plugins registered through the compatibility
`register_memory_backend` API receive a `MemoryBackendContext`. When a durable
background job must notify the user, use
`context.operational_event_publisher`; do not import `app.inbox_store`, a
FastAPI router, or a SQLite adapter:

```python
publisher = context.operational_event_publisher
if publisher is not None:
    await publisher(
        agent_id=context.agent_id,
        source_type="memory",
        source_id="daily_digest",
        event_type="daily_digest_result",
        status="success",
        severity="info",
        title="Daily digest completed",
        body="The digest is ready.",
        payload={"date": "2026-09-29"},
    )
```

The Host binds the producer and Agent identity, commits an immutable
`OperationalEvent`, pins the current capability generation, settles Delivery,
and then projects Inbox. Publish completed domain facts only; reasoning deltas,
progress ticks, and transient retries belong in runtime telemetry rather than
Inbox. Replaying identical content is idempotent. A mismatched `agent_id`
fails closed.

`InvocationScope.conversation_id` is the stable `ChatSpec.id` when an
invocation belongs to a persisted Chat. It is `None` for transports that have
no Conversation identity and never falls back to `session_id`. Plugins should
use it for Conversation ownership and use `session_id` only when adapting a
legacy transport or persistence implementation.

Conversation branching follows the same identity rule. The public SDK exports
`ConversationForkCommand`, `ConversationForkOrigin`,
`ConversationForkResult`, and `ConversationForkPort`. A host-provided Port
accepts `agent_id + parent_conversation_id + source_message_id`; plugins must
not derive or persist legacy `session_id` values. Fork ownership, completed
response validation, idempotency, and lineage authorization remain Kernel
infrastructure rather than a replaceable plugin contribution.

The public SDK also exposes `EnvironmentContract`, `EnvironmentResolution`,
and `EnvironmentRef`. A Chat provider may inspect
`InvocationScope.environment_contract` and
`InvocationScope.environment_resolution` to understand the environment the
Host actually resolved. Treat the Resolution as evidence, not as permission:
it does not expand the provider's tool or credential access.

Inspect `EnvironmentResolution.evidence_level` before relying on a claim.
`host_verified` means the QwenPaw host checked the fact, `provider_declared`
means an external Harness accepted or advertised the setting, and
`provider_attested` is reserved for independently verifiable remote-runtime
evidence. `declared_constraints` must never be treated as
`enforced_constraints`; for example, Codex `workspace-write` and Qoder
`acceptEdits` describe different Provider policies and neither proves a host
Sandbox by itself.

An Action may carry a different `EnvironmentRef` from the Invocation default
when the Host applies a per-tool Sandbox. Resolve that reference through
host-owned evidence; do not assume the Invocation's host environment proves
the Action's isolation. Environment variable names may appear in the Contract,
but their values never belong in plugin-visible evidence.

Do not implement or import an environment store or resolver from a plugin.
Resolution, immutable evidence storage, Sandbox enforcement, and edition
selection are host-owned infrastructure. If a contract cannot be enforced,
the Host fails the Invocation before the provider runs instead of silently
downgrading it.

### Delivery Adapter

A `delivery.adapter` contribution translates an already committed public
`DeliveryRequest` into one external side effect. It does not read Task runtime
state, mutate Ledger facts, or create Inbox records. The Host selects the
adapter from `DeliveryDestination.adapter_id`, pins the request's registry
generation, owns attempts and leases, and persists the returned
`DeliveryReceipt`.

Implement `adapter_id`, `supports(request)`, and
`deliver(request, attempt)`. Return a receipt with the exact request identity
and attempt. Report a definite destination rejection as `failed`; use
`uncertain` when the external effect may have happened but confirmation was
lost. A crash after the external side effect and before receipt persistence is
inherently ambiguous, so pass `delivery_id` to the external system as its
idempotency key whenever that system supports one. Never silently claim an
unsupported destination.

Declare up to eight bounded route examples in Contribution metadata so the
install-time gate can verify routing without producing an external effect:

```json
{
  "id": "local-jsonl",
  "slot": "delivery.adapter",
  "entrypoint": "delivery_provider.provider:create_adapter",
  "metadata": {"delivery_addresses": ["local-jsonl"]}
}
```

Promotion calls only `supports()`: every declared address must accept a final
text projection owned by this adapter, and a request carrying a foreign
`adapter_id` must be rejected. It never calls `deliver()`. Missing route hints
produce `not_applicable`, while malformed hints or inconsistent routing block
publication. Evidence records only outcome and capability identity, not the
address or payload. Keep `supports()` deterministic and side-effect free.

See `examples/plugins/delivery-provider`. Its JSONL adapter intentionally uses
only `qwenpaw.plugins.sdk`, records the stable `delivery_id`, and can be
installed or removed without restarting the service. The system Inbox adapter
and this plugin pass the same Task Event to durable Receipt behavior contract;
replaying the same projection does not repeat the side effect.

Profile capability selection is persisted in `agent.json` and applies only to
new invocations. Scalar Slots select one provider, while tuple-valued Slots
replace the automatically discovered provider list:

```json
{
  "capability_selection": {
    "memory_provider_id": "my-plugin.project-memory",
    "driver_provider_id": "my-plugin.project-driver",
    "tool_provider_ids": [
      "my-plugin.project-tools",
      "qwenpaw.system.workspace-tools"
    ]
  }
}
```

Omitted or `null` fields retain automatic selection. An empty provider list
explicitly disables a multi-provider Slot. To disable an optional scalar Slot,
use `disabled_optional_slots`, whose supported values are `strategy`,
`memory.provider`, and `driver.provider`. Selecting and disabling the same Slot
is invalid:

```json
{
  "capability_selection": {
    "disabled_optional_slots": ["driver.provider"]
  }
}
```

Provider IDs are resolved against one pinned registry generation before the
invocation starts. Missing or incorrectly slotted IDs fail closed and release
the generation lease. Editing the Profile affects the next invocation; a
running invocation retains its original selection and implementations.

The executable reference is
`examples/plugins/runtime-provider-kit/runtime_provider_kit/memory.py`. It
uses `pathlib` rather than platform-specific path splitting and imports all
QwenPaw contracts through `qwenpaw.plugins.sdk`.

External coding or research engines that already translate their events into
the common Task stream may use `harness.runner`. It implements the same
`TaskRunner`/`ContextualTaskRunner` protocol as `runner`; the different Slot
preserves selection and policy intent without creating another execution
pipeline. `TaskExecutionCoordinator` accepts both Slots, pins their registry
generation, supplies the same `RuntimeContext`, and persists the same
`RunnerSignal` stream:

```python
from collections.abc import AsyncIterator

from qwenpaw.plugins.sdk import (
    CostAccountingMode,
    LocalAgentRunner,
    Run,
    RunnerSignal,
    RuntimeContext,
    TaskOrder,
)


async def execute_harness(
    order: TaskOrder,
    run: Run,
    context: RuntimeContext,
) -> AsyncIterator[RunnerSignal]:
    yield RunnerSignal(
        event_type="plugin.my-plugin.harness.completed",
        source="my-plugin.harness",
        payload={"generation": context.registry_generation},
    )
    yield await context.artifact_emitter.emit(
        kind="agent.response",
        media_type="text/markdown",
        content=order.objective.encode("utf-8"),
        name="task-result.md",
        evidence_claim="Harness completed the requested objective",
    )


def create_harness_runner() -> LocalAgentRunner:
    return LocalAgentRunner(
        "my-plugin.harness",
        execute_context=execute_harness,
        cost_accounting=CostAccountingMode.UNKNOWN,
    )
```

This boundary does not expose a raw subprocess or provider session. The host
still owns cancellation, timeout, approval, Artifact/Evidence emission and
generation release. See
`examples/plugins/runtime-provider-kit/runtime_provider_kit/harness.py`.
The example emits both a provider-owned lifecycle signal and a content-addressed
Artifact with Evidence; plugins should use the injected emitter instead of
writing result paths into Ledger payloads.

Lite publishes the built-in Codex and Qoder adapters through the same boundary
as `qwenpaw.system.tasks.codex-harness` and
`qwenpaw.system.tasks.qoder-harness`. Both use the `harness.runner` Slot. Their
normalized reasoning, tool, assistant-text, error, cancellation, and terminal
events become the same `RunnerSignal` stream as a native or plugin Runner; the
final assistant response is emitted through the same Artifact/Evidence host.
Task-owned Harness execution does not create a Chat Queue lease, because the
Task Supervisor already owns cancellation and durable run state. A provider
event cannot override its tool call identity or claim that host usage has
already been accounted.

Planner and Strategy are independent public Task slots. A Planner returns
`PlanStep` values before the Run starts; a Strategy prepares a bounded JSON
directive after the host creates the immutable `RuntimeContext` and before it
opens the Runner stream:

```python
from qwenpaw.plugins.sdk import (
    JsonObject,
    PlanStep,
    RuntimeContext,
    TaskOrder,
)


class ReviewPlanner:
    planner_id = "my-plugin.review-planner"

    async def plan(self, order: TaskOrder) -> tuple[PlanStep, ...]:
        return (
            PlanStep(title="Review objective", objective=order.objective),
        )


class ReviewStrategy:
    strategy_id = "my-plugin.review-strategy"

    async def prepare(
        self,
        context: RuntimeContext,
        order: TaskOrder,
    ) -> JsonObject:
        return {
            "review_depth": "concise",
            "generation": context.registry_generation,
        }
```

Declare them as `planner` and `strategy`. Their IDs must equal
`<plugin-id>.<contribution-id>`. The Orchestrator resolves Planner, Strategy,
and Runner from one pinned generation, persists the Plan before starting the
Run, validates Strategy output as JSON with a 32 KiB limit, and exposes a
detached copy as `context.strategy_parameters`. A plugin must not place
credentials, application objects, or mutable provider sessions in Strategy
parameters. See `examples/plugins/task-insights` for a complete
Planner → Plan → Strategy → Runner → Artifact/Evidence example.

Agent Modes use a separate `agent.mode.provider` slot. Do not implement a
Chat mode as the Task `strategy` slot: `strategy` prepares a durable Task Run,
while Agent Modes own interactive turn behavior. The current public
`AgentModeProvider` / `AgentModeSession` contract pins active names and the
turn-start lifecycle. `reset_conversation()` is called by both `/clear` and
`/new`; it must clear only state owned by that invocation and must not delete
Workspace configuration. The built-in provider adapts Default, Coding, Goal,
Mission, and custom-loop modes through a private compatibility Host. A plugin
receives a different minimal Host containing only a detached, schema-validated
configuration snapshot; it cannot inspect the Workspace context, enumerate
system modes, or invoke their lifecycle. Plugin-owned prompt, command, tool,
hook, and stop-gate behavior belongs in those independent public Slots rather
than being hidden inside a Mode Provider. See
`examples/plugins/runtime-provider-kit/runtime_provider_kit/mode.py`.

The SDK exports `AgentModeState` and the Host methods `read_state()`,
`write_state()`, and `clear_state()`. State is automatically namespaced by Provider, Agent,
`ChatSpec.id`, and `state_key`; writes require the revision returned by the
previous read. A new plugin generation may read the previous generation's
state and explicitly advance `state_schema_version`, while a stale Session
cannot overwrite a newer revision. Each value is limited to 64 KiB. The SDK
does not export the backing Store: never open `.qwenpaw/lite/mode-state.db` or
`.qwenpaw/lite/goals.db` directly from a plugin. Domain-specific Goal outcome
state remains in `GoalExecutionStore`; generic Mode State does not replace it.

`reset_conversation()` must call `clear_state()` for every known state key.
Clear writes an empty value and advances revision instead of deleting the row,
so an older invocation cannot recreate pre-clear state with revision zero.

See `examples/plugins/runtime-provider-kit/runtime_provider_kit/mode.py` for
a lifecycle counter that persists through the Host without accessing
Workspace internals.

`agent.factory` is deliberately a system-only Contribution Slot. It pins the
QwenPaw/AgentScope framework adapter to an invocation generation, but it is not
a plugin extension contract and is not exported by `qwenpaw.plugins.sdk`.
Third-party manifests declaring it fail before implementation loading with a
`system_slot` diagnostic. Extend Agent behavior through the public Mode,
Prompt, Tool, Command, Hook, Gate, Memory, or Driver Slots instead; this keeps
plugins independent of `HookContext`, application services, and framework
event objects.

Task resume keeps the previous Run's `strategy_id` unless the host supplies an
explicit replacement in `RuntimeLaunchConfig`. The new attempt resolves that
same ID from its newly pinned generation; it never reuses a stale strategy
object and never silently falls back to Default when the prior strategy was
Coding, Goal, Mission, or a plugin contribution. A missing replacement in the
new generation fails closed before the Runner starts.

Scheduler plugins implement the process-scoped `scheduler.provider` Slot.
Implement `SchedulerProvider.open(host)` and obtain the admitted
`SchedulerPort` through `SchedulerHost.scheduler_store()`. Import
`ScheduleDefinition`, `ScheduleWorkKind`, `ScheduleTrigger`, `ScheduleFire`,
`ScheduleLease`,
`SchedulerHost`, `SchedulerPort`, and `SchedulerProvider` only from
`qwenpaw.plugins.sdk`. The plugin must not choose a database path, read a
storage environment variable, or close Host-owned persistence. A scheduler
owns time triggers and revisioned Fire leases. For `ScheduleWorkKind.TASK`, it
must create `TaskSource.SCHEDULE` work through the host's Task application
boundary instead of calling a Chat Runtime directly. Host-owned `SERVICE` work
binds the completed Lease to `completion_ref` rather than inventing a Task ID;
third-party Providers persist the same contract but never receive Python
callbacks. `DELIVERY` schedules bind a stable Delivery ID before an adapter is
invoked; adapter execution and receipts remain owned by the Delivery Slot, not
the Scheduler. Task budget, approval, artifacts, and evidence remain outside
the Scheduler.
`schedule_id` is Agent-scoped: definitions and fires both carry `agent_id`,
and the idempotency domain is
`agent_id + schedule_id + idempotency_key`. Implementations must never bind a
process-scoped Scheduler to the first Workspace that resolves it.
An interval trigger may carry aware `start_at` and `end_at` bounds; stores must
round-trip both values unchanged so anchored recurrence remains stable after a
restart. Unbounded intervals leave both fields empty.
The host pins the registry generation before claiming a Fire and persists that
generation in the Fire identity. Planner, Strategy, and Runner must resolve
from the same retained generation; replacing a Scheduler affects only later
fires. An idempotent replay returns the Task already bound to the lease and
must never create a second Task or Run.

Before publication, the Host opens the Provider with an isolated process-local
Store and performs read-only catalog discovery for a synthetic Agent. It
rejects non-tuple results, foreign Agent ownership, duplicate IDs, more than
128 definitions, or catalogs over 64 KiB. Every mutating Store method fails in
this scenario, and no user SQLite file is opened. This verifies the Provider
boundary; runtime durability and Fire idempotency remain covered by the shared
Scheduler behavior contract.

Prompt extensions use `prompt.provider` and return typed fragments:

```python
from qwenpaw.plugins.sdk import InvocationScope, PromptFragment, PromptHost


class ProjectPromptProvider:
    provider_id = "my-plugin.project-prompt"

    async def health_check(self) -> bool:
        return True

    async def list_fragments(
        self,
        scope: InvocationScope,
        host: PromptHost,
    ) -> tuple[PromptFragment, ...]:
        del scope, host
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.terminology",
                content="Use the project's established terminology.",
                priority=120,
            ),
        )
```

Every fragment ID must begin with `<provider-id>.`; duplicate or foreign IDs
fail closed. New invocations automatically select installed Prompt Providers
from their pinned generation. Fragments are ordered by `priority` and then ID,
so ordering remains deterministic across installation sequences. Treat prompt
content as instructions authored by the plugin, never as a channel for secrets
or untrusted document contents.

Slash-command extensions use `command.provider`. A provider opens one session
per pinned invocation; the session publishes a static `CommandDefinition`
catalog and returns an explicit `CommandResult`:

```python
from qwenpaw.plugins.sdk import (
    CommandDefinition,
    CommandDisposition,
    CommandMessage,
    CommandResult,
)


class ProjectCommandSession:
    provider_id = "my-plugin.project-commands"
    allows_dynamic_fallback = False

    def list_commands(self):
        return (
            CommandDefinition(
                command_id=f"{self.provider_id}.review",
                provider_id=self.provider_id,
                name="review",
                help_text="Review the current project",
            ),
        )

    async def dispatch(self, request):
        del request
        return CommandResult(
            disposition=CommandDisposition.RESPOND,
            message=CommandMessage(text="Review started."),
        )

    async def fallback(self, request):
        del request
        return CommandResult(
            disposition=CommandDisposition.NOT_HANDLED,
        )

    async def close(self):
        return None
```

The session `provider_id` and every definition's `provider_id` must match.
Only system providers may mark names as protected or enable dynamic fallback.
`/clear`, `/new`, and the other system command names are reserved. Two plugin
providers declaring the same ordinary name make that name explicitly
ambiguous; installation order never decides dispatch. Use `CONTINUE` only when
the command handled or transformed the input and the Agent should still run.
The legacy `register_slash_command()` API remains behind the Workspace adapter,
but new plugins should declare the `command.provider` Contribution directly.

When a loaded plugin calls a legacy registration API that already has an
equivalent Contribution contract, QwenPaw keeps the compatibility path working
and adds a structured `migration_diagnostics` entry to `/api/plugins`. Each
entry contains the called API, target Slot, recovery guidance, and a v2 manifest
fragment. The following mappings are currently exact:

| Legacy API | Contribution Slot |
|---|---|
| `register_tool()` | `tool.provider` |
| `register_memory_backend()` | `memory.provider` |
| `register_slash_command()` | `command.provider` |
| `register_mode()` | `agent.mode.provider` |
| `register_runtime_hook()` | `hook.provider` |
| `register_agent_stop_handler()` | `loop.gate.provider` |
| `register_prompt_section()` | `prompt.provider` |

Host lifecycle APIs such as HTTP router, install/uninstall hooks, inbound
channel registration, model-provider registration, and skill-provider
registration are not labelled as replaceable until an equivalent public Slot
exists. This prevents migration guidance from changing plugin semantics.

For a loaded legacy plugin, generate one merged, non-mutating migration plan:

```bash
qwenpaw plugin migration-plan <plugin-id>
qwenpaw plugin migration-plan <plugin-id> --format json
```

The plan deduplicates target Slots, preserves already-declared Contributions,
and produces a `contributions_to_add` patch. Provider entrypoints remain
explicit placeholders because a legacy handler or hook instance is not a v2
provider factory. The command never edits plugin files and always reports
`safe_to_apply: false`; replace the placeholders, implement the public Slot
protocol, validate, and hot-install the plugin before removing legacy calls.

### UI Contribution activation

V2 UI Contributions must use a plugin-relative JavaScript entrypoint such as
`frontend/index.js`. Absolute paths, `..` traversal, backslashes, and non-JS
entrypoints fail before a registry generation is published.

The Console loads v2 UI bundles in a host-owned activation transaction. A
bundle may register only its own plugin ID and the `ui.*` Slots declared in its
manifest. Every declared Slot must register synchronously while the bundle is
being imported. Undeclared, spoofed, missing, or delayed registrations fail
closed. On hot replacement, new registrations are staged; a failed bundle
keeps the previous UI generation active. V1 frontend bundles continue through
the explicit compatibility loader until they migrate to Contributions.

Lifecycle extensions use `hook.provider`. Sessions publish a fixed
`HookDefinition` catalog; the host then orders all providers by declared
dependencies and executes hooks through framework-independent `HookOutcome`
values:

```python
from qwenpaw.plugins.sdk import (
    HookDefinition,
    HookOutcome,
    LifecyclePhase,
)


class ProjectHookSession:
    provider_id = "my-plugin.project-hooks"

    def __init__(self, host):
        self.host = host

    def list_hooks(self):
        return (
            HookDefinition(
                hook_id=f"{self.provider_id}.terminology",
                provider_id=self.provider_id,
                phase=LifecyclePhase.PRE_EXECUTE,
                priority=120,
            ),
        )

    async def run_hook(self, hook_id):
        self.host.inject_context(
            "Use the project's established terminology.",
            source=hook_id,
        )
        return HookOutcome()

    async def close(self):
        return None


class ProjectHookProvider:
    provider_id = ProjectHookSession.provider_id

    async def health_check(self):
        return True

    async def open(self, scope, host):
        del scope
        return ProjectHookSession(host)
```

Hook IDs and provider ownership must match. `before` and `after` contain full
hook IDs and may cross provider boundaries within the same phase. Cycles fail
closed. `SKIP_AGENT` is sticky for the phase, while `SHORT_CIRCUIT` stops the
phase immediately and requires a `HookMessage`. The system compatibility
provider keeps existing `HookBase` plugins working, but new plugins should not
depend on `HookContext`, AgentScope `Msg`, or mutable Workspace registries.
The runnable reference is
`examples/plugins/runtime-provider-kit/runtime_provider_kit/hook.py`; it uses
only the public SDK and is activated from the same manifest as the other
runtime providers.

ReAct loop decisions use `loop.gate.provider`. This slot decides whether the
current reasoning loop should continue or terminate; it does not cancel a
request, process, or Task. Sessions publish a fixed `StopGateDefinition`
catalog for the invocation and evaluate framework-independent inputs:

```python
from qwenpaw.plugins.sdk import (
    StopGateAction,
    StopGateDecision,
    StopGateDefinition,
)


class ReviewGateSession:
    provider_id = "my-plugin.review-gates"

    def list_gates(self):
        return (
            StopGateDefinition(
                gate_id=f"{self.provider_id}.review",
                provider_id=self.provider_id,
                priority=120,
                scope="goal",
            ),
        )

    def is_active(self, gate_id):
        return gate_id.endswith(".review")

    async def evaluate(self, gate_id, gate_input):
        del gate_id
        if gate_input.iteration < 2:
            return StopGateDecision(
                action=StopGateAction.INTERRUPT_AND_CONTINUE,
                continuation_message="Review the answer once more.",
                reason="minimum review pass",
            )
        return StopGateDecision(action=StopGateAction.TERMINATE)

    async def start_turn(self):
        return None

    async def reset_conversation(self):
        return None

    async def close(self):
        return None


class ReviewGateProvider:
    provider_id = ReviewGateSession.provider_id

    async def health_check(self):
        return True

    async def open(self, scope, host):
        del scope, host
        return ReviewGateSession()
```

Unscoped gates run before the selected scoped gate. The first active explicit
scope suppresses the `default` scope; otherwise `default` is used. When tool
calls are present, a terminating decision is deferred until those calls have
completed. Existing invocations retain their pinned catalog when a plugin is
hot-replaced, while new invocations use the new generation. `/clear` and
`/new` reset the pinned session. New plugins should declare this Contribution
instead of mutating the Workspace stop-handler registry.
The runnable reference is
`examples/plugins/runtime-provider-kit/runtime_provider_kit/stop_gate.py`.
A Stop Gate only controls the ReAct reasoning loop; process cancellation,
Queue interrupt, and user Steer remain separate host-owned infrastructure.

Artifact projections use `artifact.renderer`. A renderer never changes the
stored `ArtifactRef`; it returns a bounded derived view tied to the original
`content_hash`. Lower priority numbers run first, and the host tries the next
compatible renderer if one raises or violates the contract:

```python
from qwenpaw.plugins.sdk import (
    ArtifactRenderDisposition,
    ArtifactRenderResult,
)


class ReportRenderer:
    renderer_id = "my-plugin.report-renderer"
    priority = 200

    async def health_check(self):
        return True

    def supports(self, artifact, disposition):
        return (
            artifact.kind == "my-plugin.report"
            and disposition is ArtifactRenderDisposition.INLINE
        )

    async def render(self, request):
        source = request.content.decode("utf-8")
        return ArtifactRenderResult(
            renderer_id=self.renderer_id,
            content=f"# Report\n\n{source}".encode("utf-8"),
            media_type="text/markdown",
            filename=request.filename,
            disposition=request.disposition,
            source_content_hash=request.artifact.content_hash,
        )
```

Declare the implementation in an `artifact.renderer` Contribution. Inline
results currently must be `text/plain`, `text/markdown`, or
`application/json`; HTML and SVG are rejected even when a plugin returns them.
Attachment rendering must preserve the verified source bytes and media type.
Before a renderer is published into a new capability generation, Lite runs a
bounded host scenario against staged implementations. The current fixture is a
`task.summary` Markdown Artifact in inline and attachment dispositions. A
supported fixture must preserve renderer identity, source hash, disposition,
filename, output budget, safe inline media type, and attachment bytes. A
contract violation or timeout blocks publication; an unsupported fixture is
recorded as `not_applicable`, never as a pass. Scenario evidence is persisted
in the same Promotion Evidence Bundle for system and plugin providers. This is
an install-time compatibility gate, not a plugin sandbox; renderer code must
still be treated as trusted local extension code.
The Task projection exposes `preview.available`, `renderer_id`, and
`registry_generation`, so UI code must not guess support from file extensions.
`ui.artifact.preview` remains an optional presentation Slot and cannot bypass
the backend Renderer or read unverified bytes. The
`examples/plugins/task-insights` plugin contains a complete backend Renderer.

Driver extensions use `driver.provider`. A session returns
`DriverToolDefinition` values and provider-owned `PromptFragment` values; it
never returns AgentScope `ToolBase`, FastAPI objects, or arbitrary callables:

```python
from qwenpaw.plugins.sdk import (
    ActionIdempotencyMode,
    DriverApprovalRequest,
    DriverToolDefinition,
    PromptFragment,
    current_action_execution,
)


class ProjectDriverSession:
    provider_id = "my-plugin.project-driver"

    def __init__(self, host):
        self.host = host

    async def invoke_echo(self, payload):
        execution = current_action_execution()
        if execution is None:
            raise RuntimeError("driver_echo requires the Action Host")
        await self.host.require_approval(
            DriverApprovalRequest(
                provider_id=self.provider_id,
                capability_id="driver://project/local/tools/echo#invoke",
                tool_name="driver_echo",
                redacted_arguments={"text": payload.get("text", "")},
            ),
        )
        return {"echo": payload.get("text", "")}

    def list_tools(self):
        return (
            DriverToolDefinition(
                provider_id=self.provider_id,
                capability_id="driver://project/local/tools/echo#invoke",
                name="driver_echo",
                description="Echo text after Driver policy admission.",
                effect="external_write",
                risk="high",
                reversible=False,
                idempotency_mode=ActionIdempotencyMode.UNDECLARED,
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
                invoke=self.invoke_echo,
            ),
        )

    def prompt_fragments(self):
        return (
            PromptFragment(
                fragment_id=f"{self.provider_id}.policy",
                content="driver_echo is governed by Project Driver policy.",
            ),
        )
```

QwenPaw validates session/provider ownership, unique capability and tool IDs,
prompt ownership, count and byte limits before Toolkit publication. The host
then performs the only conversion to AgentScope. A Driver definition means
the Driver owns authentication and policy admission before `invoke` returns;
plugins must not use it to bypass Tool Guard. `DriverHost.require_approval()`
uses the same authoritative Approval fact, Task bridge and blocking
Interaction projection as built-in Driver policy. It returns only after an
approval; denial, timeout or persistence failure raises
`DriverApprovalRejectedError`, while Invocation cancellation propagates
normally. Never catch that error and execute the side effect anyway. See
`examples/plugins/runtime-provider-kit/runtime_provider_kit/driver.py`.

Promotion opens the staged Driver once with empty non-secret configuration,
no credentials, and an approval method that always rejects. It validates the
same Kernel-owned Session/catalog contract used at runtime: provider ownership,
unique capability IDs and names, 128-tool and 64 KiB catalog limits, at most
16 Prompt Fragments and 32 KiB of prompt content. It never calls `invoke()`.
Requesting approval during `open()` is invalid because no real Invocation or
Action exists yet; approval belongs inside the returned tool's invocation
path. The Session must close within five seconds even after validation fails.
Built-in Driver discovery uses a separate private Host returning an empty
compatibility catalog, so third-party plugins cannot probe `load()`.

Every Driver definition must classify its effect, risk, and reversibility so
the host can persist an `ActionRequest` before invocation. The compatibility
defaults are deliberately conservative (`external_write`, `high`, and not
reversible); read-only providers should declare narrower values explicitly.
Approvals requested during `invoke` are linked to that Action by the host.
Plugins neither create Action IDs nor write Action records directly.

Third-party providers receive a minimal `DriverHost` containing only config,
pre-bound credential handles, and the unified approval method. The built-in
Workspace adapter receives a different private compatibility Host with
`load()` access. Plugin code cannot access `DriverManager`, request context,
Workspace, or that compatibility method through its runtime Host.

A Driver contribution may declare a JSON `config_schema` in `plugin.json`.
QwenPaw carries that schema into the pinned capability generation and validates
the selected Profile value before calling `DriverProvider.open()`. The provider
receives a detached snapshot through `host.config_snapshot()`; invalid,
oversized, or undeclared configuration fails closed without opening a session.

Non-secret values and credential references are configured separately:

```json
{
  "capability_configs": {
    "my-plugin.project-driver": {
      "endpoint": "https://service.example"
    }
  },
  "capability_credential_refs": {
    "my-plugin.project-driver": {
      "service": "credential:project-service"
    }
  }
}
```

Never put tokens or passwords in `capability_configs`, the manifest, or an
Invocation. Call `host.credential("service")` to obtain the alias-scoped
`DriverCredentialHandle`; it exposes detached public values and
`read_secret(name)`, but not the credential store or arbitrary references.
Missing aliases return `None`; missing records or fields raise
`DriverCredentialUnavailableError`. Handles redact the backing reference and
secret values from diagnostics. A plugin can access only aliases configured
under its exact capability ID.

A new sensor should implement `ContextualProposalSensor`: expose `sensor_id`,
keep the compatibility `propose()` method, and implement
`propose_context(context)`. `SensorContext` contains the authenticated
`agent_id`, pinned `registry_generation`, and a host-owned JSON trigger payload
limited to 32 KiB. A sensor may return at most 25 `Proposal` values, and every
proposal `source` must equal its exact capability ID. Legacy
`ProposalSensor.propose()` implementations remain supported but do not receive
context. The host never lets a sensor execute work directly. Polling
`POST /api/tasks/sensors/<capability-id>/poll` sends every proposal through the
same durable Task and human-approval ingress used by Lite proactive cognition.
The resulting Task records `sensor_id` and `sensor_registry_generation`, so a
decision remains auditable after a hot replacement. Built-in proactive memory
uses the same system Sensor adapter and no longer calls proposal persistence
directly.

Frontend entries execute as ES modules and use the stable global Host SDK:

```javascript
const { React } = window.QwenPaw.host;

window.QwenPaw.slot.fill(
  "my-plugin",
  "ui.task.inspector",
  (_defaultContent, context) =>
    React.createElement("span", null, context.task.objective),
  { id: "my-plugin.inspector", order: 200 },
);
```

Task Slot contexts are read-only host data:

| Slot | Context fields |
|---|---|
| `ui.task.toolbar` | `task`, `tasks` |
| `ui.task.tab` | `task`, `run`, `plan`, `events`, `artifacts`, `evidence` |
| `ui.task.inspector` | `task`, `run`, `plan` |
| `ui.artifact.preview` | `task`, `artifact`, `evidence` |

Renderers written for the earlier one-argument contract remain valid because
the context is an optional second argument.

## 3. Validate locally

Run:

```bash
qwenpaw plugin validate examples/plugins/task-insights
qwenpaw plugin validate examples/plugins/chat-tool-provider
qwenpaw plugin validate examples/plugins/runtime-provider-kit
qwenpaw plugin validate examples/plugins/scheduler-provider
qwenpaw plugin validate examples/plugins/delivery-provider
```

Validation reports the exact manifest field, unsupported slot, and recovery
action. It does not require the QwenPaw server to be running.

The references cover one Chat Tool Provider, one Memory Provider, one typed
Driver Provider, one Planner, one Strategy, one native Runner, one Harness
Runner, one Scheduler, one Delivery Adapter, one sensor, one Artifact Renderer,
and four contextual UI slots.
The Scheduler example is a stateless `SchedulerProvider`: it binds its stable
identity to the `SchedulerPort` supplied by the Host. It does not import the
SQLite implementation or own a persistence path.

Driver Provider has typed tool, invocation selection and prompt contracts;
credential material remains Driver-owned and never enters the manifest. The
public approval Host uses the same Approval/Interaction infrastructure as
built-ins. Profile-level provider selection and typed provider configuration
are public and durable. `scheduler.provider` is a public Contribution Slot and
must implement `SchedulerProvider`; activation fails closed before publication
when the Provider, returned Port, manifest entrypoint, capability identity,
read-only catalog, or JSON `config_schema` is invalid. The earlier `scheduler`
Slot remains compatibility-only for existing bundles.

## 4. Install and update

Run `qwenpaw plugin install <plugin-directory>`. When the app is already
running, hot-compatible contributions are staged, health-checked, and
published together as a new registry generation. Existing runs retain their
old generation; new runs see the update. A failed health check leaves the
current generation unchanged.

Capability promotion uses the Host Slot Contract risk instead of a risk value
reported by plugin code. Low-risk UI-only candidates can be installed without
an extra confirmation. Medium- and high-risk candidates use an exact-candidate
conditional request. The first API request returns HTTP `428` with a
content-safe candidate hash and the Host-classified risk. The Console and CLI
repeat the same explicit install with
`X-QwenPaw-Authorize-Candidate: <candidate-hash>`. The hash binds the manifest,
Contribution contract, and bounded plugin source tree. QwenPaw validates it
under the per-plugin lifecycle lock before copying files, installing
dependencies, or executing plugin code, then verifies the copied tree again.
A changed URL, ZIP, directory, or stale confirmation fails closed. Startup
restoration does not require an operator to be online and is recorded as
`not_applicable`; an explicit install records
`promotion.operator-authorized=passed` in the Evidence Bundle.

The same policy runs at the provider-neutral Generation Registry boundary, so
system and plugin candidates cannot diverge. Every evaluation records
`promotion.risk.low`, `.medium`, or `.high` Evidence. Plugins can declare a
Slot, but cannot lower its Host-owned risk or mint an authorization grant.

Publication also creates an immutable Promotion Evidence Bundle before the
new generation becomes visible. The built-in Gate records separate schema,
implementation, and health checks bound to the candidate hash and exact
capability IDs. If that bundle cannot be persisted and read back, installation
fails closed. Plugin code does not write these host-owned records. Use the
read-only `/api/plugins/capability-promotions` and
`/api/plugins/capability-promotion-evidence?candidate_id=<uuid>` endpoints to
trace a release; neither endpoint exposes implementation objects, config
values, or credentials.

The evidence response also returns a standard `artifacts` collection. Each
reference is derived from the authoritative Bundle rather than copied into a
second store. Download its canonical, timestamp-independent JSON from
`/api/plugins/capability-promotion-evidence/<bundle-id>/artifact`; the response
bytes must match the reference's `content_hash` and `size_bytes`. Metadata is
limited to candidate, bundle, and evaluator identity, so plugin code and host
paths never become part of the public artifact contract.

Use the same install command after changing the version to update the plugin.
Replacement is transactional: the old capability bundle remains resolvable
until the new bundle passes import, contract, identity, schema, and health
checks. A successful replacement publishes one generation; a failed
replacement restores the previous files, manifest, registrations, and
capabilities. Update does not execute permanent uninstall hooks.

## 5. Uninstall

Run `qwenpaw plugin uninstall task-insights`. A running app removes hot
contributions without a service restart. Runs already holding a generation
lease finish against their pinned snapshot.

Uninstall uses the same append-only promotion journal. QwenPaw commits the
provider deactivation before dismantling plugin-owned host state; a journal or
evidence failure leaves the loaded plugin intact instead of producing a torn
half-unload.

Capability-bearing plugins also use an exact-release authorization fence.
The first permanent delete returns HTTP `428` with a content-safe release hash
and capability IDs. An authorized Console or CLI client repeats the request
with `X-QwenPaw-Confirm-Release: <release-hash>`. QwenPaw validates that hash
under the same per-plugin lifecycle lock before running uninstall hooks,
removing registrations, or deleting files. If an update won the race, the
stale confirmation is rejected and the new release remains loaded. Internal
hot replacement does not use this permanent-delete path.

## 6. Clean-room acceptance walkthrough

Use this sequence from a fresh checkout before publishing a plugin:

1. Copy `examples/plugins/task-insights` to a new directory. Change the
   manifest `id`, Python package name, and entrypoints. A runner capability ID
   is always `<plugin-id>.<contribution-id>`; its `runner_id` must match.
2. Import only `qwenpaw.plugins.sdk` from backend contribution modules. Do not
   import `qwenpaw.app`, loader internals, FastAPI routers, or the SQLite store.
3. Run `qwenpaw plugin validate <new-plugin-directory>`. Success must report
   the plugin ID and exact contribution count.
4. With QwenPaw running, run `qwenpaw plugin install
   <new-plugin-directory>`. The command must report `hot reload`; do not
   restart the backend or frontend.
5. Create a Task with `runner_id` set to the runner capability ID and start it
   in the Workbench. Verify timeline events, conversation, approvals, and the
   artifact/evidence cards from `GET /api/tasks/{task_id}/projection`.
6. Increment the plugin version, change its result text, and run the same
   install command. A new Task must use the new registry generation while an
   already-running Task retains its original generation lease.
7. Run `qwenpaw plugin uninstall <plugin-id>`. New resolution must no longer
   expose its contributions; already-pinned work may finish safely.

The repository keeps this path executable with focused checks:

```bash
python -m pytest -q \
  tests/integration/test_lite_plugin_hot_activation.py \
  tests/unit/plugins/test_plugin_sdk_example.py \
  tests/unit/plugins/test_registry_generations.py \
  tests/unit/plugins/test_plugin_contributions.py \
  tests/unit/tasks/test_runner.py
```

These checks cover public-SDK-only imports, validation-compatible manifest
shape, native and Harness Runner dispatch, Memory session isolation,
Artifact/Evidence emission, atomic hot activation, rollback, unload, and
generation pinning.
