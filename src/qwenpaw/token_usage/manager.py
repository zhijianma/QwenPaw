# -*- coding: utf-8 -*-
"""Token usage manager — thin orchestrator.

Delegates all I/O to :mod:`buffer` and :mod:`storage` so callers only
need to interact with the single ``TokenUsageManager`` singleton.

Public API is backward-compatible with the previous implementation:
  - ``record(...)``
  - ``get_summary(...)``

New additions:
  - ``record`` now accepts optional ``session_id`` and ``agent_id``.
  - ``get_session_stats(session_id)`` — stats for a single session.
  - ``get_agent_stats(agent_id)`` — stats for a single agent.
  - ``get_all_agent_stats()`` — stats for every known agent.
  - ``start()`` / ``stop()`` — lifecycle (called from app lifespan).
"""

import logging
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from ..constant import WORKING_DIR, TOKEN_USAGE_FILE
from .buffer import TokenUsageBuffer, _UsageEvent
from .models import (
    TokenUsageAgentStats,
    TokenUsageByModel,
    TokenUsageRecord,
    TokenUsageSessionStats,
    TokenUsageStats,
    TokenUsageSummary,
)
from .storage import DAILY_RETENTION_DAYS

logger = logging.getLogger(__name__)


class TokenUsageManager:
    """Orchestrator for token usage recording and querying.

    Use ``get_instance()`` to obtain the process-wide singleton.
    """

    _instance: "TokenUsageManager | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        path: Path = (WORKING_DIR / TOKEN_USAGE_FILE).expanduser()
        self._buffer = TokenUsageBuffer(path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, flush_interval: int = 10) -> None:
        """Start background flush task.

        Must be called from an async context (e.g. app lifespan startup).
        ``flush_interval`` is the number of seconds between flushes.
        """
        self._buffer._flush_interval = flush_interval
        self._buffer.start()

    async def stop(self) -> None:
        """Stop the flush task and perform a final flush before exit."""
        await self._buffer.stop()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def enqueue(self, event: _UsageEvent) -> None:
        """Synchronous fire-and-forget — enqueue a pre-built usage event.

        Called directly from ``TokenRecordingModelWrapper._record_usage()``
        on the hot path.  No ``await`` required.
        """
        self._buffer.enqueue(event)

    async def record(
        self,
        provider_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        at_date: Optional[date] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        """Record token usage for a given provider, model and date.

        Convenience async wrapper around ``enqueue()`` for callers that
        prefer the original async interface (e.g. tests, skill tools).

        Args:
            provider_id: ID of the provider (e.g. "dashscope", "openai").
            model_name: Name of the model (e.g. "qwen3-max", "gpt-4").
            prompt_tokens: Number of input/prompt tokens.
            completion_tokens: Number of output/completion tokens.
            at_date: Date to record under. Defaults to today (local).
            session_id: Optional conversation session ID.
            agent_id: Optional agent ID.
        """
        from datetime import datetime, timezone

        if at_date is None:
            at_date = date.today()
        self._buffer.enqueue(
            _UsageEvent(
                provider_id=provider_id,
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                date_str=at_date.isoformat(),
                now_iso=datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                session_id=session_id or "",
                agent_id=agent_id or "",
            )
        )

    # ------------------------------------------------------------------
    # Query path
    # ------------------------------------------------------------------

    async def _query_daily(
        self,
        merged: dict,
        start_date: date,
        end_date: date,
        model_name: Optional[str],
        provider_id: Optional[str],
    ) -> list[TokenUsageRecord]:
        """Return per-day records from the merged data dict.

        For date ranges that fall within the daily retention window the
        per-day ``"daily"`` section is used.  For older ranges (or when the
        requested window straddles the cutoff) the aggregated ``"monthly"``
        bucket is used, yielding one record per ``YYYY-MM`` per model with
        the synthetic date set to the first day of that month.
        """
        results: list[TokenUsageRecord] = []
        daily = merged.get("daily", {})
        monthly = merged.get("monthly", {})

        # Cutoff: days older than this live in monthly buckets.
        cutoff = date.today() - timedelta(days=DAILY_RETENTION_DAYS)

        # ---- Collect from daily (within retention window) ----
        current = max(start_date, cutoff)
        while current <= end_date:
            date_str = current.isoformat()
            by_key = daily.get(date_str, {})
            for _key, entry in by_key.items():
                rec_provider = entry.get("provider_id", "")
                rec_model = entry.get("model_name") or _key
                if model_name is not None and rec_model != model_name:
                    continue
                if provider_id is not None and rec_provider != provider_id:
                    continue
                results.append(
                    TokenUsageRecord(
                        date=date_str,
                        provider_id=rec_provider,
                        model=rec_model,
                        prompt_tokens=entry.get("prompt_tokens", 0),
                        completion_tokens=entry.get("completion_tokens", 0),
                        call_count=entry.get("call_count", 0),
                    )
                )
            current += timedelta(days=1)

        # ---- Collect from monthly (beyond retention window) ----
        if start_date < cutoff:
            # Determine which YYYY-MM buckets overlap [start_date, min(end_date, cutoff-1)]
            month_end = min(end_date, cutoff - timedelta(days=1))
            # Iterate over existing monthly keys that fall in range.
            for month_key, by_key in monthly.items():
                try:
                    month_date = date.fromisoformat(month_key + "-01")
                except ValueError:
                    continue
                # The month overlaps the query range if its first day <= month_end
                # and its last day >= start_date.  Use a simple month-boundary check.
                month_last_day = (
                    date(month_date.year, month_date.month + 1, 1) - timedelta(days=1)
                    if month_date.month < 12
                    else date(month_date.year, 12, 31)
                )
                if month_date > month_end or month_last_day < start_date:
                    continue
                for _key, entry in by_key.items():
                    rec_provider = entry.get("provider_id", "")
                    rec_model = entry.get("model_name") or _key
                    if model_name is not None and rec_model != model_name:
                        continue
                    if provider_id is not None and rec_provider != provider_id:
                        continue
                    results.append(
                        TokenUsageRecord(
                            date=month_key + "-01",  # synthetic: first of month
                            provider_id=rec_provider,
                            model=rec_model,
                            prompt_tokens=entry.get("prompt_tokens", 0),
                            completion_tokens=entry.get("completion_tokens", 0),
                            call_count=entry.get("call_count", 0),
                        )
                    )

        return results

    async def get_summary(
        self,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        model_name: Optional[str] = None,
        provider_id: Optional[str] = None,
    ) -> TokenUsageSummary:
        """Get aggregated token usage summary.

        Args:
            start_date: Start of date range (inclusive). Default: 30 days ago.
            end_date: End of date range (inclusive). Default: today.
            model_name: Optional model name filter.
            provider_id: Optional provider ID filter.

        Returns:
            TokenUsageSummary with totals, by_model, by_provider, by_date,
            by_session and by_agent.
        """
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        merged = await self._buffer.get_merged_data()

        records = await self._query_daily(
            merged, start_date, end_date, model_name, provider_id
        )

        total_prompt = 0
        total_completion = 0
        total_calls = 0
        by_model_raw: dict[str, dict] = {}
        by_provider_raw: dict[str, dict] = {}
        by_date_raw: dict[str, dict] = {}

        for r in records:
            pt = r.prompt_tokens
            ct = r.completion_tokens
            calls = r.call_count
            total_prompt += pt
            total_completion += ct
            total_calls += calls

            model = r.model
            prov = r.provider_id
            composite = f"{prov}:{model}" if prov else model
            bm = by_model_raw.setdefault(
                composite,
                {"provider_id": prov, "model": model,
                 "prompt_tokens": 0, "completion_tokens": 0, "call_count": 0},
            )
            bm["prompt_tokens"] += pt
            bm["completion_tokens"] += ct
            bm["call_count"] += calls

            bp = by_provider_raw.setdefault(
                prov,
                {"prompt_tokens": 0, "completion_tokens": 0, "call_count": 0},
            )
            bp["prompt_tokens"] += pt
            bp["completion_tokens"] += ct
            bp["call_count"] += calls

            bd = by_date_raw.setdefault(
                r.date,
                {"prompt_tokens": 0, "completion_tokens": 0, "call_count": 0},
            )
            bd["prompt_tokens"] += pt
            bd["completion_tokens"] += ct
            bd["call_count"] += calls

        # Build session stats
        by_session: dict[str, TokenUsageSessionStats] = {}
        for sid, raw in merged.get("sessions", {}).items():
            by_session[sid] = TokenUsageSessionStats(
                agent_id=raw.get("agent_id", ""),
                prompt_tokens=raw.get("prompt_tokens", 0),
                completion_tokens=raw.get("completion_tokens", 0),
                call_count=raw.get("call_count", 0),
                last_updated=raw.get("last_updated"),
            )

        # Build agent stats
        by_agent: dict[str, TokenUsageAgentStats] = {}
        for aid, raw in merged.get("agents", {}).items():
            by_agent[aid] = TokenUsageAgentStats(
                agent_id=aid,
                prompt_tokens=raw.get("prompt_tokens", 0),
                completion_tokens=raw.get("completion_tokens", 0),
                call_count=raw.get("call_count", 0),
            )

        return TokenUsageSummary(
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_calls=total_calls,
            by_model={
                k: TokenUsageByModel.model_validate(v)
                for k, v in by_model_raw.items()
            },
            by_provider={
                k: TokenUsageStats.model_validate(v)
                for k, v in by_provider_raw.items()
            },
            by_date={
                k: TokenUsageStats.model_validate(v)
                for k, v in sorted(by_date_raw.items())
            },
            by_session=by_session,
            by_agent=by_agent,
        )

    async def get_session_stats(
        self, session_id: str
    ) -> Optional[TokenUsageSessionStats]:
        """Return token usage stats for a single session, or None if not found."""
        merged = await self._buffer.get_merged_data()
        raw = merged.get("sessions", {}).get(session_id)
        if raw is None:
            return None
        return TokenUsageSessionStats(
            agent_id=raw.get("agent_id", ""),
            prompt_tokens=raw.get("prompt_tokens", 0),
            completion_tokens=raw.get("completion_tokens", 0),
            call_count=raw.get("call_count", 0),
            last_updated=raw.get("last_updated"),
        )

    async def get_agent_stats(
        self, agent_id: str
    ) -> Optional[TokenUsageAgentStats]:
        """Return token usage stats for a single agent, or None if not found."""
        merged = await self._buffer.get_merged_data()
        raw = merged.get("agents", {}).get(agent_id)
        if raw is None:
            return None
        return TokenUsageAgentStats(
            agent_id=agent_id,
            prompt_tokens=raw.get("prompt_tokens", 0),
            completion_tokens=raw.get("completion_tokens", 0),
            call_count=raw.get("call_count", 0),
        )

    async def get_all_agent_stats(self) -> dict[str, TokenUsageAgentStats]:
        """Return token usage stats for every known agent."""
        merged = await self._buffer.get_merged_data()
        return {
            aid: TokenUsageAgentStats(
                agent_id=aid,
                prompt_tokens=raw.get("prompt_tokens", 0),
                completion_tokens=raw.get("completion_tokens", 0),
                call_count=raw.get("call_count", 0),
            )
            for aid, raw in merged.get("agents", {}).items()
        }

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

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
