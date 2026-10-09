# -*- coding: utf-8 -*-
"""Framework-independent ports implemented by QwenPaw editions."""

from __future__ import annotations

from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import JsonValue

from .events import (
    ExecutionCommit,
    ExecutionEvent,
    TaskProjectionSnapshot,
)
from .releases import (
    CapabilityPromotionCandidate,
    CapabilityPromotionAssessment,
    CapabilityPromotionEvidence,
    CapabilityPromotionEvidenceBundle,
    CapabilityPromotionEvent,
    CapabilityReleaseTag,
)
from .models import (
    ActionApprovalLink,
    ActionRecord,
    ActionRequest,
    ActionResult,
    ActionRetryDecision,
    ActionRetryInputCheckpoint,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    ArtifactRenderDisposition,
    ArtifactRenderRequest,
    ArtifactRenderResult,
    ArtifactRef,
    CapabilityDescriptor,
    CommandDefinition,
    CommandRequest,
    CommandResult,
    ContextManifest,
    CostAccountingMode,
    DriverApprovalRequest,
    DriverToolDefinition,
    EnvironmentContract,
    EnvironmentRecord,
    EnvironmentResolution,
    HookDefinition,
    HookOutcome,
    StopGateDecision,
    StopGateDefinition,
    StopGateInput,
    ExecutionCheckpoint,
    IdempotencyRecord,
    JsonObject,
    ModelCallAttempt,
    ModelCallRecord,
    ModelCallResult,
    Plan,
    PlanStep,
    PromptFragment,
    Proposal,
    RouteDecision,
    SensorContext,
    Run,
    RunnerPreflightRequest,
    RunnerPreflightResult,
    SideEffectRecord,
    RunnerSignal,
    RuntimeContext,
    Task,
    TaskOrder,
    ToolDefinition,
    ToolSelection,
    VerificationRecord,
)
from .invocation import InvocationScope
from .invocation_control import (
    ControlCommand,
    ControlRecord,
    ControlReceipt,
    QueueProjection,
    TurnSubmission,
    TurnSubmissionRequest,
)
from .compaction import CompactionRecord
from .interactions import (
    InteractionOption,
    InteractionRecord,
    InteractionRequest,
    InteractionResolution,
    InteractionResponse,
    UserInputReason,
)
from .memory import MemoryStateScope, MemoryStateSnapshot
from .waits import (
    ActionRetryContinuation,
    ModelResourceWait,
    ModelStepContinuation,
)
from .scheduling import (
    ScheduleDefinition,
    ScheduleFire,
    ScheduleLease,
    ScheduleTriggerCursor,
)
from .conversations import (
    ConversationForkCommand,
    ConversationForkResult,
)
from .delivery import DeliveryAttempt, DeliveryReceipt, DeliveryRequest
from .inbox import InboxItem
from .operational import OperationalEvent
from .observations import ObservationPage, RuntimeObservation
from .outcomes import ConversationOutcome, ConversationOutcomeRequest
from .goals import GoalExecution
from .mode_state import AgentModeState
from .waits import ConversationContinuation, WaitCondition
from .artifacts import (
    ConversationArtifactRecord,
    ConversationTaskResultRecords,
)
from .capability_locks import CapabilityLockManifest


@runtime_checkable
class EnvironmentResolver(Protocol):
    """Resolve one backend-neutral contract against a concrete edition."""

    async def resolve(
        self,
        contract: EnvironmentContract,
        *,
        invocation_id: UUID,
        workspace_dir: str,
    ) -> EnvironmentResolution:
        """Return explicit satisfied or unsatisfied environment evidence."""


@runtime_checkable
class EnvironmentStore(Protocol):
    """Persist immutable environment evidence outside execution events."""

    async def record(
        self,
        record: EnvironmentRecord,
        *,
        conversation_id: str | None,
    ) -> None:
        """Persist one contract and its concrete resolution exactly once."""


@runtime_checkable
class InvocationControlPort(Protocol):
    """Server-authoritative queue and live invocation control boundary."""

    async def submit(
        self,
        submission: TurnSubmissionRequest,
        *,
        expected_revision: int | None = None,
    ) -> ControlReceipt:
        """Append one turn, optionally requiring a queue revision."""

    async def control(self, command: ControlCommand) -> ControlReceipt:
        """Apply or durably accept one runtime control command."""

    async def read_queue(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> QueueProjection:
        """Return one consistent queue and active-invocation projection."""

    async def get_submission(
        self,
        submission_id: UUID,
    ) -> TurnSubmission | None:
        """Return one durable submission by its server identity."""

    async def list_dispatchable(
        self,
        *,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        """Return the next queued turn for each idle conversation."""

    async def recover_orphaned_submissions(
        self,
        *,
        agent_id: str,
    ) -> tuple[TurnSubmission, ...]:
        """Terminate active turns whose Runtime generation is gone."""

    def watch(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        after_revision: int,
    ) -> AsyncIterator[QueueProjection]:
        """Replay and follow projections newer than one revision."""


@runtime_checkable
class ControlHistoryPort(Protocol):
    """Optional read-only history surface over runtime control commands."""

    async def list_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> Sequence[ControlRecord]:
        """List newest command requests with their latest receipts."""


@runtime_checkable
class SubmissionHistoryPort(Protocol):
    """Read-only history of durable Conversation submissions."""

    async def list_submissions_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> Sequence[TurnSubmission]:
        """List newest submissions, including terminal records."""


@runtime_checkable
class ConversationArtifactHistoryPort(Protocol):
    """Read-only registry of Conversation-owned Artifact/Evidence pairs."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[ConversationArtifactRecord]:
        """List newest records without exposing Artifact content."""


@runtime_checkable
class CompactionStore(Protocol):
    """Durable owner-only history for material context compactions."""

    async def append(self, record: CompactionRecord) -> None:
        """Persist one immutable terminal compaction record."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[CompactionRecord]:
        """List newest records for one ChatSpec identity."""


@runtime_checkable
class ConversationForkPort(Protocol):
    """ChatSpec-owned, storage-neutral Conversation branching boundary."""

    async def fork(
        self,
        command: ConversationForkCommand,
    ) -> ConversationForkResult:
        """Create or replay one child at a completed message boundary."""

    async def lineage(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> tuple[str, ...]:
        """Return child-to-root Conversation identities."""


@runtime_checkable
class SchedulerPort(Protocol):
    """Durable schedule catalog and fire-lease ownership boundary."""

    async def upsert(
        self,
        definition: ScheduleDefinition,
    ) -> ScheduleDefinition:
        """Create or replace one validated schedule definition."""

    async def remove(self, *, agent_id: str, schedule_id: str) -> bool:
        """Remove one schedule, returning whether it existed."""

    async def list_definitions(
        self,
        *,
        agent_id: str,
    ) -> tuple[ScheduleDefinition, ...]:
        """Return one Agent's definitions in stable schedule-ID order."""

    async def claim(
        self,
        fire: ScheduleFire,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> ScheduleLease:
        """Claim one idempotent fire or return its existing lease."""

    async def renew(
        self,
        lease_id: UUID,
        *,
        owner_id: str,
        expected_revision: int,
        lease_seconds: float,
    ) -> ScheduleLease:
        """Extend a live owned lease using optimistic concurrency."""

    async def complete(
        self,
        lease_id: UUID,
        *,
        owner_id: str,
        expected_revision: int,
        task_id: UUID | None = None,
        run_id: UUID | None = None,
        completion_ref: str | None = None,
    ) -> ScheduleLease:
        """Bind one fire to exactly one Task or non-Task completion."""

    async def fail(
        self,
        lease_id: UUID,
        *,
        owner_id: str,
        expected_revision: int,
        error_code: str,
        retry_at: datetime | None = None,
    ) -> ScheduleLease:
        """Record a bounded failure without rewriting Task facts."""

    async def recover_expired(
        self,
        *,
        agent_id: str,
        now: datetime,
        schedule_id: str | None = None,
    ) -> tuple[ScheduleLease, ...]:
        """Fail scoped expired claims and return terminal records."""


@runtime_checkable
class ScheduleTriggerCursorStore(Protocol):
    """Host-owned durable progress for framework-neutral trigger workers."""

    async def reconcile_cursor(
        self,
        cursor: ScheduleTriggerCursor,
    ) -> ScheduleTriggerCursor:
        """Create progress or reset it when the definition hash changes."""

    async def get_cursor(
        self,
        *,
        agent_id: str,
        schedule_id: str,
    ) -> ScheduleTriggerCursor | None:
        """Return one exact cursor or none when it is not registered."""

    async def list_due_cursors(
        self,
        *,
        agent_id: str,
        now: datetime,
        limit: int = 100,
    ) -> tuple[ScheduleTriggerCursor, ...]:
        """Return due progress ordered by occurrence and Schedule ID."""

    async def advance_cursor(
        self,
        *,
        agent_id: str,
        schedule_id: str,
        definition_hash: str,
        expected_revision: int,
        scheduled_for: datetime,
        next_fire_at: datetime | None,
    ) -> ScheduleTriggerCursor:
        """Commit one handled occurrence using definition and revision CAS."""

    async def defer_cursor(
        self,
        *,
        agent_id: str,
        schedule_id: str,
        definition_hash: str,
        expected_revision: int,
        scheduled_for: datetime,
        retry_not_before: datetime,
    ) -> ScheduleTriggerCursor:
        """Persist retry backoff for one uncommitted occurrence by CAS."""

    async def remove_cursor(
        self,
        *,
        agent_id: str,
        schedule_id: str,
        expected_definition_hash: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Remove progress, optionally only when its identity still matches."""


@runtime_checkable
class SchedulerHost(Protocol):
    """Host-owned durable Scheduler state exposed to one Provider."""

    def scheduler_store(self) -> SchedulerPort:
        """Return the admitted durable store without exposing its path."""


@runtime_checkable
class SchedulerProvider(Protocol):
    """Open a Scheduler against Host-owned persistence."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this Provider."""

    async def open(self, host: SchedulerHost) -> SchedulerPort:
        """Bind scheduling behavior to one Host-owned Store."""


@runtime_checkable
class DeliveryAdapter(Protocol):
    """External projection boundary for already committed domain facts."""

    @property
    def adapter_id(self) -> str:
        """Return the stable capability ID of this delivery adapter."""

    def supports(self, request: DeliveryRequest) -> bool:
        """Return whether this adapter owns the destination and fact kind."""

    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        """Attempt delivery without rewriting the request's source fact."""


@runtime_checkable
class DeliveryProjectionPort(Protocol):
    """Durable request and attempt ownership boundary."""

    async def claim(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
        owner_id: str,
        lease_seconds: float,
    ) -> DeliveryAttempt:
        """Claim one explicit attempt or replay its existing record."""

    async def renew(
        self,
        delivery_id: UUID,
        *,
        attempt: int,
        owner_id: str,
        expected_revision: int,
        lease_seconds: float,
    ) -> DeliveryAttempt:
        """Extend one live attempt through owner/revision CAS."""

    async def settle(
        self,
        receipt: DeliveryReceipt,
        *,
        owner_id: str,
        expected_revision: int,
    ) -> DeliveryAttempt:
        """Commit the terminal receipt for one owned attempt."""

    async def get_request(
        self,
        delivery_id: UUID,
    ) -> DeliveryRequest | None:
        """Return the immutable request projection when it exists."""

    async def list_attempts(
        self,
        delivery_id: UUID,
    ) -> tuple[DeliveryAttempt, ...]:
        """Return attempts in ascending explicit attempt order."""

    async def recover_expired(
        self,
        *,
        agent_id: str,
        now: datetime,
    ) -> tuple[DeliveryAttempt, ...]:
        """Fail one Agent's expired attempts without retrying them."""


@runtime_checkable
class InboxProjectionPort(Protocol):
    """Read-state projection over immutable Delivery source facts."""

    async def project(
        self,
        request: DeliveryRequest,
        receipt: DeliveryReceipt,
    ) -> InboxItem:
        """Create or advance one item from a terminal receipt."""

    async def get(self, item_id: UUID) -> InboxItem | None:
        """Return one item without consulting or changing source facts."""

    async def list_items(
        self,
        *,
        agent_id: str,
        unread_only: bool = False,
        limit: int = 50,
        include_handled: bool = False,
    ) -> tuple[InboxItem, ...]:
        """Return one Agent's newest projections."""

    async def mark_read(
        self,
        item_id: UUID,
        *,
        agent_id: str,
        expected_revision: int,
    ) -> InboxItem:
        """Mutate only projection-local read state through CAS."""

    async def mark_handled(
        self,
        item_id: UUID,
        *,
        agent_id: str,
        expected_revision: int,
    ) -> InboxItem:
        """Mutate only projection-local handled state through CAS."""


@runtime_checkable
class OperationalEventPort(Protocol):
    """Durable source boundary for non-Task operational facts."""

    async def commit(self, event: OperationalEvent) -> OperationalEvent:
        """Commit one immutable event or replay the identical fact."""

    async def get(self, event_id: UUID) -> OperationalEvent | None:
        """Return one event by stable identity."""

    async def list_events(
        self,
        *,
        agent_id: str,
        producer_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationalEvent, ...]:
        """Return newest events within one Agent ownership boundary."""


@runtime_checkable
class InteractionPort(Protocol):
    """Durable boundary shared by approval, user input, and suggestions."""

    async def open(
        self,
        request: InteractionRequest,
    ) -> InteractionRequest:
        """Persist one interaction before it is delivered."""

    async def resolve(
        self,
        response: InteractionResponse,
    ) -> InteractionResolution:
        """Validate and durably resolve one open interaction."""

    async def list_open(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> Sequence[InteractionRequest]:
        """List all open interactions for one ChatSpec in creation order."""

    async def cancel_invocation(
        self,
        invocation_id: UUID,
        *,
        detail: str,
        include_non_blocking: bool = True,
        preserve_conversation_continuations: bool = False,
    ) -> Sequence[InteractionResolution]:
        """Cancel invocation interactions, optionally retaining suggestions."""

    async def wait(
        self,
        interaction_id: UUID,
        *,
        timeout_seconds: float | None = None,
    ) -> InteractionResolution:
        """Wait for one blocking interaction's authoritative terminal state."""


@runtime_checkable
class InteractionHistoryPort(Protocol):
    """Optional read-only history surface over runtime interactions."""

    async def list_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        limit: int = 100,
    ) -> Sequence[InteractionRecord]:
        """List newest interaction requests with terminal resolutions."""


@runtime_checkable
class WaitConditionProjectionPort(Protocol):
    """Read-only normalized view over authoritative wait sources."""

    async def list_wait_conditions(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        include_terminal: bool = False,
        limit: int = 100,
    ) -> Sequence[WaitCondition]:
        """List content-free wait conditions for one ChatSpec."""


@runtime_checkable
class ConversationContinuationPort(Protocol):
    """Durable outbox for user-input conversation continuations."""

    async def get_request(
        self,
        interaction_id: UUID,
    ) -> InteractionRequest | None:
        """Load the immutable interaction source for dispatch."""

    async def get_resolution(
        self,
        interaction_id: UUID,
    ) -> InteractionResolution | None:
        """Load the terminal response used to materialize the next turn."""

    async def list_ready_continuations(
        self,
        *,
        agent_id: str,
    ) -> Sequence[ConversationContinuation]:
        """List unresolved outbox entries owned by one Agent."""

    async def mark_continuation_dispatched(
        self,
        interaction_id: UUID,
        submission_id: UUID,
    ) -> ConversationContinuation:
        """Bind one outbox entry to its durable Submission exactly once."""


@runtime_checkable
class RuntimeInteractionProducer(Protocol):
    """Invocation-bound producer surface shared by built-ins and plugins."""

    @property
    def has_deferred_user_input(self) -> bool:
        """Return whether execution awaits a future conversation turn."""

    async def ask_user(
        self,
        *,
        reason: UserInputReason,
        title: str,
        prompt: str,
        options: tuple[InteractionOption, ...] = (),
        response_schema: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        source_id: UUID | None = None,
        timeout_seconds: float | None = None,
        expires_at: datetime | None = None,
    ) -> InteractionResolution:
        """Persist and await input only within the current Invocation."""

    async def defer_user_input(
        self,
        *,
        reason: UserInputReason,
        title: str,
        prompt: str,
        options: tuple[InteractionOption, ...] = (),
        response_schema: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        source_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> InteractionRequest:
        """Persist input for durable continuation in a new Invocation."""

    async def suggest(
        self,
        *,
        title: str,
        prompt: str,
        options: tuple[InteractionOption, ...] = (),
        metadata: dict[str, object] | None = None,
        source_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> InteractionRequest:
        """Persist one non-blocking suggestion and return immediately."""


@runtime_checkable
class TaskStore(Protocol):
    """Atomic persistence boundary for task projections and events."""

    async def commit(self, commit: ExecutionCommit) -> bool:
        """Persist projections and events in one atomic transaction."""

    async def get_task(self, task_id: UUID) -> Task | None:
        """Return a task by ID."""

    async def list_tasks(
        self,
        *,
        cursor: str | None,
        limit: int,
    ) -> Sequence[Task]:
        """Return a stable cursor-based task page."""

    async def get_run(self, run_id: UUID) -> Run | None:
        """Return one run by ID."""

    async def list_runs(self, task_id: UUID) -> Sequence[Run]:
        """Return all attempts for a task in ascending attempt order."""

    async def latest_plan(self, task_id: UUID) -> Plan | None:
        """Return the newest immutable plan revision for a task."""

    async def get_approval(
        self,
        approval_id: UUID,
    ) -> tuple[ApprovalRequest, ApprovalDecision | None] | None:
        """Return an approval request and its optional decision."""

    async def list_approvals(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
        status: ApprovalStatus | None = None,
    ) -> Sequence[tuple[ApprovalRequest, ApprovalDecision | None]]:
        """List approval projections for one task in creation order."""

    async def get_idempotency(
        self,
        operation: str,
        key: str,
    ) -> IdempotencyRecord | None:
        """Return a previously committed mutation response."""

    async def get_side_effect(
        self,
        record_id: UUID,
    ) -> SideEffectRecord | None:
        """Return one side-effect record by durable identity."""

    async def get_side_effect_by_key(
        self,
        task_id: UUID,
        idempotency_key: str,
    ) -> SideEffectRecord | None:
        """Return one side-effect record by task-scoped idempotency key."""

    async def list_side_effects(
        self,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
    ) -> Sequence[SideEffectRecord]:
        """List side effects for one task in creation order."""

    async def read_projection(
        self,
        task_id: UUID,
        *,
        event_limit: int = 1000,
    ) -> TaskProjectionSnapshot:
        """Read all Task Workbench projections in one snapshot."""


@runtime_checkable
class ExecutionLedger(Protocol):
    """Append-only source of execution events."""

    async def append(self, event: ExecutionEvent) -> bool:
        """Append an event, returning false for an idempotent duplicate."""

    async def list_events(
        self,
        task_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> Sequence[ExecutionEvent]:
        """Read a task timeline in ascending sequence order."""

    async def latest_sequence(self, task_id: UUID) -> int:
        """Return the last committed sequence for a task."""


@runtime_checkable
class AgentFactory(Protocol):
    """Build one Agent session for an immutable invocation scope."""

    @property
    def factory_id(self) -> str:
        """Return the stable capability ID of this factory."""

    async def build(self, context: Any, app_services: Any) -> Any:
        """Build an Agent using invocation context and host services."""


@runtime_checkable
class TaskRunner(Protocol):
    """Execution backend selected for one task order."""

    @property
    def runner_id(self) -> str:
        """Return the stable capability ID of this runner."""

    def execute(
        self,
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        """Yield unsequenced signals for the runtime to persist."""


@runtime_checkable
class CostAwareTaskRunner(TaskRunner, Protocol):
    """Optional Runner extension declaring trusted cost accounting."""

    @property
    def cost_accounting(self) -> CostAccountingMode:
        """Return whether cost is reported, known zero, or unknown."""


@runtime_checkable
class ContextualTaskRunner(TaskRunner, Protocol):
    """Runner using the typed context while legacy runners migrate."""

    def execute_context(
        self,
        order: TaskOrder,
        run: Run,
        context: RuntimeContext,
    ) -> AsyncIterator[RunnerSignal]:
        """Yield signals with one immutable runtime context."""


@runtime_checkable
class PreflightTaskRunner(TaskRunner, Protocol):
    """Runner exposing a side-effect-free staged contract probe."""

    async def preflight(
        self,
        request: RunnerPreflightRequest,
    ) -> RunnerPreflightResult:
        """Describe execution claims without starting task execution."""


@runtime_checkable
class TaskPlanner(Protocol):
    """Create deterministic plan steps for one task order."""

    @property
    def planner_id(self) -> str:
        """Return the stable planner capability ID."""

    async def plan(self, order: TaskOrder) -> Sequence[PlanStep]:
        """Return ordered plan steps for the task."""


@runtime_checkable
class RuntimeStrategy(Protocol):
    """Prepare bounded runtime parameters before runner dispatch."""

    @property
    def strategy_id(self) -> str:
        """Return the stable capability ID of this strategy."""

    async def prepare(
        self,
        context: RuntimeContext,
        order: TaskOrder,
    ) -> JsonObject:
        """Return public parameters for the selected runtime strategy."""


@runtime_checkable
class ProposalSensor(Protocol):
    """Bounded cognition source that may propose but never execute work."""

    @property
    def sensor_id(self) -> str:
        """Return the stable capability ID of this sensor."""

    async def propose(self) -> Sequence[Proposal]:
        """Return a bounded batch of non-executable proposals."""


@runtime_checkable
class ContextualProposalSensor(ProposalSensor, Protocol):
    """Sensor that receives immutable Agent and generation identity."""

    async def propose_context(
        self,
        context: SensorContext,
    ) -> Sequence[Proposal]:
        """Return proposals for one host-owned polling context."""


@runtime_checkable
class ApprovalPort(Protocol):
    """Delivery and resolution boundary for human approvals."""

    async def request(
        self,
        approval: ApprovalRequest,
    ) -> ApprovalDecision:
        """Deliver an approval request and wait for its decision."""


@runtime_checkable
class ArtifactStore(Protocol):
    """Storage boundary for payloads kept outside the ledger."""

    async def put(
        self,
        *,
        kind: str,
        media_type: str,
        content: bytes,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ArtifactRef:
        """Persist bytes and return a content-addressed reference."""

    async def read(self, artifact: ArtifactRef) -> bytes:
        """Read an artifact after validating its reference."""


@runtime_checkable
class ArtifactRenderer(Protocol):
    """Render safe views without changing durable artifact bytes."""

    @property
    def renderer_id(self) -> str:
        """Return the capability ID that owns this renderer."""

    @property
    def priority(self) -> int:
        """Return the stable routing priority; lower values run first."""

    def supports(
        self,
        artifact: ArtifactRef,
        disposition: ArtifactRenderDisposition,
    ) -> bool:
        """Return whether this renderer can handle the requested view."""

    async def render(
        self,
        request: ArtifactRenderRequest,
    ) -> ArtifactRenderResult:
        """Return one bounded view derived from verified source bytes."""


@runtime_checkable
class CapabilityLease(Protocol):
    """Pinned immutable view of one registry generation."""

    @property
    def generation(self) -> int:
        """Return the pinned generation number."""

    @property
    def registry_epoch_id(self) -> UUID:
        """Return the process epoch that scopes the generation."""

    def resolve(self, capability_id: str) -> CapabilityDescriptor | None:
        """Resolve one capability from the pinned generation."""

    def descriptors(
        self,
        slot: str | None = None,
    ) -> Sequence[CapabilityDescriptor]:
        """List pinned descriptors, optionally filtered by slot."""

    async def close(self) -> None:
        """Release the generation lease."""


@runtime_checkable
class ExecutableCapabilityLease(CapabilityLease, Protocol):
    """Pinned application lease exposing staged runtime implementations."""

    def implementation(self, capability_id: str) -> object | None:
        """Resolve an implementation from the same immutable snapshot."""


@runtime_checkable
class CapabilityResolver(Protocol):
    """Factory for immutable capability registry leases."""

    async def pin(
        self,
        generation: int | None = None,
    ) -> CapabilityLease:
        """Pin the current or one retained registry generation."""


@runtime_checkable
class AgentModeHost(Protocol):
    """Minimal invocation Host exposed to third-party Mode Providers."""

    def config_snapshot(self) -> JsonObject:
        """Return validated non-secret configuration for this provider."""

    async def read_state(
        self,
        state_key: str = "default",
    ) -> AgentModeState | None:
        """Read state owned by this provider and ChatSpec."""

    async def write_state(
        self,
        value: JsonObject,
        *,
        expected_revision: int,
        state_key: str = "default",
        state_schema_version: int = 1,
    ) -> AgentModeState:
        """CAS-write state without exposing the backing adapter."""

    async def clear_state(
        self,
        *,
        expected_revision: int,
        state_key: str = "default",
    ) -> AgentModeState:
        """CAS-clear one value while retaining its revision fence."""


@runtime_checkable
class AgentModeStateStore(Protocol):
    """Host-private persistence for namespaced Agent Mode state."""

    async def read(
        self,
        *,
        provider_id: str,
        agent_id: str,
        conversation_id: str,
        state_key: str,
    ) -> AgentModeState | None:
        """Read one exact provider-owned state value."""

    async def write(
        self,
        state: AgentModeState,
        *,
        expected_revision: int,
    ) -> AgentModeState:
        """Create or replace one exact observed state revision."""


@runtime_checkable
class AgentModeSession(Protocol):
    """Agent Mode behavior bound to one immutable invocation."""

    def active_mode_names(self) -> Sequence[str]:
        """Return names used to gate mode-owned tools and prompts."""

    async def start_turn(self) -> None:
        """Prepare mode-owned state after the agent is built."""

    async def reset_conversation(self) -> None:
        """Reset mode-owned state for the current conversation."""

    async def close(self) -> None:
        """Release invocation-scoped mode resources."""


@runtime_checkable
class AgentModeProvider(Protocol):
    """Open one task-independent Agent Mode session."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def open(
        self,
        scope: InvocationScope,
        host: AgentModeHost,
    ) -> AgentModeSession:
        """Bind mode lifecycle behavior to one immutable invocation."""


@runtime_checkable
class PromptHost(Protocol):
    """Invocation host for the existing Workspace prompt assembly."""

    async def build_workspace_prompt(self) -> str:
        """Return the prompt produced by current built-in contributors."""


@runtime_checkable
class PromptProvider(Protocol):
    """Return ordered system-prompt fragments for one invocation."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def list_fragments(
        self,
        scope: InvocationScope,
        host: PromptHost,
    ) -> Sequence[PromptFragment]:
        """Return bounded, provider-owned prompt fragments."""


@runtime_checkable
class CapabilityCredentialHandle(Protocol):
    """Capability-scoped access to one configured credential alias."""

    async def public_values(self) -> JsonObject:
        """Return non-secret credential values as a detached snapshot."""

    async def read_secret(self, name: str) -> str:
        """Resolve one declared secret field or fail closed."""


DriverCredentialHandle = CapabilityCredentialHandle


@runtime_checkable
class ToolHost(Protocol):
    """Minimal invocation Host exposed to third-party Tool Providers."""

    def config_snapshot(self) -> JsonObject:
        """Return validated non-secret configuration for this provider."""

    def credential(
        self,
        alias: str,
    ) -> CapabilityCredentialHandle | None:
        """Return only a credential alias bound to this provider."""

    def interaction_broker(self) -> RuntimeInteractionProducer | None:
        """Return this invocation's structured user-interaction producer."""


@runtime_checkable
class DriverHost(Protocol):
    """Minimal invocation Host exposed to third-party Driver Providers."""

    async def require_approval(
        self,
        request: DriverApprovalRequest,
    ) -> None:
        """Return only after approval; reject through the public error."""

    def config_snapshot(self) -> JsonObject:
        """Return the validated provider configuration for this invocation."""

    def credential(self, alias: str) -> DriverCredentialHandle | None:
        """Return only a credential alias bound to this provider."""


@runtime_checkable
class DriverSession(Protocol):
    """Driver capabilities bound to one immutable invocation."""

    @property
    def provider_id(self) -> str:
        """Return the provider that owns every session contribution."""

    def list_tools(self) -> Sequence[DriverToolDefinition]:
        """Return framework-neutral, policy-owned Driver tools."""

    def prompt_fragments(self) -> Sequence[PromptFragment]:
        """Return bounded, provider-owned prompt fragments."""

    async def close(self) -> None:
        """Release invocation-scoped Driver resources."""


@runtime_checkable
class DriverProvider(Protocol):
    """Open one task-independent Driver session."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def open(
        self,
        scope: InvocationScope,
        host: DriverHost,
    ) -> DriverSession:
        """Bind Driver behavior to one immutable invocation."""


@runtime_checkable
class CommandHost(Protocol):
    """Invocation host for the existing Workspace command registry."""

    def list_commands(self) -> Sequence[CommandDefinition]:
        """Return the fixed static catalog for this invocation."""

    async def dispatch(self, request: CommandRequest) -> CommandResult:
        """Dispatch a catalog command through the compatibility boundary."""

    async def fallback(self, request: CommandRequest) -> CommandResult:
        """Try the bounded dynamic fallback after catalog lookup fails."""


@runtime_checkable
class CommandSession(Protocol):
    """Slash-command behavior bound to one immutable invocation."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of the owning provider."""

    @property
    def allows_dynamic_fallback(self) -> bool:
        """Return whether this session may resolve uncatalogued names."""

    def list_commands(self) -> Sequence[CommandDefinition]:
        """Return the provider's fixed command catalog."""

    async def dispatch(self, request: CommandRequest) -> CommandResult:
        """Execute one catalog command."""

    async def fallback(self, request: CommandRequest) -> CommandResult:
        """Try one uncatalogued command when explicitly allowed."""

    async def close(self) -> None:
        """Release invocation-scoped command resources."""


@runtime_checkable
class CommandProvider(Protocol):
    """Open one task-independent command session."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def open(
        self,
        scope: InvocationScope,
        host: CommandHost,
    ) -> CommandSession:
        """Bind command behavior to one immutable invocation."""


@runtime_checkable
class HookHost(Protocol):
    """Invocation host for the existing Workspace lifecycle hooks."""

    def list_hooks(self) -> Sequence[HookDefinition]:
        """Return the fixed hook catalog for this invocation."""

    async def run_hook(self, hook_id: str) -> HookOutcome:
        """Execute one compatibility hook by stable identity."""

    def inject_context(
        self,
        content: str,
        *,
        priority: int = 100,
        source: str = "",
    ) -> None:
        """Add bounded system context for the current Agent turn."""


@runtime_checkable
class HookSession(Protocol):
    """Lifecycle hooks bound to one immutable invocation."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of the owning provider."""

    def list_hooks(self) -> Sequence[HookDefinition]:
        """Return the provider's fixed hook catalog."""

    async def run_hook(self, hook_id: str) -> HookOutcome:
        """Execute one hook from this session's catalog."""

    async def close(self) -> None:
        """Release invocation-scoped hook resources."""


@runtime_checkable
class HookProvider(Protocol):
    """Open one task-independent lifecycle hook session."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def open(
        self,
        scope: InvocationScope,
        host: HookHost,
    ) -> HookSession:
        """Bind lifecycle behavior to one immutable invocation."""


@runtime_checkable
class StopGateHost(Protocol):
    """Invocation host for existing Workspace loop stop handlers."""

    def list_gates(self) -> Sequence[StopGateDefinition]:
        """Return the fixed stop-gate catalog for this invocation."""

    def is_active(self, gate_id: str) -> bool:
        """Return whether one scoped compatibility gate is active."""

    async def evaluate(
        self,
        gate_id: str,
        gate_input: StopGateInput,
    ) -> StopGateDecision:
        """Evaluate one compatibility stop gate."""


@runtime_checkable
class StopGateSession(Protocol):
    """Loop stop gates bound to one immutable invocation."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of the owning provider."""

    def list_gates(self) -> Sequence[StopGateDefinition]:
        """Return the provider's fixed stop-gate catalog."""

    def is_active(self, gate_id: str) -> bool:
        """Return whether one scoped gate is active."""

    async def evaluate(
        self,
        gate_id: str,
        gate_input: StopGateInput,
    ) -> StopGateDecision:
        """Evaluate one gate from this session's catalog."""

    async def start_turn(self) -> None:
        """Prepare provider-owned state for one user turn."""

    async def reset_conversation(self) -> None:
        """Clear provider-owned state for the current conversation."""

    async def close(self) -> None:
        """Release invocation-scoped stop-gate resources."""


@runtime_checkable
class StopGateProvider(Protocol):
    """Open one task-independent loop stop-gate session."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def open(
        self,
        scope: InvocationScope,
        host: StopGateHost,
    ) -> StopGateSession:
        """Bind loop stop behavior to one immutable invocation."""


@runtime_checkable
class ToolProvider(Protocol):
    """Task-independent source of tools for one invocation."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def list_tools(
        self,
        scope: InvocationScope,
        selection: ToolSelection,
        host: ToolHost,
    ) -> Sequence[ToolDefinition | Callable[..., object]]:
        """Return tools without depending on a concrete Agent runtime."""


@runtime_checkable
class MemoryStateStore(Protocol):
    """Revisioned provider state within one pre-authorized namespace."""

    async def read(self, key: str) -> MemoryStateSnapshot | None:
        """Return the current value or ``None`` when it does not exist."""

    async def write(
        self,
        key: str,
        value: JsonValue,
        *,
        expected_revision: int,
    ) -> MemoryStateSnapshot:
        """Create at revision zero or replace one observed revision."""

    async def delete(self, key: str, *, expected_revision: int) -> None:
        """Delete exactly one observed revision."""


@runtime_checkable
class MemoryHost(Protocol):
    """Invocation-scoped host services available to memory providers."""

    def config_snapshot(self) -> JsonObject:
        """Return validated non-secret configuration for this provider."""

    def state(self, scope: MemoryStateScope) -> MemoryStateStore:
        """Return state already scoped to this provider and owner."""


@runtime_checkable
class MemorySession(Protocol):
    """Memory capabilities bound to one immutable invocation."""

    def get_prompt(self) -> str:
        """Return bounded memory guidance for the system prompt."""

    def list_tools(
        self,
    ) -> Sequence[ToolDefinition | Callable[..., object]]:
        """Return memory tools exposed for this invocation."""

    async def close(self) -> None:
        """Release invocation-scoped memory resources."""


@runtime_checkable
class MemoryProvider(Protocol):
    """Open one task-independent memory session from a pinned provider."""

    @property
    def provider_id(self) -> str:
        """Return the stable capability ID of this provider."""

    async def open(
        self,
        scope: InvocationScope,
        host: MemoryHost,
    ) -> MemorySession:
        """Bind memory behavior to one immutable invocation."""


@runtime_checkable
class CheckpointStore(Protocol):
    """Persistence boundary for safe execution checkpoints."""

    async def save_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
    ) -> None:
        """Persist a checkpoint."""

    async def latest_resumable(
        self,
        task_id: UUID,
    ) -> ExecutionCheckpoint | None:
        """Return the newest checkpoint marked safe to resume."""


@runtime_checkable
class ContextManifestStore(Protocol):
    """Durable audit boundary for actual provider model-call inputs."""

    async def append(self, manifest: ContextManifest) -> None:
        """Persist one immutable manifest before the provider call."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[ContextManifest]:
        """Return newest manifests for one ChatSpec identity."""


@runtime_checkable
class CapabilityLockStore(Protocol):
    """Durable boundary for selected immutable capability releases."""

    async def append(self, manifest: CapabilityLockManifest) -> None:
        """Persist one immutable Invocation lock before execution."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[CapabilityLockManifest]:
        """Return newest locks for one ChatSpec identity."""


@runtime_checkable
class CapabilityPromotionJournal(Protocol):
    """Append-only evidence boundary for registry promotion phases."""

    async def append(self, event: CapabilityPromotionEvent) -> None:
        """Persist one immutable operation phase exactly once."""

    async def list_events(
        self,
        *,
        provider_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[CapabilityPromotionEvent]:
        """Return newest promotion phases with optional provider filter."""


@runtime_checkable
class CapabilityPromotionEvidenceStore(Protocol):
    """Immutable evidence bundles supporting promotion evaluations."""

    async def append(
        self,
        bundle: CapabilityPromotionEvidenceBundle,
    ) -> None:
        """Persist one content-safe bundle exactly once."""

    async def get(
        self,
        bundle_id: UUID,
    ) -> CapabilityPromotionEvidenceBundle | None:
        """Return one bundle by immutable identity."""

    async def list_for_candidate(
        self,
        candidate_id: UUID,
        *,
        limit: int = 100,
    ) -> Sequence[CapabilityPromotionEvidenceBundle]:
        """Return newest bundles for one candidate."""


@runtime_checkable
class CapabilityPromotionGate(Protocol):
    """Host policy boundary that decides candidate publication."""

    async def evaluate(
        self,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
        scenario_evidence: Sequence[CapabilityPromotionEvidence] = (),
    ) -> CapabilityPromotionAssessment:
        """Return a decision paired with complete supporting evidence."""


@runtime_checkable
class CapabilityPromotionScenarioRunner(Protocol):
    """Host-owned behavioral checks over staged implementations."""

    async def run(
        self,
        candidate: CapabilityPromotionCandidate,
        release: CapabilityReleaseTag,
        implementations: Mapping[str, object],
        descriptors: Mapping[str, CapabilityDescriptor],
    ) -> Sequence[CapabilityPromotionEvidence]:
        """Return content-safe scenario evidence without leaking objects."""


@runtime_checkable
class ModelCallStore(Protocol):
    """Durable route and attempt evidence for actual provider calls."""

    async def begin(
        self,
        route: RouteDecision,
        attempt: ModelCallAttempt,
    ) -> None:
        """Persist route selection and attempt intent before dispatch."""

    async def complete(self, result: ModelCallResult) -> None:
        """Persist one immutable terminal outcome."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[ModelCallRecord]:
        """Return newest provider attempts for one ChatSpec identity."""

    async def scan_all(self) -> Sequence[ModelCallRecord]:
        """Return all records for rebuilding disposable projections."""


@runtime_checkable
class ModelResourceRecoveryPort(Protocol):
    """Host boundary for durable model-resource continuation work."""

    async def defer(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> ModelResourceWait | None:
        """Persist a terminal provider failure as a resource wait."""

    async def defer_model_step(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> ModelStepContinuation | None:
        """Persist a continuation after a partial model stream."""

    async def release_provider_resource(
        self,
        *,
        provider_id: str,
        model_id: str,
    ) -> Sequence[ModelResourceWait]:
        """Release exact waits after verified provider availability."""


@runtime_checkable
class ModelStepContinuationHistoryPort(Protocol):
    """Read-only partial-stream recovery history for one ChatSpec."""

    async def scan_model_steps_for_conversation(
        self,
        conversation_id: str,
    ) -> Sequence[ModelStepContinuation]:
        """Return every content-free model-step continuation in order."""


@runtime_checkable
class ModelRecoveryHistoryPort(
    ModelStepContinuationHistoryPort,
    Protocol,
):
    """Read-only model recovery history for one ChatSpec."""

    async def scan_model_resource_waits_for_conversation(
        self,
        conversation_id: str,
    ) -> Sequence[ModelResourceWait]:
        """Return every content-free resource wait in order."""


@runtime_checkable
class ObservationProjectionPort(Protocol):
    """Read-only semantic projection over authoritative runtime facts."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[RuntimeObservation]:
        """Return newest observations for one ChatSpec identity."""

    async def page_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ObservationPage:
        """Return one stable page over a fixed source snapshot."""


@runtime_checkable
class ObservationHistoryPort(Protocol):
    """Read every semantic observation for deterministic replay."""

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> Sequence[RuntimeObservation]:
        """Return all current observations for one ChatSpec identity."""


@runtime_checkable
class VerificationHistoryPort(Protocol):
    """Read-only Task verification history for one ChatSpec identity."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[VerificationRecord]:
        """Return newest authoritative verifier records for one ChatSpec."""


@runtime_checkable
class TaskResultHistoryPort(Protocol):
    """Read Task-owned results associated with one ChatSpec identity."""

    async def read_for_conversation(
        self,
        conversation_id: str,
    ) -> ConversationTaskResultRecords:
        """Return one consistent result snapshot from matching Task ledgers."""


@runtime_checkable
class ConversationOutcomeStore(Protocol):
    """Durable source of explicit business outcomes for Chat intents."""

    async def append(self, outcome: ConversationOutcome) -> None:
        """Append one immutable outcome with explicit supersession."""

    async def latest_for_correlations(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        correlation_ids: Sequence[UUID],
    ) -> Sequence[ConversationOutcome]:
        """Return at most one latest outcome for each requested intent."""


@runtime_checkable
class ConversationOutcomeLookupPort(Protocol):
    """Optional exact lookup used for cross-Invocation idempotency."""

    async def get(self, outcome_id: UUID) -> ConversationOutcome | None:
        """Return one immutable outcome by id for recovery replay."""


@runtime_checkable
class GoalExecutionStore(Protocol):
    """Revisioned source of the current long-running Goal per Chat."""

    async def read(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> GoalExecution | None:
        """Return the current durable Goal snapshot for one Chat."""

    async def write(
        self,
        execution: GoalExecution,
        *,
        expected_revision: int,
    ) -> GoalExecution:
        """Create or replace one exact observed Goal revision."""

    async def list_pending(
        self,
        *,
        agent_id: str,
    ) -> Sequence[GoalExecution]:
        """List Goal outcomes requiring Host reconciliation."""


@runtime_checkable
class ConversationCorrelationResolver(Protocol):
    """Resolve an active long-running intent at submission admission."""

    async def active_correlation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> UUID | None:
        """Return the active correlation or None for a new intent."""


@runtime_checkable
class ConversationOutcomeHistoryPort(Protocol):
    """Read immutable Outcome supersession history for replay."""

    async def list_for_correlation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        correlation_id: UUID,
    ) -> Sequence[ConversationOutcome]:
        """Return one complete correlation history in chronological order."""


@runtime_checkable
class OutcomeHost(Protocol):
    """Invocation-scoped service for admitted business outcome requests."""

    async def declare(
        self,
        request: ConversationOutcomeRequest,
    ) -> ConversationOutcome:
        """Bind trusted invocation identity and submit through Host policy."""


@runtime_checkable
class OutcomeHostAccess(Protocol):
    """Optional extension implemented by outcome-aware Provider Hosts."""

    def outcome_host(self) -> OutcomeHost | None:
        """Return an admitted outcome service or None for this provider."""


@runtime_checkable
class ActionStore(Protocol):
    """Durable request/result boundary for observable Agent OS actions."""

    async def begin(self, request: ActionRequest) -> None:
        """Persist immutable action intent before execution."""

    async def complete(self, result: ActionResult) -> None:
        """Persist one immutable terminal result after verification."""

    async def link_approval(self, link: ActionApprovalLink) -> None:
        """Persist an approval relation discovered during execution."""

    async def get(
        self,
        action_id: UUID,
        *,
        invocation_id: UUID,
        conversation_id: str | None,
    ) -> ActionRecord | None:
        """Read one exact Action identity without scanning projections."""

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[ActionRecord]:
        """Return newest action records for one ChatSpec identity."""

    async def scan_for_conversation(
        self,
        conversation_id: str,
    ) -> Sequence[ActionRecord]:
        """Scan all records for recovery reconciliation."""


@runtime_checkable
class ActionRetryInputStore(Protocol):
    """Private raw input paired with a content-safe retry checkpoint."""

    async def save(
        self,
        request: ActionRequest,
        decision: ActionRetryDecision,
    ) -> ActionRetryInputCheckpoint:
        """Persist exact input before publishing a retryable result."""

    async def load(
        self,
        checkpoint_id: UUID,
    ) -> tuple[ActionRetryInputCheckpoint, JsonObject]:
        """Load exact input for one verified retry dispatcher."""

    async def list_checkpoints(
        self,
        *,
        agent_id: str,
    ) -> Sequence[ActionRetryInputCheckpoint]:
        """List private retry references owned by one Agent."""


@runtime_checkable
class ActionRetryContinuationStore(Protocol):
    """Durable outbox for one Host-admitted Action retry."""

    async def defer(
        self,
        checkpoint: ActionRetryInputCheckpoint,
        result: ActionResult,
    ) -> ActionRetryContinuation:
        """Publish retry work only after its terminal result commits."""

    async def get(
        self,
        continuation_id: UUID,
    ) -> ActionRetryContinuation | None:
        """Read one retry entry without changing its lifecycle."""

    async def list_pending(
        self,
        *,
        agent_id: str,
        now: datetime | None = None,
    ) -> Sequence[ActionRetryContinuation]:
        """List owned waiting work and promote matured delays."""

    async def cancel(
        self,
        continuation_id: UUID,
    ) -> ActionRetryContinuation:
        """Cancel one undispatched retry entry idempotently."""

    async def cancel_for_conversation(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> Sequence[ActionRetryContinuation]:
        """Cancel all pending retry work for one ChatSpec identity."""

    async def dispatch(
        self,
        continuation_id: UUID,
        dispatcher: Callable[
            [ActionRetryContinuation],
            Awaitable[UUID | None],
        ],
    ) -> ActionRetryContinuation:
        """Bind one ready entry to an idempotent dispatcher."""

    async def list_dispatched(
        self,
        *,
        agent_id: str,
    ) -> Sequence[ActionRetryContinuation]:
        """List work already bound to a durable dispatcher identity."""

    async def requeue_dispatched(
        self,
        continuation_id: UUID,
        *,
        dispatch_id: UUID,
    ) -> ActionRetryContinuation:
        """Requeue one exact interrupted dispatch for reconciliation."""


@runtime_checkable
class EventPublisher(Protocol):
    """Live delivery boundary for already committed events."""

    async def publish(self, event: ExecutionEvent) -> None:
        """Publish an event after the ledger transaction commits."""
