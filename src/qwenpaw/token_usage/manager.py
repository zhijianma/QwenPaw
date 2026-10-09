# -*- coding: utf-8 -*-
"""Token usage manager — thin orchestrator."""

import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..constant import WORKING_DIR, TOKEN_USAGE_FILE
from ..kernel import ModelCallAttempt, ModelCallRecord, ModelCallResult
from .buffer import TokenUsageBuffer, _UsageEvent
from .models import (
    TokenUsageByAgent,
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


_UNATTRIBUTED_SCOPE = "__unattributed__"


def _scope_key(*parts: str | None) -> str:
    """Build a collision-free public key for one ownership scope."""
    normalized = [part or _UNATTRIBUTED_SCOPE for part in parts]
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _new_stats(**identity: str | None) -> dict:
    """Return a mutable aggregate with optional identity fields."""
    return {
        **identity,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_eligible_input_tokens": 0,
        "cache_observed_calls": 0,
        "context_input_tokens": 0,
        "context_window_tokens": 0,
        "context_observed_calls": 0,
        "near_compaction_calls": 0,
        "cost_micros": 0,
        "cost_unknown_calls": 0,
        "context_usage_ratio": None,
        "max_context_usage_ratio": None,
        "usage_observed_calls": 0,
        "usage_unobserved_calls": 0,
        "call_count": 0,
    }


def _add_stats(target: dict, record: TokenUsageRecord) -> None:
    """Accumulate one immutable usage record into a mutable aggregate."""
    target["prompt_tokens"] += record.prompt_tokens
    target["completion_tokens"] += record.completion_tokens
    target["cache_read_tokens"] += record.cache_read_tokens
    target["cache_write_tokens"] += record.cache_write_tokens
    target["cache_eligible_input_tokens"] += record.cache_eligible_input_tokens
    target["cache_observed_calls"] += record.cache_observed_calls
    target["context_input_tokens"] += record.context_input_tokens
    target["context_window_tokens"] += record.context_window_tokens
    target["context_observed_calls"] += record.context_observed_calls
    target["near_compaction_calls"] += record.near_compaction_calls
    target["cost_micros"] += record.cost_micros
    target["cost_unknown_calls"] += record.cost_unknown_calls
    target["context_usage_ratio"] = (
        target["context_input_tokens"] / target["context_window_tokens"] * 100
        if target["context_window_tokens"] > 0
        else None
    )
    maxima = (
        target.get("max_context_usage_ratio"),
        record.max_context_usage_ratio,
    )
    target["max_context_usage_ratio"] = (
        max(value for value in maxima if value is not None)
        if any(value is not None for value in maxima)
        else None
    )
    target["usage_observed_calls"] += record.usage_observed_calls
    target["usage_unobserved_calls"] += record.usage_unobserved_calls
    target["call_count"] += record.call_count


def _matches_filters(
    *,
    model: str,
    provider_id: str,
    agent_id: str | None,
    conversation_id: str | None,
    turn_id: str | None,
    expected_model: str | None,
    expected_provider: str | None,
    expected_agent: str | None,
    expected_conversation: str | None,
    expected_turn: str | None,
) -> bool:
    """Return whether one usage row belongs to the requested scope."""
    return all(
        (
            expected_model is None or model == expected_model,
            expected_provider is None or provider_id == expected_provider,
            expected_agent is None or agent_id == expected_agent,
            expected_conversation is None
            or conversation_id == expected_conversation,
            expected_turn is None or turn_id == expected_turn,
        ),
    )


def _record_identity(record: TokenUsageRecord) -> tuple[str | None, ...]:
    """Return the exact aggregation identity shared by both projections."""
    return (
        record.date,
        record.agent_id,
        record.conversation_id,
        record.turn_id,
        record.provider_id,
        record.model,
    )


def _overlay_fact_stats(
    legacy: TokenUsageRecord,
    shadow: TokenUsageRecord,
) -> TokenUsageRecord:
    """Add fact-only fields without duplicating compatibility totals."""
    return legacy.model_copy(
        update={
            "context_input_tokens": shadow.context_input_tokens,
            "context_window_tokens": shadow.context_window_tokens,
            "context_observed_calls": shadow.context_observed_calls,
            "near_compaction_calls": shadow.near_compaction_calls,
            "context_usage_ratio": shadow.context_usage_ratio,
            "max_context_usage_ratio": shadow.max_context_usage_ratio,
            "cost_micros": shadow.cost_micros,
            "cost_unknown_calls": shadow.cost_unknown_calls,
        },
    )


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
        conversation_id: str | None = None,
        turn_id: str | None = None,
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
        """
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
                conversation_id=conversation_id or "",
                turn_id=turn_id or "",
            ),
        )

    async def _query(
        self,
        merged: dict,
        start_date: date,
        end_date: date,
        model_name: Optional[str],
        provider_id: Optional[str],
        agent_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> list[TokenUsageRecord]:
        """Return per-day records from the merged data dict."""
        results: list[TokenUsageRecord] = []

        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            by_key = merged.get(date_str, {})
            for _key, entry in by_key.items():
                rec_provider = entry.get("provider_id", "") or ""
                if "agent_id" not in entry:
                    rec_agent = None
                else:
                    rec_agent = entry.get("agent_id") or ""
                rec_conversation = entry.get("conversation_id") or None
                rec_turn = entry.get("turn_id") or None
                rec_model = entry.get("model_name") or ""
                if not rec_model:
                    key = str(_key)
                    if "\x1f" in key:
                        rec_model = key.rsplit("\x1f", 1)[-1]
                    elif ":" in key:
                        rec_model = key.split(":", 1)[1]
                    else:
                        rec_model = key
                if not _matches_filters(
                    model=rec_model,
                    provider_id=rec_provider,
                    agent_id=rec_agent,
                    conversation_id=rec_conversation,
                    turn_id=rec_turn,
                    expected_model=model_name,
                    expected_provider=provider_id,
                    expected_agent=agent_id,
                    expected_conversation=conversation_id,
                    expected_turn=turn_id,
                ):
                    continue
                results.append(
                    TokenUsageRecord(
                        date=date_str,
                        provider_id=rec_provider,
                        model=rec_model,
                        prompt_tokens=entry.get("prompt_tokens", 0),
                        completion_tokens=entry.get("completion_tokens", 0),
                        cache_read_tokens=entry.get(
                            "cache_read_tokens",
                            0,
                        ),
                        cache_write_tokens=entry.get(
                            "cache_write_tokens",
                            0,
                        ),
                        cache_eligible_input_tokens=entry.get(
                            "cache_eligible_input_tokens",
                            0,
                        ),
                        cache_observed_calls=entry.get(
                            "cache_observed_calls",
                            0,
                        ),
                        cost_micros=0,
                        cost_unknown_calls=entry.get("call_count", 0),
                        usage_observed_calls=entry.get("call_count", 0),
                        usage_unobserved_calls=0,
                        call_count=entry.get("call_count", 0),
                        agent_id=rec_agent,
                        conversation_id=rec_conversation,
                        turn_id=rec_turn,
                    ),
                )
            current += timedelta(days=1)

        return results

    async def get_summary(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> TokenUsageSummary:
        """Get aggregated token usage summary.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            model_name: Optional model name filter.
            provider_id: Optional provider ID filter.

        Returns:
            TokenUsageSummary with totals, by_model, by_provider, by_date.
        """
        if end_date is None:
            end_date = datetime.now(tz=timezone.utc).date()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        records = await self._get_records(
            start_date,
            end_date,
            model_name,
            provider_id,
            agent_id,
            conversation_id,
            turn_id,
        )

        total_stats = _new_stats()
        by_model_raw: dict[str, dict] = {}
        by_date_raw: dict[str, dict] = {}
        by_date_model_raw: dict[str, dict[str, dict]] = {}
        by_agent_raw: dict[str, dict] = {}
        by_chat_raw: dict[str, dict] = {}
        by_turn_raw: dict[str, dict] = {}

        for r in records:
            _add_stats(total_stats, r)

            # Aggregate by model
            model_key = (
                f"{r.provider_id}:{r.model}" if r.provider_id else r.model
            )
            model_identity = {
                "provider_id": r.provider_id,
                "model": r.model,
            }
            bm = by_model_raw.setdefault(
                model_key,
                _new_stats(**model_identity),
            )
            _add_stats(bm, r)

            # Aggregate by date
            bd = by_date_raw.setdefault(
                r.date,
                _new_stats(),
            )
            _add_stats(bd, r)

            date_models = by_date_model_raw.setdefault(r.date, {})
            bdm = date_models.setdefault(
                model_key,
                _new_stats(**model_identity),
            )
            _add_stats(bdm, r)

            agent_key = _scope_key(r.agent_id)
            ba = by_agent_raw.setdefault(
                agent_key,
                _new_stats(agent_id=r.agent_id),
            )
            _add_stats(ba, r)

            chat_key = _scope_key(r.agent_id, r.conversation_id)
            bc = by_chat_raw.setdefault(
                chat_key,
                _new_stats(
                    agent_id=r.agent_id,
                    conversation_id=r.conversation_id,
                ),
            )
            _add_stats(bc, r)

            turn_key = _scope_key(
                r.agent_id,
                r.conversation_id,
                r.turn_id,
            )
            bt = by_turn_raw.setdefault(
                turn_key,
                _new_stats(
                    agent_id=r.agent_id,
                    conversation_id=r.conversation_id,
                    turn_id=r.turn_id,
                ),
            )
            _add_stats(bt, r)

        by_agent = {
            key: TokenUsageByAgent.model_validate(value)
            for key, value in sorted(by_agent_raw.items())
        }
        by_chat = {
            key: TokenUsageByConversation.model_validate(value)
            for key, value in sorted(by_chat_raw.items())
        }
        by_turn = {
            key: TokenUsageByTurn.model_validate(value)
            for key, value in sorted(by_turn_raw.items())
        }
        return TokenUsageSummary(
            total_prompt_tokens=total_stats["prompt_tokens"],
            total_completion_tokens=total_stats["completion_tokens"],
            total_cache_read_tokens=total_stats["cache_read_tokens"],
            total_cache_write_tokens=total_stats["cache_write_tokens"],
            total_cache_eligible_input_tokens=(
                total_stats["cache_eligible_input_tokens"]
            ),
            cache_observed_calls=total_stats["cache_observed_calls"],
            cache_hit_rate=(
                total_stats["cache_read_tokens"]
                / total_stats["cache_eligible_input_tokens"]
                * 100
                if total_stats["cache_eligible_input_tokens"] > 0
                else None
            ),
            total_context_input_tokens=total_stats["context_input_tokens"],
            total_context_window_tokens=total_stats["context_window_tokens"],
            context_observed_calls=total_stats["context_observed_calls"],
            near_compaction_calls=total_stats["near_compaction_calls"],
            total_cost_micros=total_stats["cost_micros"],
            cost_unknown_calls=total_stats["cost_unknown_calls"],
            context_usage_ratio=total_stats["context_usage_ratio"],
            max_context_usage_ratio=total_stats["max_context_usage_ratio"],
            total_calls=total_stats["call_count"],
            usage_observed_calls=total_stats["usage_observed_calls"],
            usage_unobserved_calls=total_stats["usage_unobserved_calls"],
            by_model={
                k: TokenUsageByModel.model_validate(v)
                for k, v in sorted(by_model_raw.items())
            },
            by_date={
                k: TokenUsageStats.model_validate(v)
                for k, v in sorted(by_date_raw.items())
            },
            by_date_model={
                date_key: {
                    model_key: TokenUsageByDateModel.model_validate(value)
                    for model_key, value in sorted(models.items())
                }
                for date_key, models in sorted(by_date_model_raw.items())
            },
            scopes=TokenUsageScopeRows(
                agents=list(by_agent.values()),
                chats=list(by_chat.values()),
                turns=list(by_turn.values()),
            ),
            by_agent=by_agent,
            by_chat=by_chat,
            by_turn=by_turn,
        )

    async def get_details(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> list[TokenUsageRecord]:
        """Get raw token usage records for frontend aggregation.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            model_name: Optional model name filter.
            provider_id: Optional provider ID filter.

        Returns:
            List of TokenUsageRecord. With agent tracking, a row is
            (date, agent, provider, model).
        """
        if end_date is None:
            end_date = datetime.now(tz=timezone.utc).date()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        return await self._get_records(
            start_date,
            end_date,
            model_name,
            provider_id,
            agent_id,
            conversation_id,
            turn_id,
        )

    async def _get_records(
        self,
        start_date: date,
        end_date: date,
        model_name: Optional[str],
        provider_id: Optional[str],
        agent_id: Optional[str],
        conversation_id: Optional[str],
        turn_id: Optional[str],
    ) -> list[TokenUsageRecord]:
        """Merge pre-cutover compatibility rows with fact projections."""
        merged = await self._buffer.get_merged_data()
        legacy = await self._query(
            merged,
            start_date,
            end_date,
            model_name,
            provider_id,
            agent_id,
            conversation_id,
            turn_id,
        )
        cutover, projected = await self._projection.query(
            start_date,
            end_date,
            model_name=model_name,
            provider_id=provider_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            include_shadow=True,
        )
        shadow_by_identity = {
            _record_identity(record): record
            for record in projected
            if date.fromisoformat(record.date) < cutover
        }
        compatible: list[TokenUsageRecord] = []
        for record in legacy:
            before_cutover = date.fromisoformat(record.date) < cutover
            unscoped = (
                record.conversation_id is None and record.turn_id is None
            )
            if not before_cutover and not unscoped:
                continue
            shadow = shadow_by_identity.get(_record_identity(record))
            compatible.append(
                _overlay_fact_stats(record, shadow)
                if before_cutover and shadow is not None
                else record,
            )
        projected = [
            record
            for record in projected
            if date.fromisoformat(record.date) >= cutover
        ]
        return [*compatible, *projected]

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
