# -*- coding: utf-8 -*-
"""Immutable domain models shared by every QwenPaw edition."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Protocol, Self, runtime_checkable
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

NonEmptyStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
NamespacedId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    ),
]
JsonObject = dict[str, JsonValue]
TOOL_ARTIFACT_OUTPUTS_METADATA_KEY = "qwenpaw_artifact_outputs"


def utc_now() -> datetime:
    """Return an aware UTC timestamp for model defaults."""
    return datetime.now(timezone.utc)


class KernelModel(BaseModel):
    """Base class for immutable, strict public kernel contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        validate_default=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    schema_id: Literal["qwenpaw.kernel-model.v1"] = Field(
        default="qwenpaw.kernel-model.v1",
        alias="schema",
    )


class TaskStatus(str, Enum):
    """Durable lifecycle states for a user goal."""

    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    """Lifecycle states for one execution attempt."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskSource(str, Enum):
    """Origin of a task request."""

    USER = "user"
    SENSOR = "sensor"
    SCHEDULE = "schedule"
    META = "meta"
    API = "api"


class RiskLevel(str, Enum):
    """Risk classification used by policy and approval contracts."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolEffect(str, Enum):
    """Observable effect class declared by a governed tool."""

    NONE = "none"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    PROCESS = "process"


class ActionKind(str, Enum):
    """Executor family behind one observable Agent OS action."""

    TOOL = "tool"
    DRIVER = "driver"
    MCP = "mcp"
    SHELL = "shell"
    BROWSER = "browser"
    HARNESS_REMOTE = "harness_remote"


class ActionStatus(str, Enum):
    """Terminal outcome of an action at the verification boundary."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    DENIED = "denied"


class ModelRouteReason(str, Enum):
    """Why one concrete provider/model was selected for an attempt."""

    PRIMARY = "primary"
    SAME_MODEL_RETRY = "same_model_retry"
    FALLBACK = "fallback"
    OVERFLOW_RETRY = "overflow_retry"


class ModelCallStatus(str, Enum):
    """Terminal state of one concrete upstream model attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EnvironmentIsolation(str, Enum):
    """Execution isolation required by an environment contract."""

    HOST = "host"
    SANDBOX = "sandbox"
    CONTAINER = "container"
    REMOTE = "remote"


class EnvironmentNetworkMode(str, Enum):
    """Network posture required before execution starts."""

    INHERIT = "inherit"
    DENY = "deny"
    RESTRICTED = "restricted"


class EnvironmentMountAccess(str, Enum):
    """Filesystem access granted by one logical mount."""

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class EnvironmentFilesystemMode(str, Enum):
    """Visibility posture required for paths outside declared mounts."""

    HOST = "host"
    READ_ALL = "read_all"
    ALLOWLIST = "allowlist"


class EnvironmentVariableMode(str, Enum):
    """Host environment inheritance and injection posture."""

    INHERIT = "inherit"
    INJECT = "inject"
    ALLOWLIST = "allowlist"


class EnvironmentDependencyKind(str, Enum):
    """Dependency classes the Lite resolver can verify locally."""

    EXECUTABLE = "executable"
    PATH = "path"


class EnvironmentResolutionStatus(str, Enum):
    """Whether a concrete runtime satisfies its declared contract."""

    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"


class EnvironmentEvidenceLevel(str, Enum):
    """Strength of the evidence behind a concrete environment claim."""

    HOST_VERIFIED = "host_verified"
    PROVIDER_DECLARED = "provider_declared"
    PROVIDER_ATTESTED = "provider_attested"


class EnvironmentMount(KernelModel):
    """Logical mount requirement independent of a sandbox backend."""

    source: NonEmptyStr
    target: NonEmptyStr
    access: EnvironmentMountAccess = EnvironmentMountAccess.READ_ONLY
    required: bool = True
    executable: bool = False


class EnvironmentDependency(KernelModel):
    """One host-verifiable dependency requirement."""

    kind: EnvironmentDependencyKind
    name: NonEmptyStr
    version_constraint: NonEmptyStr | None = None
    required: bool = True


class EnvironmentPortRule(KernelModel):
    """One TCP admission rule independent of an OS firewall backend."""

    port: int = Field(ge=1, le=65535)
    direction: Literal["connect", "bind"] = "connect"
    allow: bool = True


class EnvironmentResourceLimits(KernelModel):
    """Resource ceilings requested from an execution backend."""

    cpu_cores: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)
    storage_mb: int | None = Field(default=None, gt=0)
    max_processes: int | None = Field(default=None, gt=0)

    @property
    def constrained(self) -> bool:
        """Return whether any hard resource ceiling was requested."""
        return any(
            value is not None
            for value in (
                self.cpu_cores,
                self.memory_mb,
                self.storage_mb,
                self.max_processes,
            )
        )


class EnvironmentContract(KernelModel):
    """Backend-neutral requirements fixed before one invocation starts."""

    contract_id: NamespacedId = "qwenpaw.system.environment.lite-local"
    version: NonEmptyStr = "1.0.0"
    os_families: tuple[Literal["linux", "macos", "windows"], ...] = ()
    architectures: tuple[NonEmptyStr, ...] = ()
    isolation: EnvironmentIsolation = EnvironmentIsolation.HOST
    runtime_image: NonEmptyStr | None = None
    filesystem_mode: EnvironmentFilesystemMode = EnvironmentFilesystemMode.HOST
    workspace: EnvironmentMount
    mounts: tuple[EnvironmentMount, ...] = ()
    denied_paths: tuple[NonEmptyStr, ...] = ()
    network_mode: EnvironmentNetworkMode = EnvironmentNetworkMode.INHERIT
    allowed_hosts: tuple[NonEmptyStr, ...] = ()
    network_ports: tuple[EnvironmentPortRule, ...] = ()
    environment_mode: EnvironmentVariableMode = EnvironmentVariableMode.INHERIT
    environment_variables: tuple[NonEmptyStr, ...] = ()
    credential_refs: tuple[NonEmptyStr, ...] = ()
    dependencies: tuple[EnvironmentDependency, ...] = ()
    native_constraint_names: tuple[NonEmptyStr, ...] = ()
    resources: EnvironmentResourceLimits = Field(
        default_factory=EnvironmentResourceLimits,
    )
    timeout_seconds: int | None = Field(default=None, gt=0)
    max_concurrency: int | None = Field(default=None, gt=0)
    snapshot_required: bool = False
    cleanup_policy: Literal["retain", "delete_temporary"] = "retain"

    @model_validator(mode="after")
    def validate_environment_requirements(self) -> Self:
        """Reject ambiguous mounts, dependencies, and network policy."""
        mounts = (self.workspace, *self.mounts)
        targets = tuple(mount.target for mount in mounts)
        if len(targets) != len(set(targets)):
            raise ValueError("environment mount targets must be unique")
        dependencies = tuple(
            (dependency.kind, dependency.name)
            for dependency in self.dependencies
        )
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("environment dependencies must be unique")
        if (
            self.network_mode is EnvironmentNetworkMode.RESTRICTED
            and not self.allowed_hosts
        ):
            raise ValueError("restricted network requires allowed_hosts")
        if (
            self.network_mode is not EnvironmentNetworkMode.RESTRICTED
            and self.allowed_hosts
        ):
            raise ValueError(
                "allowed_hosts require restricted network mode",
            )
        if len(self.credential_refs) != len(set(self.credential_refs)):
            raise ValueError("environment credential refs must be unique")
        for values, label in (
            (self.denied_paths, "denied paths"),
            (self.environment_variables, "environment variables"),
            (self.native_constraint_names, "native constraint names"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"environment {label} must be unique")
        ports = tuple(
            (rule.port, rule.direction, rule.allow)
            for rule in self.network_ports
        )
        if len(ports) != len(set(ports)):
            raise ValueError("environment port rules must be unique")
        if (
            self.environment_mode is EnvironmentVariableMode.INHERIT
            and self.environment_variables
        ):
            raise ValueError(
                "inherited environment cannot declare injected variables",
            )
        return self


class EnvironmentResolution(KernelModel):
    """Immutable evidence produced by an edition-specific resolver."""

    resolution_id: UUID = Field(default_factory=uuid4)
    invocation_id: UUID
    action_id: UUID | None = None
    contract_id: NamespacedId
    contract_version: NonEmptyStr
    resolver_id: NamespacedId
    status: EnvironmentResolutionStatus
    evidence_level: EnvironmentEvidenceLevel = (
        EnvironmentEvidenceLevel.HOST_VERIFIED
    )
    os_family: Literal["linux", "macos", "windows"]
    architecture: NonEmptyStr
    workspace_root: NonEmptyStr
    enforced_constraints: tuple[NonEmptyStr, ...] = ()
    declared_constraints: tuple[NonEmptyStr, ...] = ()
    violations: tuple[NonEmptyStr, ...] = ()
    resolved_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        """Keep terminal status and violation evidence coherent."""
        if (
            self.status is EnvironmentResolutionStatus.SATISFIED
            and self.violations
        ):
            raise ValueError("satisfied environment cannot have violations")
        if (
            self.status is EnvironmentResolutionStatus.UNSATISFIED
            and not self.violations
        ):
            raise ValueError("unsatisfied environment requires violations")
        return self


class EnvironmentRef(KernelModel):
    """Stable Action reference to its invocation environment evidence."""

    resolution_id: UUID
    contract_id: NamespacedId
    contract_version: NonEmptyStr
    resolver_id: NamespacedId


class EnvironmentRecord(KernelModel):
    """Durable pair of declared requirements and observed resolution."""

    contract: EnvironmentContract
    resolution: EnvironmentResolution

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Require durable evidence to describe the same contract."""
        if self.resolution.contract_id != self.contract.contract_id:
            raise ValueError("environment record contract mismatch")
        if self.resolution.contract_version != self.contract.version:
            raise ValueError("environment record version mismatch")
        return self


class ActionApprovalLink(KernelModel):
    """Immutable relation for approvals discovered during an action."""

    action_id: UUID
    invocation_id: UUID
    conversation_id: NonEmptyStr | None = None
    approval_id: UUID
    source: ApprovalSource
    linked_at: AwareDatetime = Field(default_factory=utc_now)


class ActionRequest(KernelModel):
    """Privacy-safe durable intent recorded before external execution.

    ``arguments`` is available only to the in-memory executor. Durable
    stores serialize the redacted projection and its integrity hash.
    """

    action_id: UUID = Field(default_factory=uuid4)
    invocation_id: UUID
    correlation_id: UUID
    conversation_id: NonEmptyStr | None = None
    registry_generation: int = Field(ge=1)
    environment_ref: EnvironmentRef | None = None
    capability_id: NamespacedId
    kind: ActionKind
    action_name: NonEmptyStr
    arguments: SkipJsonSchema[JsonObject] = Field(
        default_factory=dict,
        exclude=True,
    )
    redacted_arguments: JsonObject = Field(default_factory=dict)
    arguments_hash: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ] = Field(
        description=(
            "SHA256 of redacted_arguments; never a fingerprint of raw "
            "secret or omitted content"
        ),
    )
    effect: ToolEffect = ToolEffect.NONE
    risk: RiskLevel = RiskLevel.LOW
    reversible: bool = True
    idempotency_key: NonEmptyStr
    approval_id: UUID | None = None
    policy_decision: NonEmptyStr = "allow"
    requested_at: AwareDatetime = Field(default_factory=utc_now)


class ActionResult(KernelModel):
    """Content-minimal terminal evidence for one action request."""

    action_id: UUID
    invocation_id: UUID
    conversation_id: NonEmptyStr | None = None
    status: ActionStatus
    observation: SkipJsonSchema[object | None] = Field(
        default=None,
        exclude=True,
    )
    observation_digest: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ] = Field(
        description=(
            "SHA256 of content-free status and durable evidence references"
        ),
    )
    artifact_refs: tuple["ArtifactRef", ...] = ()
    evidence_refs: tuple["EvidenceRef", ...] = ()
    approval_ids: tuple[UUID, ...] = ()
    error_code: str = ""
    retryable: bool = False
    side_effect_status: SideEffectStatus | None = None
    completed_at: AwareDatetime = Field(default_factory=utc_now)


class ActionRecord(KernelModel):
    """Queryable action projection with an optional terminal result."""

    request: ActionRequest
    approval_links: tuple[ActionApprovalLink, ...] = ()
    result: ActionResult | None = None

    @model_validator(mode="after")
    def validate_result_identity(self) -> Self:
        """Require request and result to describe the same execution."""
        if self.result is None:
            result_approval_ids: tuple[UUID, ...] = ()
        else:
            result_approval_ids = self.result.approval_ids
        link_ids = tuple(link.approval_id for link in self.approval_links)
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("action approval links must be unique")
        for link in self.approval_links:
            if link.action_id != self.request.action_id:
                raise ValueError("action approval link does not match request")
            if link.invocation_id != self.request.invocation_id:
                raise ValueError(
                    "action approval link invocation does not match request",
                )
            if link.conversation_id != self.request.conversation_id:
                raise ValueError(
                    "action approval link conversation does not match request",
                )
        if self.result is not None and set(result_approval_ids) != set(
            link_ids,
        ):
            raise ValueError("action result approvals do not match links")
        if self.result is None:
            return self
        if self.result.action_id != self.request.action_id:
            raise ValueError("action result does not match request")
        if self.result.invocation_id != self.request.invocation_id:
            raise ValueError("action result invocation does not match request")
        if self.result.conversation_id != self.request.conversation_id:
            raise ValueError(
                "action result conversation does not match request",
            )
        return self


class ToolArtifactOutput(KernelModel):
    """Host-captured artifact declared by one successful tool result.

    ``path_parameter`` names an already-governed tool input instead of
    accepting a new path from result metadata. The host resolves and reads
    that value only after the tool succeeds, then replaces this declaration
    with canonical Artifact/Evidence references.
    """

    path_parameter: NonEmptyStr
    kind: NamespacedId
    evidence_claim: NonEmptyStr
    media_type: NonEmptyStr | None = None
    name: NonEmptyStr | None = None
    path_normalization: Literal["native", "url"] = "native"
    metadata: JsonObject = Field(default_factory=dict)


class SideEffectStatus(str, Enum):
    """Durable lifecycle for one potentially non-repeatable operation."""

    PREPARED = "prepared"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ArtifactStatus(str, Enum):
    """Lifecycle of one immutable artifact registry entry."""

    DRAFT = "draft"
    READY = "ready"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class VerificationStatus(str, Enum):
    """Terminal verifier decision over task acceptance."""

    PASSED = "passed"
    FAILED = "failed"


class SideEffectDisposition(str, Enum):
    """Whether a caller may execute after reserving an idempotency key."""

    EXECUTE = "execute"
    REPLAY = "replay"
    BLOCKED = "blocked"


class ApprovalStatus(str, Enum):
    """Lifecycle state of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalSource(str, Enum):
    """Runtime boundary that originated an approval request."""

    TOOL = "tool"
    DRIVER = "driver"
    HARNESS = "harness"
    PROPOSAL = "proposal"
    SYSTEM = "system"


class ApprovalDecisionValue(str, Enum):
    """Final choices accepted by an approval decision."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalContinuation(str, Enum):
    """Task behavior after a human resolves an approval request."""

    FAIL_ON_REJECTION = "fail_on_rejection"
    RESUME_ON_DECISION = "resume_on_decision"


class ApprovalLevel(str, Enum):
    """Effective tool approval policy for one task run."""

    STRICT = "strict"
    SMART = "smart"
    AUTO = "auto"
    OFF = "off"
    AGENT_PROFILE = "agent_profile"


class AutonomyLevel(str, Enum):
    """How independently a Task may run."""

    INTERACTIVE = "l1"
    CONTROLLED_BACKGROUND = "l2"
    UNATTENDED = "l3"


class CostAccountingMode(str, Enum):
    """How a Runner accounts for externally billable execution."""

    REPORTED = "reported"
    ZERO = "zero"
    UNKNOWN = "unknown"


class ActorType(str, Enum):
    """Type of actor responsible for a domain action."""

    USER = "user"
    SYSTEM = "system"
    AGENT = "agent"
    PLUGIN = "plugin"
    RUNNER = "runner"
    SENSOR = "sensor"


class RestartPolicy(str, Enum):
    """Activation boundary required by a plugin contribution."""

    HOT = "hot"
    SCOPED = "scoped"
    FULL = "full"


class CapabilityProviderKind(str, Enum):
    """Trust origin of one capability provider.

    Dispatch must not branch on this value. It exists for lifecycle policy,
    diagnostics, and audit output only.
    """

    SYSTEM = "system"
    PLUGIN = "plugin"


class CommandDisposition(str, Enum):
    """Effect produced by a slash-command dispatch."""

    NOT_HANDLED = "not_handled"
    CONTINUE = "continue"
    RESPOND = "respond"


class LifecyclePhase(str, Enum):
    """Stable phase points around one interactive runtime invocation."""

    PRE_DISPATCH = "pre_dispatch"
    POST_DISPATCH = "post_dispatch"
    PRE_AGENT_BUILD = "pre_agent_build"
    POST_AGENT_BUILD = "post_agent_build"
    PRE_EXECUTE = "pre_execute"
    POST_RESPONSE = "post_response"
    ON_ERROR = "on_error"
    FINALLY = "finally"


class HookDisposition(str, Enum):
    """Effect produced by one lifecycle hook or phase."""

    CONTINUE = "continue"
    SHORT_CIRCUIT = "short_circuit"
    SKIP_AGENT = "skip_agent"


class StopGateAction(str, Enum):
    """Decision made after one Agent reasoning iteration."""

    BYPASS = "bypass"
    INTERRUPT_AND_CONTINUE = "interrupt_and_continue"
    TERMINATE = "terminate"


class ActorRef(KernelModel):
    """Stable identity attached to events and decisions."""

    type: ActorType
    id: NonEmptyStr


class ExecutionBudget(KernelModel):
    """Hard resource ceilings shared by every execution entrypoint."""

    max_duration_seconds: float | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost_micros: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, gt=0)
    max_retries: int = Field(default=0, ge=0)
    max_concurrency: int = Field(default=1, gt=0)


class UsageDelta(KernelModel):
    """One additive resource observation from an execution boundary."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_micros: int = Field(default=0, ge=0)
    cost_unknown: bool = False
    tool_calls: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        """Return provider-independent input plus output tokens."""
        return self.input_tokens + self.output_tokens

    @model_validator(mode="after")
    def validate_non_empty(self) -> Self:
        """Reject no-op observations that add noise to the ledger."""
        if not any(
            (
                self.input_tokens,
                self.output_tokens,
                self.cost_micros,
                self.cost_unknown,
                self.tool_calls,
            ),
        ):
            raise ValueError("usage delta must record at least one resource")
        return self


class UsageSnapshot(KernelModel):
    """Cumulative resource usage reconstructed from durable events."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_micros: int = Field(default=0, ge=0)
    cost_unknown: bool = False
    tool_calls: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        """Return cumulative provider-independent token usage."""
        return self.input_tokens + self.output_tokens

    def add(self, delta: UsageDelta) -> "UsageSnapshot":
        """Return a new snapshot containing one additive observation."""
        return UsageSnapshot(
            input_tokens=self.input_tokens + delta.input_tokens,
            output_tokens=self.output_tokens + delta.output_tokens,
            cost_micros=self.cost_micros + delta.cost_micros,
            cost_unknown=self.cost_unknown or delta.cost_unknown,
            tool_calls=self.tool_calls + delta.tool_calls,
        )


class RetryPolicy(KernelModel):
    """Bounded retry behavior independent from API request retries."""

    backoff_seconds: float = Field(default=0, ge=0)
    retryable_error_codes: tuple[NonEmptyStr, ...] = ()
    retry_side_effects: bool = False


class TimeoutPolicy(KernelModel):
    """Timeouts applied at execution and human-wait boundaries."""

    attempt_seconds: float | None = Field(default=None, gt=0)
    idle_seconds: float | None = Field(default=None, gt=0)
    approval_seconds: float | None = Field(default=None, gt=0)


class SideEffectPolicy(KernelModel):
    """Fail-closed rules for externally observable operations."""

    require_idempotency: bool = True
    automatic_retry: bool = False
    uncertain_outcome: Literal["block", "escalate"] = "block"


class EscalationPolicy(KernelModel):
    """Action selected when an autonomous run cannot continue safely."""

    on_budget_exhausted: Literal["fail", "pause", "inbox"] = "pause"
    on_permission_denied: Literal["fail", "pause", "inbox"] = "pause"
    on_recovery_required: Literal["fail", "pause", "inbox"] = "inbox"


class ExitCondition(KernelModel):
    """One explicit condition that can terminate an execution."""

    condition_id: NamespacedId
    kind: Literal[
        "acceptance_met",
        "artifact_emitted",
        "max_iterations",
        "explicit_signal",
    ]
    parameters: JsonObject = Field(default_factory=dict)
    required: bool = True

    @model_validator(mode="after")
    def validate_parameters(self) -> Self:
        """Reject ambiguous built-in condition parameters at creation."""
        if self.kind == "max_iterations":
            limit = self.parameters.get("limit")
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
            ):
                raise ValueError(
                    "max_iterations exit condition requires positive "
                    "integer parameters.limit",
                )
        criterion = self.parameters.get("criterion")
        if self.kind == "acceptance_met" and criterion is not None:
            if not isinstance(criterion, str) or not criterion.strip():
                raise ValueError(
                    "acceptance_met parameters.criterion must be non-empty",
                )
        artifact_kind = self.parameters.get("kind")
        if self.kind == "artifact_emitted" and artifact_kind is not None:
            if not isinstance(artifact_kind, str) or not artifact_kind.strip():
                raise ValueError(
                    "artifact_emitted parameters.kind must be non-empty",
                )
        return self


class ArtifactRequirement(KernelModel):
    """Artifact shape required before a Task may claim completion."""

    kind: NamespacedId
    media_types: tuple[NonEmptyStr, ...] = ()
    min_count: int = Field(default=1, gt=0)


class VerificationPolicy(KernelModel):
    """Evidence and verifier requirements applied after execution."""

    verifier_ids: tuple[NamespacedId, ...] = ()
    require_all_acceptance: bool = True
    require_evidence: bool = False
    fail_on_unverified: bool = True


class ExecutionContract(KernelModel):
    """Immutable reliability contract shared by all Task producers."""

    goal: NonEmptyStr
    acceptance: tuple[NonEmptyStr, ...] = ()
    autonomy_level: AutonomyLevel = AutonomyLevel.INTERACTIVE
    permission_scope: NamespacedId | None = None
    side_effect_policy: SideEffectPolicy = Field(
        default_factory=SideEffectPolicy,
    )
    budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_policy: TimeoutPolicy = Field(default_factory=TimeoutPolicy)
    escalation_policy: EscalationPolicy = Field(
        default_factory=EscalationPolicy,
    )
    exit_conditions: tuple[ExitCondition, ...] = ()
    required_artifacts: tuple[ArtifactRequirement, ...] = ()
    verification_policy: VerificationPolicy | None = None

    @model_validator(mode="after")
    def validate_unattended_contract(  # pylint: disable=too-many-branches
        self,
    ) -> Self:
        """Reject ambiguous policies and incomplete unattended work."""
        condition_ids = tuple(
            condition.condition_id for condition in self.exit_conditions
        )
        if len(condition_ids) != len(set(condition_ids)):
            raise ValueError("exit condition ids must be unique")
        artifact_kinds = tuple(
            requirement.kind for requirement in self.required_artifacts
        )
        if len(artifact_kinds) != len(set(artifact_kinds)):
            raise ValueError("required artifact kinds must be unique")
        for condition in self.exit_conditions:
            if condition.kind != "acceptance_met":
                continue
            criterion = condition.parameters.get("criterion")
            if criterion is not None and criterion not in self.acceptance:
                raise ValueError(
                    "acceptance_met criterion must belong to contract "
                    "acceptance",
                )
        automatic_retry = getattr(
            self.side_effect_policy,
            "automatic_retry",
        )
        retry_side_effects = getattr(
            self.retry_policy,
            "retry_side_effects",
        )
        if automatic_retry != retry_side_effects:
            raise ValueError("side-effect retry policies must agree")
        if automatic_retry and not getattr(
            self.side_effect_policy,
            "require_idempotency",
        ):
            raise ValueError(
                "automatic side-effect retry requires idempotency",
            )
        if self.autonomy_level is not AutonomyLevel.UNATTENDED:
            return self
        missing = []
        if not self.acceptance:
            missing.append("acceptance")
        if self.permission_scope is None:
            missing.append("permission_scope")
        if not self.exit_conditions:
            missing.append("exit_conditions")
        if self.verification_policy is None:
            missing.append("verification_policy")
        budget_fields = (
            "max_duration_seconds",
            "max_tokens",
            "max_cost_micros",
            "max_tool_calls",
        )
        missing_budget = tuple(
            field
            for field in budget_fields
            if getattr(self.budget, field) is None
        )
        if missing_budget:
            missing.append(f"budget.{','.join(missing_budget)}")
        if missing:
            raise ValueError(
                f"unattended execution requires: {', '.join(missing)}",
            )
        return self


class Task(KernelModel):
    """Durable representation of a user goal."""

    task_id: UUID = Field(default_factory=uuid4)
    objective: NonEmptyStr
    status: TaskStatus = TaskStatus.CREATED
    source: TaskSource
    agent_id: NonEmptyStr
    constraints: tuple[NonEmptyStr, ...] = ()
    acceptance_criteria: tuple[NonEmptyStr, ...] = ()
    execution_contract: ExecutionContract | None = None
    active_run_id: UUID | None = None
    version: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        """Reject impossible task timestamps."""
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.execution_contract is not None:
            if self.execution_contract.goal != self.objective:
                raise ValueError(
                    "execution contract goal must match objective",
                )
            if self.execution_contract.acceptance != self.acceptance_criteria:
                raise ValueError(
                    "execution contract acceptance must match task criteria",
                )
        return self


class PlanStep(KernelModel):
    """One deterministic unit in a task plan."""

    step_id: UUID = Field(default_factory=uuid4)
    title: NonEmptyStr
    objective: NonEmptyStr
    depends_on: tuple[UUID, ...] = ()
    capability_id: NamespacedId | None = None
    metadata: JsonObject = Field(default_factory=dict)


class Plan(KernelModel):
    """Versioned plan associated with a task."""

    plan_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    revision: int = Field(ge=1)
    steps: tuple[PlanStep, ...]
    acceptance_criteria: tuple[NonEmptyStr, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_step_graph(self) -> Self:
        """Require unique steps, known dependencies, and an acyclic graph."""
        step_ids = [step.step_id for step in self.steps]
        known = set(step_ids)
        if len(known) != len(step_ids):
            raise ValueError("plan step ids must be unique")

        graph = {step.step_id: set(step.depends_on) for step in self.steps}
        for step_id, dependencies in graph.items():
            unknown = dependencies - known
            if unknown:
                raise ValueError(
                    f"step {step_id} depends on an unknown step",
                )
            if step_id in dependencies:
                raise ValueError("plan dependency cycle detected")

        visiting: set[UUID] = set()
        visited: set[UUID] = set()

        def visit(step_id: UUID) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                raise ValueError("plan dependency cycle detected")
            visiting.add(step_id)
            for dependency in graph[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        return self


class Run(KernelModel):
    """One immutable snapshot of an execution attempt."""

    run_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    plan_id: UUID | None = None
    attempt: int = Field(ge=1)
    status: RunStatus = RunStatus.PENDING
    registry_generation: int = Field(ge=1)
    runner_id: NamespacedId
    strategy_id: NamespacedId | None = None
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    checkpoint_id: UUID | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        """Reject a finish without a coherent start timestamp."""
        if self.finished_at is None:
            return self
        if self.started_at is None:
            raise ValueError("finished_at requires started_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        return self


class ArtifactRef(KernelModel):
    """Reference to data stored outside the execution ledger."""

    artifact_id: UUID = Field(default_factory=uuid4)
    kind: NamespacedId
    uri: NonEmptyStr
    media_type: NonEmptyStr
    content_hash: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^(?:sha256:)?[0-9a-f]{64}$",
        ),
    ]
    size_bytes: int = Field(ge=0)
    metadata: JsonObject = Field(default_factory=dict)


class ArtifactRecord(KernelModel):
    """Task-owned registry metadata around immutable artifact bytes."""

    artifact: ArtifactRef
    task_id: UUID
    run_id: UUID
    event_id: UUID
    step_id: UUID | None = None
    cause_event_id: UUID | None = None
    correlation_id: UUID | None = None
    version: int = Field(default=1, ge=1)
    status: ArtifactStatus = ArtifactStatus.READY
    supersedes_artifact_id: UUID | None = None
    producer: NamespacedId
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_supersedes(self) -> Self:
        """Prevent an artifact version from superseding itself."""
        if self.supersedes_artifact_id == self.artifact.artifact_id:
            raise ValueError("artifact cannot supersede itself")
        return self


class ArtifactRenderDisposition(str, Enum):
    """Browser handling requested for one immutable artifact view."""

    INLINE = "inline"
    ATTACHMENT = "attachment"


class ArtifactRenderRequest(KernelModel):
    """Bounded, verified input supplied to one Artifact Renderer."""

    artifact: ArtifactRef
    content: SkipJsonSchema[bytes] = Field(exclude=True)
    disposition: ArtifactRenderDisposition
    filename: NonEmptyStr
    max_output_bytes: int = Field(gt=0)


class ArtifactRenderResult(KernelModel):
    """Safe derived view returned without mutating the source artifact."""

    renderer_id: NamespacedId
    content: SkipJsonSchema[bytes] = Field(exclude=True)
    media_type: NonEmptyStr
    filename: NonEmptyStr
    disposition: ArtifactRenderDisposition
    source_content_hash: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^(?:sha256:)?[0-9a-f]{64}$",
        ),
    ]
    metadata: JsonObject = Field(default_factory=dict)


class ArtifactPreviewDescriptor(KernelModel):
    """Current-generation preview availability exposed to clients."""

    available: bool
    registry_generation: int = Field(ge=1)
    renderer_id: NamespacedId | None = None
    reason: str = ""

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        """Require a renderer exactly when preview is available."""
        if self.available and self.renderer_id is None:
            raise ValueError("available preview requires renderer_id")
        if not self.available and self.renderer_id is not None:
            raise ValueError("unavailable preview cannot name renderer_id")
        return self


class EvidenceRef(KernelModel):
    """Evidence claim backed by a durable artifact."""

    evidence_id: UUID = Field(default_factory=uuid4)
    artifact_id: UUID
    claim: NonEmptyStr
    producer: NonEmptyStr
    captured_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)


class EvidenceRecord(KernelModel):
    """Host-owned causal provenance for one immutable evidence claim."""

    evidence: EvidenceRef
    task_id: UUID
    run_id: UUID
    event_id: UUID
    step_id: UUID | None = None
    cause_event_id: UUID | None = None
    correlation_id: UUID | None = None
    source: NamespacedId


class AcceptanceVerification(KernelModel):
    """One verifier decision for one public acceptance criterion."""

    criterion: NonEmptyStr
    passed: bool
    evidence_ids: tuple[UUID, ...] = ()
    reason: str = ""

    @model_validator(mode="after")
    def validate_evidence_ids(self) -> Self:
        """Reject ambiguous duplicate evidence links."""
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("acceptance evidence ids must be unique")
        return self


class VerificationResult(KernelModel):
    """Immutable verifier output persisted through an execution event."""

    verification_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    run_id: UUID
    verifier_id: NamespacedId
    status: VerificationStatus
    acceptance: tuple[AcceptanceVerification, ...] = ()
    artifact_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = ()
    created_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        """Keep a passed decision consistent and references unambiguous."""
        if self.status is VerificationStatus.PASSED and any(
            not item.passed for item in self.acceptance
        ):
            raise ValueError("passed verification has failed acceptance")
        for name, values in (
            ("artifact", self.artifact_ids),
            ("evidence", self.evidence_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"verification {name} ids must be unique")
        return self


class VerificationRecord(KernelModel):
    """Host-owned causal provenance for one verifier result."""

    verification: VerificationResult
    task_id: UUID
    run_id: UUID
    event_id: UUID
    step_id: UUID | None = None
    cause_event_id: UUID | None = None
    correlation_id: UUID | None = None
    invocation_id: UUID | None = None
    registry_generation: int | None = Field(default=None, ge=1)
    source: NamespacedId
    occurred_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_ownership(self) -> Self:
        """Reject a verifier result wrapped by another Task or Run."""
        if self.verification.task_id != self.task_id:
            raise ValueError("verification record task ownership mismatch")
        if self.verification.run_id != self.run_id:
            raise ValueError("verification record run ownership mismatch")
        return self


class ResultPackage(KernelModel):
    """Terminal Task result assembled only from authoritative references."""

    task_id: UUID
    run_id: UUID
    artifacts: tuple[ArtifactRef, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    verifications: tuple[VerificationResult, ...] = ()


@runtime_checkable
class ArtifactEmitter(Protocol):
    """Task-scoped service that emits a durable artifact signal."""

    async def emit(
        self,
        *,
        kind: str,
        media_type: str,
        content: bytes,
        name: str | None = None,
        evidence_claim: str | None = None,
        metadata: dict[str, JsonValue] | None = None,
        step_id: UUID | None = None,
        cause_event_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> "RunnerSignal":
        """Store content and return its canonical runner signal."""


@runtime_checkable
class CheckpointBroker(Protocol):
    """Run-scoped host service for durable safe resume boundaries."""

    async def save(
        self,
        *,
        runner_cursor: JsonValue = None,
        workspace_checkpoint_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> "ExecutionCheckpoint":
        """Persist one safe checkpoint without suspending the active run."""


@runtime_checkable
class ApprovalBroker(Protocol):
    """Run-scoped durable approval service exposed to capabilities."""

    async def request(
        self,
        *,
        approval_id: UUID,
        action: str,
        risk: RiskLevel,
        requester: ActorRef,
        source: ApprovalSource,
        policy: str,
        continuation: ApprovalContinuation,
        redacted_arguments: JsonObject,
        display: ApprovalDisplay | None = None,
        expires_at: AwareDatetime | None = None,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ApprovalRequest:
        """Persist one request against the broker's active run."""

    async def get(
        self,
        approval_id: UUID,
    ) -> tuple[ApprovalRequest, ApprovalDecision | None] | None:
        """Return one request and its optional decision."""

    async def decide(
        self,
        approval_id: UUID,
        *,
        decision: ApprovalDecisionValue,
        actor: ActorRef,
        reason: str,
        scope: str = "exact",
        idempotency_key: str | None = None,
    ) -> ApprovalDecision:
        """Commit a decision through the durable task state machine."""


@runtime_checkable
class SideEffectBroker(Protocol):
    """Run-scoped ledger for potentially non-repeatable operations."""

    async def begin(
        self,
        *,
        action: str,
        target: str,
        effect: ToolEffect,
        idempotency_key: str,
        request_hash: str,
        invocation_id: UUID | None = None,
        correlation_id: UUID | None = None,
        approval_id: UUID | None = None,
        policy_decision: str = "",
    ) -> "SideEffectReservation":
        """Reserve a durable identity before the operation executes."""

    async def finish(
        self,
        record_id: UUID,
        *,
        status: SideEffectStatus,
        result_digest: str | None = None,
        external_ref: str = "",
        error_code: str = "",
    ) -> "SideEffectRecord":
        """Commit the terminal outcome of a reserved operation."""


@runtime_checkable
class CancellationToken(Protocol):
    """Read-only cooperative cancellation signal for one runtime attempt."""

    @property
    def cancelled(self) -> bool:
        """Return whether the host requested cancellation."""

    @property
    def reason(self) -> str | None:
        """Return the bounded host-provided cancellation reason."""

    async def wait(self) -> None:
        """Wait until the host requests cancellation."""

    def raise_if_cancelled(self) -> None:
        """Raise cancellation at an explicit capability boundary."""


@runtime_checkable
class UsageMeter(Protocol):
    """Task-scoped durable budget accounting exposed to capabilities."""

    async def record(
        self,
        delta: UsageDelta,
        *,
        source: str | None = None,
    ) -> UsageSnapshot:
        """Persist one observation and fail when a ceiling is exceeded."""

    async def snapshot(self) -> UsageSnapshot:
        """Reconstruct cumulative usage across all Task attempts."""

    async def acquire_concurrency(self) -> None:
        """Wait until one contract concurrency slot is available."""

    def release_concurrency(self) -> None:
        """Release one previously acquired concurrency slot."""


class ToolSelection(KernelModel):
    """Stable filters supplied to one runtime tool provider."""

    active_modes: tuple[NamespacedId, ...] = ()
    active_skills: tuple[NonEmptyStr, ...] = ()
    enabled_features: tuple[NamespacedId, ...] = ()
    explicit_enabled: tuple[NonEmptyStr, ...] = ()
    explicit_disabled: tuple[NonEmptyStr, ...] = ()
    subagent_allowed_tools: tuple[NonEmptyStr, ...] | None = None


class PromptFragment(KernelModel):
    """One ordered, provider-owned system-prompt fragment."""

    fragment_id: NamespacedId
    content: NonEmptyStr
    priority: int = 100


class ContextTrustLevel(str, Enum):
    """Host-assigned trust boundary for one model-visible input unit."""

    SYSTEM = "system"
    PROVIDER = "provider"
    USER = "user"
    MODEL = "model"
    TOOL = "tool"
    EXTERNAL = "external"


class ContextFragmentKind(str, Enum):
    """Model-input unit represented in a privacy-safe manifest."""

    MESSAGE = "message"
    TOOL_SCHEMA = "tool_schema"
    REDACTED = "redacted"


class ContextPolicy(KernelModel):
    """Stable capture policy applied before every provider model call."""

    policy_id: NamespacedId = "qwenpaw.system.context.default"
    version: NonEmptyStr = "1.0.0"
    hash_algorithm: Literal["sha256"] = "sha256"
    hidden_reasoning: Literal["redact"] = "redact"
    secret_material: Literal["hash_only"] = "hash_only"
    record_tool_schemas: Literal[True] = True
    max_fragments: int = Field(default=10_000, ge=1)
    token_estimate_divisor: float = Field(default=4.0, gt=0)


class ContextFragment(KernelModel):
    """Content-free provenance for one unit sent to a model provider."""

    fragment_id: NamespacedId
    kind: ContextFragmentKind
    source: NamespacedId
    source_version: NonEmptyStr
    trust_level: ContextTrustLevel
    selection_reason: NonEmptyStr
    transformations: tuple[NamespacedId, ...] = ()
    content_hash: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                pattern=r"^sha256:[0-9a-f]{64}$",
            ),
        ]
        | None
    ) = None
    size_bytes: int | None = Field(default=None, ge=0)
    estimated_tokens: int | None = Field(default=None, ge=0)
    redaction_reason: NamespacedId | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_redaction(self) -> Self:
        """Require captured hashes and keep redactions fingerprint-free."""
        if self.kind is ContextFragmentKind.REDACTED:
            if self.redaction_reason is None:
                raise ValueError("redacted context requires a reason")
            if any(
                value is not None
                for value in (
                    self.content_hash,
                    self.size_bytes,
                    self.estimated_tokens,
                )
            ):
                raise ValueError(
                    "redacted context cannot retain content fingerprints",
                )
            return self
        if self.content_hash is None:
            raise ValueError("captured context requires a content hash")
        if self.size_bytes is None or self.estimated_tokens is None:
            raise ValueError("captured context requires size and token data")
        if self.redaction_reason is not None:
            raise ValueError("captured context cannot have a redaction reason")
        return self


class ContextManifest(KernelModel):
    """Content-free input evidence shared by one logical model call."""

    manifest_id: UUID = Field(default_factory=uuid4)
    invocation_id: UUID
    correlation_id: UUID
    conversation_id: NonEmptyStr | None = None
    registry_generation: int = Field(ge=1)
    model_call_index: int = Field(ge=1)
    attempt_kind: Literal["primary", "overflow_retry"] = "primary"
    policy_id: NamespacedId
    policy_version: NonEmptyStr
    fragments: tuple[ContextFragment, ...] = ()
    total_size_bytes: int = Field(ge=0)
    total_estimated_tokens: int = Field(ge=0)
    disclosed_tool_count: int = Field(ge=0)
    manifest_hash: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ]
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        """Keep aggregate accounting derived from captured fragments."""
        fragment_ids = [fragment.fragment_id for fragment in self.fragments]
        if len(fragment_ids) != len(set(fragment_ids)):
            raise ValueError("context fragment IDs must be unique")
        captured = tuple(
            fragment
            for fragment in self.fragments
            if fragment.kind is not ContextFragmentKind.REDACTED
        )
        if self.total_size_bytes != sum(
            fragment.size_bytes or 0 for fragment in captured
        ):
            raise ValueError("context byte total does not match fragments")
        if self.total_estimated_tokens != sum(
            fragment.estimated_tokens or 0 for fragment in captured
        ):
            raise ValueError("context token total does not match fragments")
        tool_count = sum(
            fragment.kind is ContextFragmentKind.TOOL_SCHEMA
            for fragment in self.fragments
        )
        if self.disclosed_tool_count != tool_count:
            raise ValueError("disclosed tool count does not match fragments")
        return self


class RouteDecision(KernelModel):
    """Immutable selection of one concrete provider/model attempt."""

    route_decision_id: UUID = Field(default_factory=uuid4)
    attempt_id: UUID = Field(default_factory=uuid4)
    invocation_id: UUID
    correlation_id: UUID
    conversation_id: NonEmptyStr | None = None
    registry_generation: int = Field(ge=1)
    context_manifest_id: UUID
    model_call_index: int = Field(ge=1)
    attempt_index: int = Field(ge=1)
    provider_id: NonEmptyStr
    model_id: NonEmptyStr
    requested_provider_id: NonEmptyStr | None = None
    requested_model_id: NonEmptyStr | None = None
    reason: ModelRouteReason
    previous_attempt_id: UUID | None = None
    policy_id: NamespacedId = "qwenpaw.system.model-route"
    policy_version: NonEmptyStr = "1"
    decided_at: AwareDatetime = Field(default_factory=utc_now)


class ModelCallAttempt(KernelModel):
    """Durable intent recorded before one concrete upstream request."""

    attempt_id: UUID
    route_decision_id: UUID
    invocation_id: UUID
    correlation_id: UUID
    conversation_id: NonEmptyStr | None = None
    registry_generation: int = Field(ge=1)
    context_manifest_id: UUID
    model_call_index: int = Field(ge=1)
    attempt_index: int = Field(ge=1)
    provider_id: NonEmptyStr
    model_id: NonEmptyStr
    adapter_id: NonEmptyStr | None = None
    adapter_version: NonEmptyStr | None = None
    formatter_id: NonEmptyStr | None = None
    formatter_version: NonEmptyStr | None = None
    started_at: AwareDatetime = Field(default_factory=utc_now)


class ModelCallResult(KernelModel):
    """Content-free terminal evidence for one upstream model attempt."""

    attempt_id: UUID
    invocation_id: UUID
    conversation_id: NonEmptyStr | None = None
    status: ModelCallStatus
    error_kind: str = ""
    retryable: bool = False
    emitted_content: bool = False
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_micros: int | None = Field(default=None, ge=0)
    cost_unknown: bool = True
    completed_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_cost(self) -> Self:
        """Never turn unavailable pricing into a known zero cost."""
        if self.cost_unknown == (self.cost_micros is not None):
            raise ValueError("model call cost value and unknown flag conflict")
        return self


class ModelCallRecord(KernelModel):
    """Queryable route, attempt intent and optional terminal result."""

    route: RouteDecision
    attempt: ModelCallAttempt
    result: ModelCallResult | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        """Require every record part to identify the same attempt."""
        if self.route.attempt_id != self.attempt.attempt_id:
            raise ValueError("route and model attempt identity mismatch")
        if self.route.route_decision_id != self.attempt.route_decision_id:
            raise ValueError("route decision identity mismatch")
        if self.route.invocation_id != self.attempt.invocation_id:
            raise ValueError("route and model invocation mismatch")
        if self.route.context_manifest_id != self.attempt.context_manifest_id:
            raise ValueError("route and model context manifest mismatch")
        if self.result is None:
            return self
        if self.result.attempt_id != self.attempt.attempt_id:
            raise ValueError("model result attempt identity mismatch")
        if self.result.invocation_id != self.attempt.invocation_id:
            raise ValueError("model result invocation mismatch")
        if self.result.conversation_id != self.attempt.conversation_id:
            raise ValueError("model result conversation mismatch")
        return self


class CommandDefinition(KernelModel):
    """Static, provider-owned slash-command catalog entry."""

    command_id: NamespacedId
    provider_id: NamespacedId
    name: NonEmptyStr
    aliases: tuple[NonEmptyStr, ...] = ()
    category: NonEmptyStr = "plugin"
    help_text: str = ""
    protected: bool = False

    @model_validator(mode="after")
    def validate_names(self) -> Self:
        """Require slash-free, case-insensitively unique command names."""
        names = (self.name, *self.aliases)
        normalized = tuple(name.casefold() for name in names)
        if any(
            "/" in name or any(ch.isspace() for ch in name) for name in names
        ):
            raise ValueError("command names cannot contain '/' or whitespace")
        if len(normalized) != len(set(normalized)):
            raise ValueError("command names and aliases must be unique")
        return self


class CommandRequest(KernelModel):
    """Parsed command input passed to one provider session."""

    raw_text: NonEmptyStr
    command_name: NonEmptyStr
    arguments: str = ""


class CommandMessage(KernelModel):
    """Framework-independent assistant response from a command."""

    text: NonEmptyStr
    metadata: JsonObject = Field(default_factory=dict)


class CommandResult(KernelModel):
    """Unambiguous result of command routing or dynamic fallback."""

    disposition: CommandDisposition
    message: CommandMessage | None = None

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        """Require a message only for direct-response outcomes."""
        if self.disposition == CommandDisposition.RESPOND:
            if self.message is None:
                raise ValueError("respond command result requires a message")
        elif self.message is not None:
            raise ValueError("only respond command results may have a message")
        return self


class HookDefinition(KernelModel):
    """Static, provider-owned lifecycle hook catalog entry."""

    hook_id: NamespacedId
    provider_id: NamespacedId
    phase: LifecyclePhase
    priority: int = 100
    order: int = Field(default=0, ge=0)
    before: tuple[NamespacedId, ...] = ()
    after: tuple[NamespacedId, ...] = ()


class HookMessage(KernelModel):
    """Framework-independent short-circuit response from a hook."""

    text: NonEmptyStr
    metadata: JsonObject = Field(default_factory=dict)


class HookOutcome(KernelModel):
    """Unambiguous result of one hook or a complete lifecycle phase."""

    disposition: HookDisposition = HookDisposition.CONTINUE
    message: HookMessage | None = None

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        """Require a message only for short-circuit outcomes."""
        if self.disposition == HookDisposition.SHORT_CIRCUIT:
            if self.message is None:
                raise ValueError(
                    "short-circuit hook outcome requires a message",
                )
        elif self.message is not None:
            raise ValueError(
                "only short-circuit hook outcomes may have a message",
            )
        return self


class StopGateDefinition(KernelModel):
    """Static, provider-owned stop-gate catalog entry."""

    gate_id: NamespacedId
    provider_id: NamespacedId
    priority: int = 100
    scope: str = ""


class StopGateMessage(KernelModel):
    """Framework-independent text message used by a loop decision."""

    text: NonEmptyStr
    metadata: JsonObject = Field(default_factory=dict)


class StopGateInput(KernelModel):
    """Bounded state exposed after one Agent reasoning iteration."""

    iteration: int = Field(ge=0)
    has_tool_calls: bool
    final_message: StopGateMessage | None = None


class StopGateDecision(KernelModel):
    """Framework-independent loop continuation or termination decision."""

    action: StopGateAction = StopGateAction.BYPASS
    continuation_message: str = ""
    reason: str = ""
    continuation_metadata: JsonObject = Field(default_factory=dict)
    final_message: StopGateMessage | None = None
    inject_on_tool_call: bool = False


class ToolDefinition(KernelModel):
    """Callable and governance identity returned by a Tool Provider."""

    function: SkipJsonSchema[Callable[..., object]] = Field(exclude=True)
    name: NonEmptyStr
    tool_type: Literal["file", "network", "shell", "internal"]
    action_kind: ActionKind | None = None
    target_param: str = ""
    pattern_param: str = ""
    policy_name: str = ""
    sandbox_required: bool = False
    effect: ToolEffect = ToolEffect.NONE

    @model_validator(mode="after")
    def validate_function_name(self) -> Self:
        """Keep model-facing and governance identities unambiguous."""
        if getattr(self.function, "__name__", None) != self.name:
            raise ValueError("name must match function.__name__")
        return self


class DriverToolDefinition(KernelModel):
    """Framework-neutral tool whose policy is owned by one Driver."""

    provider_id: NamespacedId
    capability_id: NonEmptyStr
    name: NonEmptyStr
    description: str = ""
    input_schema: JsonObject = Field(default_factory=dict)
    effect: ToolEffect = ToolEffect.EXTERNAL_WRITE
    risk: RiskLevel = RiskLevel.HIGH
    reversible: bool = False
    invoke: SkipJsonSchema[Callable[[JsonObject], Awaitable[object]]] = Field(
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_input_schema(self) -> Self:
        """Require a JSON object schema at the model-facing boundary."""
        schema_type = self.input_schema.get("type")
        if schema_type not in (None, "object"):
            raise ValueError("driver tool input_schema type must be object")
        return self


class DriverApprovalRequest(KernelModel):
    """Bounded request for the host's unified Driver approval path."""

    provider_id: NamespacedId
    capability_id: NonEmptyStr
    tool_name: NonEmptyStr
    operation: NamespacedId = "invoke"
    redacted_arguments: JsonObject = Field(default_factory=dict)


class Proposal(KernelModel):
    """Non-executable suggestion produced by a cognitive component."""

    proposal_id: UUID = Field(default_factory=uuid4)
    source: NamespacedId
    objective: NonEmptyStr
    rationale_summary: NonEmptyStr
    risk: RiskLevel
    requested_capabilities: tuple[NamespacedId, ...] = ()
    execution_contract: ExecutionContract | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_execution_contract(self) -> Self:
        """Keep a proposal and its executable contract on one goal."""
        if (
            self.execution_contract is not None
            and self.execution_contract.goal != self.objective
        ):
            raise ValueError("execution contract goal must match objective")
        return self


class SensorContext(KernelModel):
    """Immutable host context for one generation-pinned Sensor poll."""

    agent_id: NonEmptyStr
    registry_generation: int = Field(ge=1)
    trigger_payload: JsonObject = Field(default_factory=dict)


class ApprovalDisplay(KernelModel):
    """Bounded user-facing description of an approval request."""

    title: NonEmptyStr
    summary: NonEmptyStr
    target: str = ""
    provider: str = ""


class ApprovalRequest(KernelModel):
    """Durable request for a human policy decision."""

    approval_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    run_id: UUID | None = None
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    source: ApprovalSource = ApprovalSource.SYSTEM
    action: NamespacedId
    risk: RiskLevel
    requester: ActorRef
    policy: NamespacedId
    continuation: ApprovalContinuation = ApprovalContinuation.FAIL_ON_REJECTION
    checkpoint_id: UUID | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    expires_at: AwareDatetime | None = None
    redacted_arguments: JsonObject = Field(default_factory=dict)
    display: ApprovalDisplay | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        """Reject approval expiry timestamps before creation."""
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("expires_at cannot precede created_at")
        return self


class ApprovalDecision(KernelModel):
    """Final immutable decision for an approval request."""

    approval_id: UUID
    decision: ApprovalDecisionValue
    actor: ActorRef
    scope: Annotated[
        str,
        StringConstraints(pattern=r"^(exact|similar)$"),
    ] = "exact"
    reason: NonEmptyStr
    decided_at: AwareDatetime = Field(default_factory=utc_now)


class CapabilityDescriptor(KernelModel):
    """Discoverable executable or presentational capability."""

    capability_id: NamespacedId
    slot: NamespacedId
    provider_id: NamespacedId = Field(
        validation_alias=AliasChoices(
            "provider_id",
            "provider_plugin_id",
        ),
    )
    provider_kind: CapabilityProviderKind = CapabilityProviderKind.PLUGIN
    version: NonEmptyStr
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject = Field(default_factory=dict)
    config_schema: JsonObject | None = None
    restart_policy: RestartPolicy = RestartPolicy.HOT
    metadata: JsonObject = Field(default_factory=dict)

    @property
    def provider_plugin_id(self) -> str:
        """Return the legacy provider name used by plugin-only callers."""
        return self.provider_id


class CapabilityContribution(KernelModel):
    """Provider-neutral declaration staged into a capability generation."""

    contribution_id: NamespacedId
    slot: NamespacedId
    entrypoint: NonEmptyStr
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject = Field(default_factory=dict)
    config_schema: JsonObject | None = None
    metadata: JsonObject = Field(default_factory=dict)


class CapabilityBundle(KernelModel):
    """Atomic set of contributions published by one provider."""

    provider_id: NamespacedId
    provider_kind: CapabilityProviderKind
    version: NonEmptyStr
    restart_policy: RestartPolicy = RestartPolicy.HOT
    contributions: tuple[CapabilityContribution, ...]

    @model_validator(mode="after")
    def validate_contribution_ids(self) -> Self:
        """Reject ambiguous capability IDs within one provider bundle."""
        identifiers = [
            contribution.contribution_id for contribution in self.contributions
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("contribution ids must be unique per provider")
        return self


class PluginContribution(KernelModel):
    """One slot contribution declared by a plugin manifest."""

    contribution_id: NamespacedId = Field(
        validation_alias=AliasChoices("contribution_id", "id"),
    )
    slot: NamespacedId
    entrypoint: NonEmptyStr
    config_schema: JsonObject | None = None
    metadata: JsonObject = Field(default_factory=dict)


class TaskOrder(KernelModel):
    """Approved, structured instruction passed to a task runner."""

    order_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    objective: NonEmptyStr
    constraints: tuple[NonEmptyStr, ...] = ()
    acceptance_criteria: tuple[NonEmptyStr, ...] = ()
    requested_capabilities: tuple[NamespacedId, ...] = ()
    approval_ids: tuple[UUID, ...] = ()
    execution_contract: ExecutionContract | None = None
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_execution_contract(self) -> Self:
        """Prevent Task and runner goal/acceptance drift."""
        if self.execution_contract is None:
            return self
        if self.execution_contract.goal != self.objective:
            raise ValueError("execution contract goal must match objective")
        if self.execution_contract.acceptance != self.acceptance_criteria:
            raise ValueError(
                "execution contract acceptance must match order criteria",
            )
        return self


class ExecutionCheckpoint(KernelModel):
    """Safe task boundary from which a new run may resume."""

    checkpoint_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    safe_to_resume: bool
    runner_cursor: JsonValue = None
    workspace_checkpoint_ref: str | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ModelSelection(KernelModel):
    """Provider-neutral model identity fixed for one Runtime attempt."""

    provider_id: NonEmptyStr
    model: NonEmptyStr


class RuntimeLaunchConfig(KernelModel):
    """Typed configuration supplied before a runtime attempt starts."""

    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr | None = None
    project_dir: NonEmptyStr
    ledger_workspace_dir: NonEmptyStr
    approval_level: ApprovalLevel = ApprovalLevel.AGENT_PROFILE
    model_selection: ModelSelection | None = None
    strategy_id: NamespacedId = "qwenpaw.system.tasks.default-strategy"
    tool_provider_ids: tuple[NamespacedId, ...] = (
        "qwenpaw.system.workspace-tools",
    )


class RuntimeStrategyDirective(KernelModel):
    """Validated strategy identity and bounded runner parameters."""

    strategy_id: NamespacedId
    parameters: JsonObject = Field(default_factory=dict)


class RuntimeContext(KernelModel):
    """Immutable public context shared by system and plugin runners."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
        validate_default=True,
        validate_by_alias=True,
        validate_by_name=True,
    )

    task_id: UUID
    run_id: UUID
    invocation_id: UUID
    correlation_id: UUID
    agent_id: NonEmptyStr
    conversation_id: NonEmptyStr | None = None
    session_id: NonEmptyStr
    project_dir: NonEmptyStr
    ledger_workspace_dir: NonEmptyStr
    registry_generation: int = Field(ge=1)
    approval_level: ApprovalLevel = ApprovalLevel.AGENT_PROFILE
    model_selection: ModelSelection | None = None
    execution_contract: ExecutionContract | None = None
    capability_ids: tuple[NamespacedId, ...] = ()
    strategy: RuntimeStrategyDirective | None = None
    tool_provider_ids: tuple[NamespacedId, ...] = ()
    tool_providers: SkipJsonSchema[tuple[object, ...]] = Field(
        default=(),
        exclude=True,
    )
    resume_checkpoint: ExecutionCheckpoint | None = None
    artifact_emitter: SkipJsonSchema[ArtifactEmitter] = Field(exclude=True)
    checkpoint_broker: SkipJsonSchema[CheckpointBroker | None] = Field(
        default=None,
        exclude=True,
    )
    approval_broker: SkipJsonSchema[ApprovalBroker] = Field(exclude=True)
    side_effect_broker: SkipJsonSchema[SideEffectBroker | None] = Field(
        default=None,
        exclude=True,
    )
    usage_meter: SkipJsonSchema[UsageMeter | None] = Field(
        default=None,
        exclude=True,
    )
    cancellation: SkipJsonSchema[CancellationToken] = Field(exclude=True)

    @property
    def strategy_id(self) -> str | None:
        """Return the selected strategy identity for legacy adapters."""
        return self.strategy.strategy_id if self.strategy is not None else None

    @property
    def strategy_parameters(self) -> JsonObject:
        """Return a copy of parameters for legacy runner adapters."""
        if self.strategy is None:
            return {}
        return dict(self.strategy.parameters)


class CausalIdentity(KernelModel):
    """Optional direct-cause identity shared by signals and events."""

    invocation_id: UUID | None = None
    step_id: UUID | None = None
    source: NamespacedId | None = None
    cause_event_id: UUID | None = None
    correlation_id: UUID | None = None


class RunnerSignal(CausalIdentity):
    """Unsequenced runner output converted to a canonical event by runtime."""

    event_type: NamespacedId
    payload: JsonObject = Field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


class IdempotencyRecord(KernelModel):
    """Durable response bound to one mutation request hash."""

    operation: NamespacedId
    key: NonEmptyStr
    request_hash: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]
    response: JsonObject


class SideEffectRecord(KernelModel):
    """Durable fact for an operation that must not be repeated blindly."""

    record_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    run_id: UUID
    invocation_id: UUID | None = None
    correlation_id: UUID | None = None
    approval_id: UUID | None = None
    action: NonEmptyStr
    target: str = ""
    effect: ToolEffect
    idempotency_key: NonEmptyStr
    request_hash: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]
    status: SideEffectStatus = SideEffectStatus.PREPARED
    policy_decision: str = ""
    result_digest: (
        Annotated[
            str,
            StringConstraints(pattern=r"^[0-9a-f]{64}$"),
        ]
        | None
    ) = None
    external_ref: str = ""
    error_code: str = ""
    started_at: AwareDatetime = Field(default_factory=utc_now)
    finished_at: AwareDatetime | None = None
    recovery_actor: ActorRef | None = None
    recovery_reason: str = ""
    recovered_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_terminal_timestamp(self) -> Self:
        """Keep status and terminal timestamps coherent."""
        terminal = self.status is not SideEffectStatus.PREPARED
        if terminal != (self.finished_at is not None):
            raise ValueError("terminal side effect requires finished_at")
        recovered = self.recovered_at is not None
        if recovered != (self.recovery_actor is not None):
            raise ValueError("side-effect recovery requires an actor")
        if recovered != bool(self.recovery_reason):
            raise ValueError("side-effect recovery requires a reason")
        return self


class SideEffectReservation(KernelModel):
    """Result of atomically reserving one side-effect idempotency key."""

    record: SideEffectRecord
    disposition: SideEffectDisposition
