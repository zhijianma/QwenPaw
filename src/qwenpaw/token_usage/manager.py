# -*- coding: utf-8 -*-
"""Token usage manager — thin orchestrator."""

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..constant import WORKING_DIR, TOKEN_USAGE_FILE
from ..kernel import ModelCallAttempt, ModelCallRecord, ModelCallResult
from .aggregation import summarize_usage
from .buffer import TokenUsageBuffer, _UsageEvent
from .compatibility import merge_cutover_usage, query_legacy_usage
from .models import (
    TokenUsageByAgent,
    TokenUsageByChat,
    TokenUsageByConversation,
    TokenUsageByDateModel,
    TokenUsageByModel,
    TokenUsageByTurn,
    TokenUsageRecord,
    TokenUsageScopeRows,
    TokenUsageStats,
    TokenUsageSummary,
)

if TYPE_CHECKING:
    from .projection import UsageProjectionStatus

logger = logging.getLogger(__name__)


def _usage_agent_id() -> str:
    """ContextVar agent id only; empty when unset."""
    try:
        from ..app.agent_context import peek_current_agent_id

        return peek_current_agent_id()
    except Exception:
        logger.warning(
            "token_usage: failed to read agent id",
            exc_info=True,
        )
        return ""


def _canonical_chat_id(
    chat_id: str | None,
    conversation_id: str | None,
) -> str | None:
    """Resolve ChatSpec.id while accepting the deprecated keyword."""
    if chat_id and conversation_id and chat_id != conversation_id:
        raise ValueError("chat_id and conversation_id must identify one Chat")
    return chat_id or conversation_id


class TokenUsageManager:
    """Orchestrator for token usage recording and querying."""

    _instance: "TokenUsageManager | None" = None
    _lock = threading.Lock()

    def __init__(
        self,
        *,
        projection_path: Path | None = None,
        projection_cutover_date: date | None = None,
    ) -> None:
        from .projection import LiteUsageProjection

        path: Path = (WORKING_DIR / TOKEN_USAGE_FILE).expanduser()
        self._buffer = TokenUsageBuffer(path)
        self._projection = LiteUsageProjection(
            projection_path
            or (WORKING_DIR / "token_usage_projection.sqlite3").expanduser(),
            initial_cutover_date=projection_cutover_date,
        )
        self._flush_interval = 10  # default

    async def project_model_call(
        self,
        attempt: ModelCallAttempt,
        result: ModelCallResult,
    ) -> bool:
        """Index one fact and return whether legacy JSON should also record."""
        try:
            cutover = await self._projection.record(attempt, result)
        except Exception:
            logger.exception(
                "token_usage: model-call projection failed attempt=%s",
                attempt.attempt_id,
            )
            # Never create an aggregated fallback row that cannot later be
            # deduplicated from the authoritative attempt during rebuild.
            return False
        completed_date = result.completed_at.astimezone(timezone.utc).date()
        return completed_date < cutover

    async def get_projection_status(self) -> "UsageProjectionStatus":
        """Return content-free state for reconciliation diagnostics."""
        return await self._projection.status()

    async def rebuild_projection(
        self,
        records: list[ModelCallRecord],
    ) -> int:
        """Replace the disposable index from authoritative Model Calls."""
        return await self._projection.rebuild(records)

    def start(self, flush_interval: int = 10) -> None:
        """Start background flush task.

        Must be called from an async context (e.g. app lifespan startup).
        ``flush_interval`` is the number of seconds between flushes.
        """
        self._flush_interval = flush_interval
        # Recreate buffer with desired flush_interval if different from default
        if flush_interval != 10:
            path: Path = (WORKING_DIR / TOKEN_USAGE_FILE).expanduser()
            self._buffer = TokenUsageBuffer(
                path,
                flush_interval=flush_interval,
            )
        self._buffer.start()

    async def stop(self) -> None:
        """Stop the flush task and perform a final flush before exit."""
        await self._buffer.stop()

    def enqueue(self, event: _UsageEvent) -> None:
        """Synchronous fire-and-forget — enqueue a pre-built usage event.

        Called directly from ``TokenRecordingModelWrapper._record_usage()``
        on the hot path. No ``await`` required.
        """
        self._buffer.enqueue(event)

    async def record(
        self,
        provider_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        at_date: Optional[date] = None,
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_eligible_input_tokens: int = 0,
        cache_observed: bool = False,
        agent_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """Record token usage for a given provider, model and date.

        Convenience async wrapper around ``enqueue()`` for callers that
        prefer the original async interface (e.g. tests, skill tools).

        Args:
            provider_id: ID of the provider (e.g. "dashscope", "openai").
            model_name: Name of the model (e.g. "qwen3-max", "gpt-4").
            prompt_tokens: Number of input/prompt tokens.
            completion_tokens: Number of output/completion tokens.
            at_date: Date to record under. Defaults to today in UTC.
            cache_read_tokens: Number of prompt tokens read from cache.
            cache_write_tokens: Number of prompt tokens written to cache.
            cache_eligible_input_tokens: Normalized cache-rate denominator.
            cache_observed: Whether the adapter reports cache usage.
            chat_id: Owning ChatSpec.id.
            conversation_id: Deprecated compatibility alias for chat_id.
        """
        canonical_chat_id = _canonical_chat_id(chat_id, conversation_id)
        observed_at = datetime.now(tz=timezone.utc)
        if at_date is None:
            at_date = observed_at.date()
        self._buffer.enqueue(
            _UsageEvent(
                provider_id=provider_id,
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                date_str=at_date.isoformat(),
                now_iso=observed_at.isoformat(
                    timespec="seconds",
                ),
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_eligible_input_tokens=cache_eligible_input_tokens,
                cache_observed=cache_observed,
                agent_id=(
                    agent_id if agent_id is not None else _usage_agent_id()
                ),
                conversation_id=canonical_chat_id or "",
                turn_id=turn_id or "",
            ),
        )

    async def get_summary(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        *,
        conversation_id: Optional[str] = None,
    ) -> TokenUsageSummary:
        """Get aggregated token usage summary.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            model_name: Optional model name filter.
            provider_id: Optional provider ID filter.
            chat_id: Optional ChatSpec.id filter.
            conversation_id: Deprecated compatibility alias for chat_id.

        Returns:
            TokenUsageSummary with totals, by_model, by_provider, by_date.
        """
        if end_date is None:
            end_date = datetime.now(tz=timezone.utc).date()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        canonical_chat_id = _canonical_chat_id(chat_id, conversation_id)
        records = await self._get_records(
            start_date,
            end_date,
            model_name,
            provider_id,
            agent_id,
            canonical_chat_id,
            turn_id,
        )

        return summarize_usage(records)

    async def get_details(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        chat_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        *,
        conversation_id: Optional[str] = None,
    ) -> list[TokenUsageRecord]:
        """Get raw token usage records for frontend aggregation.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            model_name: Optional model name filter.
            provider_id: Optional provider ID filter.
            chat_id: Optional ChatSpec.id filter.
            conversation_id: Deprecated compatibility alias for chat_id.

        Returns:
            List of TokenUsageRecord. With agent tracking, a row is
            (date, agent, provider, model).
        """
        if end_date is None:
            end_date = datetime.now(tz=timezone.utc).date()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        canonical_chat_id = _canonical_chat_id(chat_id, conversation_id)
        return await self._get_records(
            start_date,
            end_date,
            model_name,
            provider_id,
            agent_id,
            canonical_chat_id,
            turn_id,
        )

    async def _get_records(
        self,
        start_date: date,
        end_date: date,
        model_name: Optional[str],
        provider_id: Optional[str],
        agent_id: Optional[str],
        chat_id: Optional[str],
        turn_id: Optional[str],
    ) -> list[TokenUsageRecord]:
        """Merge pre-cutover compatibility rows with fact projections."""
        merged = await self._buffer.get_merged_data()
        legacy = query_legacy_usage(
            merged,
            start_date,
            end_date,
            model_name=model_name,
            provider_id=provider_id,
            agent_id=agent_id,
            conversation_id=chat_id,
            turn_id=turn_id,
        )
        cutover, projected = await self._projection.query(
            start_date,
            end_date,
            model_name=model_name,
            provider_id=provider_id,
            agent_id=agent_id,
            conversation_id=chat_id,
            turn_id=turn_id,
            include_shadow=True,
        )
        return merge_cutover_usage(legacy, projected, cutover)

    @classmethod
    def get_instance(cls) -> "TokenUsageManager":
        """Return the process-wide singleton ``TokenUsageManager``."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance


def get_token_usage_manager() -> TokenUsageManager:
    """Return the process-wide singleton ``TokenUsageManager``."""
    return TokenUsageManager.get_instance()


__all__ = [
    "TokenUsageByAgent",
    "TokenUsageByChat",
    "TokenUsageByConversation",
    "TokenUsageByDateModel",
    "TokenUsageByModel",
    "TokenUsageByTurn",
    "TokenUsageManager",
    "TokenUsageRecord",
    "TokenUsageScopeRows",
    "TokenUsageStats",
    "TokenUsageSummary",
    "get_token_usage_manager",
]
