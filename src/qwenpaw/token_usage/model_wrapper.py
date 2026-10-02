# -*- coding: utf-8 -*-
"""Model wrapper that records token usage from LLM responses."""

import asyncio
from datetime import date, datetime, timezone
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, AsyncGenerator, Literal

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from agentscope.model._model_usage import ChatUsage

from ..kernel.models import (
    ModelCallAttempt,
    ModelCallStatus,
    ModelFailureClass,
    ModelOutputBoundary,
    ModelRecoveryDisposition,
    UsageDelta,
    UsageMeter,
)
from ..utils.model_response import safe_attr
from .buffer import _UsageEvent
from .manager import _usage_agent_id, get_token_usage_manager

# AgentScope does not expose provider cache semantics through a public
# capability API. These prefixes therefore depend on its concrete adapter MRO
# module paths. Unknown or renamed paths intentionally fail closed so cache
# metrics disappear instead of being reported with an invalid denominator.
_CACHE_USAGE_MODEL_MODULES = (
    "agentscope.model._anthropic",
    "agentscope.model._dashscope",
    "agentscope.model._deepseek",
    "agentscope.model._gemini",
    "agentscope.model._moonshot",
    "agentscope.model._openai_chat",
    "agentscope.model._openai_response",
    "agentscope.model._xai",
)


@lru_cache(maxsize=32)
def _component_version(module_name: str) -> str | None:
    """Resolve the owning package version without provider imports."""
    package_name = module_name.partition(".")[0]
    if package_name == "qwenpaw":
        from ..__version__ import __version__

        return __version__
    try:
        return version(package_name.replace("_", "-"))
    except PackageNotFoundError:
        return None


def _component_identity(
    component: Any,
) -> tuple[str | None, str | None]:
    """Return a stable qualified class and its owning package version."""
    if component is None:
        return None, None
    component_type = type(component)
    module_name = component_type.__module__
    return (
        f"{module_name}.{component_type.__qualname__}",
        _component_version(module_name),
    )


def _cache_usage_metrics(
    model: Any,
    prompt_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> tuple[bool, int]:
    """Return whether cache usage is supported and its input denominator."""
    modules = {
        cls.__module__
        for cls in type(model).__mro__
        if isinstance(getattr(cls, "__module__", None), str)
    }
    observed = any(
        module.startswith(prefix)
        for module in modules
        for prefix in _CACHE_USAGE_MODEL_MODULES
    )
    if not observed:
        return False, 0
    if any(
        module.startswith("agentscope.model._anthropic") for module in modules
    ):
        return (
            True,
            prompt_tokens + cache_read_tokens + cache_write_tokens,
        )
    if cache_read_tokens + cache_write_tokens > prompt_tokens:
        return False, 0
    return True, prompt_tokens


class TokenRecordingModelWrapper(ChatModelBase):
    """Wraps a ChatModelBase to record token usage on each call."""

    _usage_by_session: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        provider_id: str,
        model: ChatModelBase,
        compact_threshold: float | None = None,
    ) -> None:
        # agentscope 2.0 ChatModelBase requires credential/model/parameters.
        # Forward the wrapped model's own values so the base attributes stay
        # consistent (some downstream code reads ``self.model`` for logging).
        super().__init__(
            credential=getattr(model, "credential", None),
            model=getattr(model, "model", "unknown"),
            parameters=getattr(model, "parameters", None)
            or ChatModelBase.Parameters(),
            stream=getattr(model, "stream", True),
            context_size=getattr(model, "context_size", 32768),
        )
        self._model = model
        # AgentScope 2.0.6 consults ``agent.model.formatter`` before the
        # model call to validate incoming media blocks.  ChatModelBase does
        # not define that attribute itself, so transparent wrappers must
        # preserve the concrete provider model's formatter explicitly.
        formatter = getattr(model, "formatter", None)
        if formatter is not None:
            self.formatter = formatter
        self._provider_id = provider_id
        # Auto-compaction threshold (fraction of the window) for the UI, or
        # None when compaction is disabled/unknown.
        self._compact_threshold = compact_threshold

    @property
    def formatter(self) -> Any:
        """Expose the wrapped model's formatter to AgentScope."""
        return self._model.formatter

    @formatter.setter
    def formatter(self, value: Any) -> None:
        """Keep formatter updates synchronized with the wrapped model."""
        self._model.formatter = value

    def _record_usage(self, usage: ChatUsage | None) -> None:
        """Enqueue a usage event synchronously — never blocks the caller."""
        if usage is None:
            return
        pt = max(int(getattr(usage, "input_tokens", 0) or 0), 0)
        ct = max(int(getattr(usage, "output_tokens", 0) or 0), 0)
        cache_read = max(
            int(getattr(usage, "cache_input_tokens", 0) or 0),
            0,
        )
        cache_write = max(
            int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            0,
        )
        if pt <= 0 and ct <= 0:
            return
        cache_observed, cache_eligible = _cache_usage_metrics(
            self._model,
            pt,
            cache_read,
            cache_write,
        )
        if not cache_observed:
            cache_read = 0
            cache_write = 0

        event = _UsageEvent(
            provider_id=self._provider_id,
            model_name=self.model,
            prompt_tokens=pt,
            completion_tokens=ct,
            date_str=date.today().isoformat(),
            now_iso=datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds",
            ),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cache_eligible_input_tokens=cache_eligible,
            cache_observed=cache_observed,
            agent_id=_usage_agent_id(),
        )
        # Fire-and-forget: synchronous put_nowait, ~100 ns, no await needed.
        get_token_usage_manager().enqueue(event)

        usage_data = {
            "provider_id": self._provider_id,
            "model_name": self.model,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "cache_eligible_input_tokens": cache_eligible,
            "cache_observed": cache_observed,
            "cache_hit_rate": (
                cache_read / cache_eligible * 100
                if cache_eligible > 0
                else None
            ),
            # Context window of the wrapped model, so the UI can show how full
            # the *current* context is (prompt_tokens / context_size), distinct
            # from the cumulative session totals. 0 = unknown.
            "context_size": int(getattr(self._model, "context_size", 0) or 0),
            # Auto-compaction threshold (fraction of the window) so the UI can
            # mark where context gets evicted. None = disabled/unknown.
            "compact_threshold": self._compact_threshold,
        }
        self._store_usage(usage_data)

    async def _record_task_budget(self, usage: ChatUsage | None) -> None:
        """Charge descendant model calls to the root Task usage scope."""
        if usage is None:
            return
        from ..app.agent_context import (
            get_current_task_usage_meter,
            should_record_current_model_usage,
        )

        if not should_record_current_model_usage():
            return
        meter = get_current_task_usage_meter()
        if not isinstance(meter, UsageMeter):
            return
        input_tokens = max(
            int(getattr(usage, "input_tokens", 0) or 0),
            0,
        )
        output_tokens = max(
            int(getattr(usage, "output_tokens", 0) or 0),
            0,
        )
        raw_cost = getattr(usage, "cost_micros", None)
        await meter.record(
            UsageDelta(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=max(int(raw_cost or 0), 0),
                cost_unknown=raw_cost is None,
            ),
            source=f"model:{self._provider_id}",
        )

    async def _begin_model_attempt(self) -> ModelCallAttempt | None:
        """Record the concrete provider route before network dispatch."""
        from ..runtime.model_calls import begin_current_model_attempt

        adapter_id, adapter_version = _component_identity(self._model)
        formatter_id, formatter_version = _component_identity(
            getattr(self._model, "formatter", None),
        )
        return await begin_current_model_attempt(
            provider_id=self._provider_id,
            model_id=str(self.model),
            adapter_id=adapter_id or type(self._model).__qualname__,
            adapter_version=adapter_version,
            formatter_id=formatter_id,
            formatter_version=formatter_version,
        )

    @staticmethod
    async def _complete_model_attempt(
        attempt: ModelCallAttempt | None,
        *,
        status: ModelCallStatus,
        usage: ChatUsage | None = None,
        error: BaseException | None = None,
        emitted_content: bool = False,
        output_boundary: ModelOutputBoundary | None = None,
    ) -> None:
        """Record a content-free result after one concrete attempt."""
        from ..runtime.model_calls import complete_current_model_attempt

        input_tokens = (
            max(int(getattr(usage, "input_tokens", 0) or 0), 0)
            if usage is not None
            else None
        )
        output_tokens = (
            max(int(getattr(usage, "output_tokens", 0) or 0), 0)
            if usage is not None
            else None
        )
        raw_cost = getattr(usage, "cost_micros", None)
        cost_micros = max(int(raw_cost), 0) if raw_cost is not None else None
        error_kind = ""
        retryable = False
        failure_class: ModelFailureClass | None = None
        recovery_disposition: ModelRecoveryDisposition | None = None
        if error is not None:
            from ..providers.model_error_policy import (
                classify_model_error,
                classify_model_recovery,
                is_retryable_same_model,
            )

            recovery = classify_model_recovery(
                error,
                emitted_content=emitted_content,
            )
            failure_class = recovery.failure_class
            recovery_disposition = recovery.disposition
            if isinstance(error, Exception):
                error_kind = classify_model_error(error).kind
                retryable = is_retryable_same_model(error)
            else:
                error_kind = type(error).__name__.lower()
        await complete_current_model_attempt(
            attempt,
            status=status,
            error_kind=error_kind,
            retryable=retryable,
            emitted_content=emitted_content,
            output_boundary=output_boundary,
            failure_class=failure_class,
            recovery_disposition=recovery_disposition,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros=cost_micros,
        )

    @classmethod
    def pop_usage_for_session(cls, session_id: str) -> dict[str, Any] | None:
        return cls._usage_by_session.pop(session_id, None)

    def _store_usage(self, usage: dict[str, Any] | None) -> None:
        from ..app.agent_context import get_current_session_id

        session_id = get_current_session_id()
        if session_id and usage:
            previous = TokenRecordingModelWrapper._usage_by_session.get(
                session_id,
            )
            if previous is None:
                TokenRecordingModelWrapper._usage_by_session[
                    session_id
                ] = usage
                return
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cache_eligible_input_tokens",
            ):
                usage[key] = int(previous.get(key, 0) or 0) + int(
                    usage.get(key, 0) or 0,
                )
            usage["total_tokens"] = (
                usage["prompt_tokens"] + usage["completion_tokens"]
            )
            usage["cache_observed"] = bool(
                previous.get("cache_observed", False)
                or usage.get("cache_observed", False),
            )
            cache_eligible = usage["cache_eligible_input_tokens"]
            usage["cache_hit_rate"] = (
                usage["cache_read_tokens"] / cache_eligible * 100
                if cache_eligible > 0
                else None
            )
            TokenRecordingModelWrapper._usage_by_session[session_id] = usage

    async def generate_structured_output(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        attempt = await self._begin_model_attempt()
        try:
            result = await self._model.generate_structured_output(
                *args,
                **kwargs,
            )
        except asyncio.CancelledError as exc:
            await self._complete_model_attempt(
                attempt,
                status=ModelCallStatus.CANCELLED,
                error=exc,
                output_boundary=ModelOutputBoundary.PRE_OUTPUT,
            )
            raise
        except Exception as exc:
            await self._complete_model_attempt(
                attempt,
                status=ModelCallStatus.FAILED,
                error=exc,
                output_boundary=ModelOutputBoundary.PRE_OUTPUT,
            )
            raise
        self._record_usage(safe_attr(result, "usage"))
        await self._record_task_budget(safe_attr(result, "usage"))
        await self._complete_model_attempt(
            attempt,
            status=ModelCallStatus.SUCCEEDED,
            usage=safe_attr(result, "usage"),
            emitted_content=True,
            output_boundary=ModelOutputBoundary.COMPLETE_RESPONSE,
        )
        return result

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        # agentscope 2.0 routes structured output through
        # ``generate_structured_output`` instead of a ``__call__`` kwarg, and
        # provider SDKs (anthropic, openai) reject unknown kwargs. Drop the
        # 1.x ``structured_model`` if a caller still passes it.
        kwargs.pop("structured_model", None)

        # Fix: Omit tool_choice="auto" for vLLM compatibility
        # vLLM without --enable-auto-tool-choice will reject requests when
        # tool_choice="auto" is present, even if tools are provided.
        # By omitting tool_choice when it's "auto", we bypass the check
        # while keeping tools available for correct tool calling behavior.
        if tool_choice == "auto":
            tool_choice = None

        attempt = await self._begin_model_attempt()
        try:
            result = await self._model(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                **kwargs,
            )
        except asyncio.CancelledError as exc:
            await self._complete_model_attempt(
                attempt,
                status=ModelCallStatus.CANCELLED,
                error=exc,
                output_boundary=ModelOutputBoundary.PRE_OUTPUT,
            )
            raise
        except Exception as exc:
            await self._complete_model_attempt(
                attempt,
                status=ModelCallStatus.FAILED,
                error=exc,
                output_boundary=ModelOutputBoundary.PRE_OUTPUT,
            )
            raise

        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(result, attempt)
        self._record_usage(safe_attr(result, "usage"))
        await self._record_task_budget(safe_attr(result, "usage"))
        await self._complete_model_attempt(
            attempt,
            status=ModelCallStatus.SUCCEEDED,
            usage=safe_attr(result, "usage"),
            emitted_content=True,
            output_boundary=ModelOutputBoundary.COMPLETE_RESPONSE,
        )
        return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator[ChatResponse, None],
        attempt: ModelCallAttempt | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        from ..providers.stream_progress import (
            has_meaningful_stream_content,
        )

        last_usage: ChatUsage | None = None
        emitted_content = False
        terminal_chunk_seen = False
        try:
            async for chunk in stream:
                usage = safe_attr(chunk, "usage")
                if usage is not None:
                    last_usage = usage
                emitted_content = emitted_content or (
                    has_meaningful_stream_content(chunk.content)
                )
                terminal_chunk_seen = terminal_chunk_seen or bool(
                    safe_attr(chunk, "is_last"),
                )
                yield chunk
        except asyncio.CancelledError as exc:
            await self._complete_model_attempt(
                attempt,
                status=ModelCallStatus.CANCELLED,
                usage=last_usage,
                error=exc,
                emitted_content=emitted_content,
                output_boundary=(
                    ModelOutputBoundary.PARTIAL_STREAM
                    if emitted_content
                    else ModelOutputBoundary.PRE_OUTPUT
                ),
            )
            raise
        except GeneratorExit as exc:
            await self._complete_model_attempt(
                attempt,
                status=(
                    ModelCallStatus.SUCCEEDED
                    if terminal_chunk_seen
                    else ModelCallStatus.CANCELLED
                ),
                usage=last_usage,
                error=None if terminal_chunk_seen else exc,
                emitted_content=emitted_content,
                output_boundary=(
                    ModelOutputBoundary.TERMINAL_STREAM
                    if terminal_chunk_seen
                    else ModelOutputBoundary.PARTIAL_STREAM
                    if emitted_content
                    else ModelOutputBoundary.PRE_OUTPUT
                ),
            )
            raise
        except Exception as exc:
            await self._complete_model_attempt(
                attempt,
                status=ModelCallStatus.FAILED,
                usage=last_usage,
                error=exc,
                emitted_content=emitted_content,
                output_boundary=(
                    ModelOutputBoundary.PARTIAL_STREAM
                    if emitted_content
                    else ModelOutputBoundary.PRE_OUTPUT
                ),
            )
            raise
        else:
            await self._complete_model_attempt(
                attempt,
                status=ModelCallStatus.SUCCEEDED,
                usage=last_usage,
                emitted_content=emitted_content,
                output_boundary=(
                    ModelOutputBoundary.TERMINAL_STREAM
                    if terminal_chunk_seen
                    else ModelOutputBoundary.CLEAN_STREAM_END
                ),
            )
        finally:
            await stream.aclose()
            self._record_usage(last_usage)
            await self._record_task_budget(last_usage)
