# -*- coding: utf-8 -*-
"""Small adapters for running local tasks behind the kernel contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

from pydantic import TypeAdapter

from ..kernel import BudgetAllocation
from ..kernel.models import (
    CostAccountingMode,
    ExecutionBudget,
    ExitCondition,
    JsonObject,
    Run,
    RunnerPreflightRequest,
    RunnerPreflightResult,
    RunnerSignal,
    RuntimeContext,
    RuntimeStrategyDirective,
    TaskOrder,
    UsageDelta,
)
from ..kernel.ports import (
    CapabilityLease,
    CapabilityResolver,
    ContextualTaskRunner,
    ExecutableCapabilityLease,
    RuntimeStrategy,
    TaskRunner,
)
from .context import runtime_context_from_order
from .approval_broker import TaskApprovalBroker
from .cancellation import RuntimeCancellationToken
from .checkpoints import TaskCheckpointBroker
from .results import (
    CompletionRequirementsError,
    load_task_result_projection,
    satisfied_result_exit_condition_ids,
    validate_result_projection,
)
from .service import TaskService
from .usage import (
    TaskUsageMeter,
    UsageAccountingUnavailableError,
    UsageBudgetExceededError,
)
from .budget_leases import (
    BudgetAllocationExceededError,
    BudgetLeaseUnavailableError,
    open_root_budget_lease,
)

LocalExecution = Callable[
    [TaskOrder, Run],
    AsyncIterator[RunnerSignal],
]
ContextualExecution = Callable[
    [TaskOrder, Run, RuntimeContext],
    AsyncIterator[RunnerSignal],
]

_MAX_STRATEGY_PARAMETERS_BYTES = 32 * 1024
_STRATEGY_PARAMETERS_ADAPTER = TypeAdapter(JsonObject)
_RUNNER_SLOTS = frozenset({"runner", "harness.runner"})


class TaskExecutionTimeoutError(TimeoutError):
    """Raised when a runner does not reach a terminal boundary in time."""


class TaskExecutionBudgetExceededError(TaskExecutionTimeoutError):
    """Raised when the Execution Contract duration budget is exhausted."""


class RunnerCapabilityUnavailableError(LookupError):
    """Raised when a pinned generation cannot supply a requested runner."""

    def __init__(self, capability_id: str, reason: str) -> None:
        self.capability_id = capability_id
        self.reason = reason
        super().__init__(
            f"runner capability '{capability_id}' is unavailable: {reason}",
        )


class LocalAgentRunner:
    """Adapt an async signal stream to the stable ``TaskRunner`` port.

    An integration supplies one async generator and does not need to know
    about event sequencing, persistence, retries, or task projections.
    """

    def __init__(
        self,
        runner_id: str,
        execute: LocalExecution | None = None,
        *,
        execute_context: ContextualExecution | None = None,
        cost_accounting: CostAccountingMode = CostAccountingMode.UNKNOWN,
    ) -> None:
        if not runner_id.strip():
            raise ValueError("runner_id cannot be empty")
        if execute is None and execute_context is None:
            raise ValueError("one execution callback is required")
        self._runner_id = runner_id
        self._execute = execute
        self._execute_context = execute_context
        self._cost_accounting = CostAccountingMode(cost_accounting)

    @property
    def runner_id(self) -> str:
        """Return the stable local runner capability ID."""
        return self._runner_id

    @property
    def cost_accounting(self) -> CostAccountingMode:
        """Declare whether this Runner can enforce monetary ceilings."""
        return self._cost_accounting

    async def preflight(
        self,
        request: RunnerPreflightRequest,
    ) -> RunnerPreflightResult:
        """Describe the local adapter without invoking its callback."""
        if request.runner_id != self.runner_id:
            raise ValueError("runner preflight identity mismatch")
        return RunnerPreflightResult(
            runner_id=self.runner_id,
            slot=request.slot,
            registry_generation=request.registry_generation,
            contextual=True,
            cost_accounting=self.cost_accounting,
        )

    async def execute(
        self,
        order: TaskOrder,
        run: Run,
    ) -> AsyncIterator[RunnerSignal]:
        """Yield signals while enforcing task identity at the boundary."""
        if order.task_id != run.task_id:
            raise ValueError("task order and run must reference one task")
        if self._execute is not None:
            signals = self._execute(order, run)
        else:
            execute_context = self._execute_context
            if execute_context is None:  # pragma: no cover - constructor guard
                raise RuntimeError("runner has no execution callback")
            context = runtime_context_from_order(order, run)
            signals = execute_context(order, run, context)
        async for signal in signals:
            yield signal

    async def execute_context(
        self,
        order: TaskOrder,
        run: Run,
        context: RuntimeContext,
    ) -> AsyncIterator[RunnerSignal]:
        """Prefer a contextual callback and adapt legacy callbacks."""
        if order.task_id != run.task_id or context.run_id != run.run_id:
            raise ValueError("runtime context does not match the active run")
        if self._execute_context is not None:
            signals = self._execute_context(order, run, context)
        else:
            execute = self._execute
            if execute is None:  # pragma: no cover - constructor guard
                raise RuntimeError("runner has no execution callback")
            signals = execute(order, run)
        async for signal in signals:
            yield signal


class TaskExecutionCoordinator:
    """Own lifecycle bookkeeping around any conforming task runner."""

    def __init__(
        self,
        service: TaskService,
        capability_resolver: CapabilityResolver,
    ) -> None:
        self._service = service
        self._capability_resolver = capability_resolver

    @staticmethod
    def _execution_timeout(
        order: TaskOrder,
        requested: float | None,
    ) -> tuple[float | None, str | None]:
        """Select the strictest host or Execution Contract deadline."""
        candidates: list[tuple[float, str]] = []
        if requested is not None:
            candidates.append((requested, "host"))
        contract = order.execution_contract
        if contract is not None:
            max_duration = getattr(
                contract.budget,
                "max_duration_seconds",
            )
            if max_duration is not None:
                candidates.append((max_duration, "budget"))
            attempt_timeout = getattr(
                contract.timeout_policy,
                "attempt_seconds",
            )
            if attempt_timeout is not None:
                candidates.append((attempt_timeout, "attempt"))
        if not candidates:
            return None, None
        timeout = min(value for value, _ in candidates)
        reasons = {reason for value, reason in candidates if value == timeout}
        if "budget" in reasons:
            return timeout, "budget"
        if "attempt" in reasons:
            return timeout, "attempt"
        return timeout, "host"

    async def execute(
        self,
        order: TaskOrder,
        runner: TaskRunner,
        *,
        timeout_seconds: float | None = None,
    ) -> Run:
        """Start, persist signals, and finish one task attempt."""
        _, execution, _ = await self.start(
            order,
            runner,
            timeout_seconds=timeout_seconds,
        )
        return await execution

    async def execute_selected(
        self,
        order: TaskOrder,
        capability_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Run:
        """Resolve and execute a contributed runner in one pinned lease."""
        _, execution, _ = await self.start_selected(
            order,
            capability_id,
            timeout_seconds=timeout_seconds,
        )
        return await execution

    async def start(
        self,
        order: TaskOrder,
        runner: TaskRunner,
        *,
        strategy: RuntimeStrategy | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[Run, asyncio.Task[Run], RuntimeCancellationToken]:
        """Start a run and return its supervised background execution."""
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        lease = await self._capability_resolver.pin()
        return await self._start_pinned(
            order,
            runner,
            lease,
            strategy=strategy,
            timeout_seconds=timeout_seconds,
        )

    async def start_selected(
        self,
        order: TaskOrder,
        capability_id: str,
        *,
        strategy: RuntimeStrategy | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[Run, asyncio.Task[Run], RuntimeCancellationToken]:
        """Start a contributed runner resolved from one pinned generation."""
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        lease = await self._capability_resolver.pin()
        try:
            runner = self.resolve_runner(lease, capability_id)
        except Exception:
            await lease.close()
            raise
        return await self._start_pinned(
            order,
            runner,
            lease,
            strategy=strategy,
            timeout_seconds=timeout_seconds,
        )

    async def _start_pinned(
        self,
        order: TaskOrder,
        runner: TaskRunner,
        lease: CapabilityLease,
        *,
        strategy: RuntimeStrategy | None,
        timeout_seconds: float | None,
    ) -> tuple[Run, asyncio.Task[Run], RuntimeCancellationToken]:
        """Start one runner while retaining its already-pinned generation."""
        run: Run | None = None
        try:
            _, run = await self._service.start_task(
                order.task_id,
                runner_id=runner.runner_id,
                strategy_id=(strategy.strategy_id if strategy else None),
                registry_generation=lease.generation,
            )
        finally:
            if run is None:
                await lease.close()
        assert run is not None
        cancellation = RuntimeCancellationToken()
        execution = asyncio.create_task(
            self._drive(
                order,
                runner,
                run,
                lease,
                cancellation,
                strategy,
                timeout_seconds=timeout_seconds,
            ),
        )
        return run, execution, cancellation

    async def start_pinned(
        self,
        order: TaskOrder,
        runner: TaskRunner,
        lease: CapabilityLease,
        *,
        strategy: RuntimeStrategy | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[Run, asyncio.Task[Run], RuntimeCancellationToken]:
        """Start using a lease already pinned by the orchestrator."""
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        return await self._start_pinned(
            order,
            runner,
            lease,
            strategy=strategy,
            timeout_seconds=timeout_seconds,
        )

    async def resume_pinned(
        self,
        order: TaskOrder,
        runner: TaskRunner,
        lease: CapabilityLease,
        *,
        strategy: RuntimeStrategy | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[
        Run,
        asyncio.Task[Run] | None,
        RuntimeCancellationToken | None,
    ]:
        """Resume a checkpoint with an already-pinned runner generation."""
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        run: Run | None = None
        try:
            self._service.set_registry_generation(lease.generation)
            outcome = await self._service.resume_task_with_outcome(
                order.task_id,
                strategy_id=(strategy.strategy_id if strategy else None),
                idempotency_key=idempotency_key,
            )
            run = outcome.run
            if run is None:
                raise RuntimeError("resumed task has no active run")
            if run.runner_id != runner.runner_id:
                raise RunnerCapabilityUnavailableError(
                    runner.runner_id,
                    "checkpoint runner does not match resolved capability",
                )
            if outcome.replayed:
                await lease.close()
                return run, None, None
        finally:
            if run is None:
                await lease.close()
        assert run is not None
        cancellation = RuntimeCancellationToken()
        execution = asyncio.create_task(
            self._drive(
                order,
                runner,
                run,
                lease,
                cancellation,
                strategy,
                timeout_seconds=timeout_seconds,
            ),
        )
        return run, execution, cancellation

    @staticmethod
    def resolve_runner(
        lease: CapabilityLease,
        capability_id: str,
    ) -> TaskRunner:
        """Validate a runner descriptor and implementation as one unit."""
        descriptor = lease.resolve(capability_id)
        if descriptor is None:
            raise RunnerCapabilityUnavailableError(
                capability_id,
                "not present in the pinned registry generation",
            )
        if descriptor.slot not in _RUNNER_SLOTS:
            raise RunnerCapabilityUnavailableError(
                capability_id,
                f"declares non-runner slot '{descriptor.slot}'",
            )
        if not isinstance(lease, ExecutableCapabilityLease):
            raise RunnerCapabilityUnavailableError(
                capability_id,
                "resolver does not expose executable implementations",
            )
        implementation = lease.implementation(capability_id)
        if not isinstance(implementation, TaskRunner):
            raise RunnerCapabilityUnavailableError(
                capability_id,
                "implementation does not satisfy TaskRunner",
            )
        if implementation.runner_id != capability_id:
            raise RunnerCapabilityUnavailableError(
                capability_id,
                "implementation runner_id does not match capability ID",
            )
        return implementation

    async def _drive(
        self,
        order: TaskOrder,
        runner: TaskRunner,
        run: Run,
        lease,
        cancellation: RuntimeCancellationToken,
        strategy: RuntimeStrategy | None,
        *,
        timeout_seconds: float | None,
    ) -> Run:
        """Drive one already-started run and always release its lease."""
        effective_timeout, timeout_source = self._execution_timeout(
            order,
            timeout_seconds,
        )
        try:
            try:
                if effective_timeout is None:
                    await self._record_signals(
                        order,
                        runner,
                        run,
                        cancellation,
                        strategy,
                    )
                else:
                    try:
                        async with asyncio.timeout(effective_timeout):
                            await self._record_signals(
                                order,
                                runner,
                                run,
                                cancellation,
                                strategy,
                            )
                    except TimeoutError as exc:
                        if timeout_source == "budget":
                            raise TaskExecutionBudgetExceededError(
                                "task execution exhausted its duration "
                                "budget",
                            ) from exc
                        raise TaskExecutionTimeoutError(
                            "task execution exceeded its attempt timeout",
                        ) from exc
                cancellation.raise_if_cancelled()
                await self._service.complete_task(order.task_id)
            except Exception as exc:
                await self._service.fail_task(
                    order.task_id,
                    error_summary=type(exc).__name__,
                )
                raise
            stored = await self._service.list_runs(order.task_id)
            return stored[-1]
        finally:
            await lease.close()

    # pylint: disable-next=too-many-branches,too-many-statements
    async def _record_signals(
        self,
        order: TaskOrder,
        runner: TaskRunner,
        run: Run,
        cancellation: RuntimeCancellationToken,
        strategy: RuntimeStrategy | None,
    ) -> None:
        """Persist one runner stream until it completes or is cancelled."""
        budget = (
            order.execution_contract.budget
            if order.execution_contract is not None
            else ExecutionBudget()
        )
        durable_usage_meter = TaskUsageMeter(
            self._service,
            order.task_id,
            run.run_id,
            budget,
        )
        iteration_limits = tuple(
            condition.parameters["limit"]
            for condition in (
                order.execution_contract.exit_conditions
                if order.execution_contract is not None
                else ()
            )
            if condition.kind == "max_iterations"
        )
        iteration_limit = min(iteration_limits) if iteration_limits else None
        optional_conditions = tuple(
            condition
            for condition in (
                order.execution_contract.exit_conditions
                if order.execution_contract is not None
                else ()
            )
            if not condition.required and condition.kind != "max_iterations"
        )
        observed_iteration = 0
        iteration_events = 0
        context = runtime_context_from_order(
            order,
            run,
            approval_broker=TaskApprovalBroker(
                service=self._service,
                task_id=order.task_id,
                run_id=run.run_id,
            ),
            checkpoint_broker=TaskCheckpointBroker(
                service=self._service,
                task_id=order.task_id,
                run_id=run.run_id,
            ),
            cancellation=cancellation,
            usage_meter=durable_usage_meter,
            strategy_id=(strategy.strategy_id if strategy else None),
        )
        budget_lease = await open_root_budget_lease(
            durable_usage_meter,
            BudgetAllocation.from_execution_budget(budget),
            owner_id=context.agent_id,
            task_id=order.task_id,
            run_id=run.run_id,
        )
        usage_meter = budget_lease
        context = context.model_copy(update={"usage_meter": usage_meter})
        try:
            if (
                budget.max_cost_micros is not None
                and self._cost_accounting_mode(runner)
                is CostAccountingMode.UNKNOWN
            ):
                await usage_meter.record(
                    UsageDelta(cost_unknown=True),
                    source=runner.runner_id,
                )
            if strategy is not None:
                raw_parameters = await strategy.prepare(context, order)
                parameters = _STRATEGY_PARAMETERS_ADAPTER.validate_python(
                    raw_parameters,
                )
                encoded_parameters = json.dumps(
                    parameters,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                if len(encoded_parameters) > _MAX_STRATEGY_PARAMETERS_BYTES:
                    raise ValueError(
                        "strategy parameters exceed 32 KiB",
                    )
                context = context.model_copy(
                    update={
                        "strategy": RuntimeStrategyDirective(
                            strategy_id=strategy.strategy_id,
                            parameters=parameters,
                        ),
                    },
                )
            if isinstance(runner, ContextualTaskRunner):
                signals = runner.execute_context(order, run, context)
            else:
                signals = runner.execute(order, run)
            async for signal in signals:
                if signal.event_type == "runner.iteration":
                    iteration_events += 1
                    raw_iteration = signal.payload.get("iteration")
                    if (
                        isinstance(raw_iteration, int)
                        and not isinstance(raw_iteration, bool)
                        and raw_iteration > 0
                    ):
                        observed_iteration = max(
                            observed_iteration,
                            raw_iteration,
                            iteration_events,
                        )
                    else:
                        observed_iteration = iteration_events
                    if (
                        iteration_limit is not None
                        and observed_iteration > iteration_limit
                    ):
                        raise TaskExecutionBudgetExceededError(
                            "task execution exceeded max_iterations "
                            f"condition ({iteration_limit})",
                        )
                if signal.event_type == "usage.recorded":
                    raw_delta = signal.payload.get("delta", signal.payload)
                    if not isinstance(raw_delta, dict):
                        raise ValueError(
                            "usage signal delta must be an object",
                        )
                    await usage_meter.record(
                        UsageDelta.model_validate(raw_delta),
                        source=signal.source or runner.runner_id,
                    )
                    continue
                if (
                    signal.event_type == "tool.started"
                    and signal.payload.get("usage_accounted") is not True
                ):
                    await usage_meter.record(
                        UsageDelta(tool_calls=1),
                        source=signal.source or runner.runner_id,
                    )
                signal = signal.model_copy(
                    update={
                        "invocation_id": (context.invocation_id),
                        "correlation_id": (
                            signal.correlation_id or context.correlation_id
                        ),
                    },
                )
                event = await self._service.record_runner_signal(
                    order.task_id,
                    run.run_id,
                    signal,
                )
                if optional_conditions and self._changes_result_projection(
                    signal,
                ):
                    triggered = await self._optional_exit_conditions(
                        order,
                        run,
                        optional_conditions,
                    )
                    if triggered:
                        await self._service.record_exit_condition_triggered(
                            order.task_id,
                            run.run_id,
                            condition_ids=triggered,
                            cause_event_id=event.event_id,
                        )
                        close = getattr(signals, "aclose", None)
                        if close is not None:
                            await close()
                        break
            # Descendant Agent calls record directly through the shared
            # UsageMeter. Recheck after the root stream ends so a child that
            # crossed a ceiling cannot be reduced to a recoverable tool error
            # while the root Task is still marked completed.
            await usage_meter.assert_within_budget()
        except (
            BudgetAllocationExceededError,
            BudgetLeaseUnavailableError,
            UsageAccountingUnavailableError,
            UsageBudgetExceededError,
        ) as exc:
            raise TaskExecutionBudgetExceededError(str(exc)) from exc
        finally:
            budget_lease.release()

    @staticmethod
    def _changes_result_projection(signal: RunnerSignal) -> bool:
        """Return whether a signal may satisfy a result-bound condition."""
        return bool(
            signal.artifact_refs
            or signal.evidence_refs
            or signal.event_type
            in {"verification.completed", "exit_condition.met"},
        )

    async def _optional_exit_conditions(
        self,
        order: TaskOrder,
        run: Run,
        optional_conditions: tuple[ExitCondition, ...],
    ) -> tuple[str, ...]:
        """Return optional conditions that can safely end the stream now."""
        contract = order.execution_contract
        if contract is None:  # pragma: no cover - caller requires conditions
            return ()
        task = await self._service.get_task(order.task_id)
        if task is None:  # pragma: no cover - active run owns the task
            return ()
        projection = await load_task_result_projection(
            self._service,
            order.task_id,
        )
        try:
            package = validate_result_projection(task, run, projection)
        except CompletionRequirementsError:
            return ()
        satisfied = satisfied_result_exit_condition_ids(
            task,
            projection,
            package,
            contract,
            required=False,
        )
        optional_ids = {
            condition.condition_id for condition in optional_conditions
        }
        return tuple(sorted(satisfied & optional_ids))

    @staticmethod
    def _cost_accounting_mode(runner: TaskRunner) -> CostAccountingMode:
        """Read the optional cost contract; legacy Runners are unknown."""
        raw_mode = getattr(
            runner,
            "cost_accounting",
            CostAccountingMode.UNKNOWN,
        )
        try:
            return CostAccountingMode(raw_mode)
        except ValueError:
            return CostAccountingMode.UNKNOWN
