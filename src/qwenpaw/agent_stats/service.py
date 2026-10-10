# -*- coding: utf-8 -*-
"""Agent statistics service."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import aiofiles
import aiofiles.os
import orjson

from ..app.chats.repo import JsonChatRepository
from ..app.chats.session import session_relative_paths
from ..config.utils import get_agent_dirs
from ..token_usage import get_token_usage_manager
from ..token_usage.turn_usage import TURN_USAGE_META_KEY
from .models import (
    AgentStatsSummary,
    ChannelStats,
    ChatUsageStats,
    DailyStats,
    LlmToolDaily,
)

logger = logging.getLogger(__name__)


@dataclass
class _SessionStats:
    """Independent result from scanning one persisted Conversation."""

    tool_calls: int = 0
    has_messages: bool = False
    estimated_prompt_tokens: int = 0
    estimated_completion_tokens: int = 0
    estimated_turns: int = 0
    chat_id: str | None = None
    chat_name: str | None = None


# pylint: disable=unused-argument
def _should_skip_by_mtime(
    session_file: Path,
    start_date: date,
    end_date: date,
) -> bool:
    try:
        mtime = session_file.stat().st_mtime
        mtime_date = date.fromtimestamp(mtime)
        if mtime_date < start_date:
            logger.debug(
                "Skipping %s by mtime (%s) before start date %s",
                session_file.name,
                mtime_date.isoformat(),
                start_date.isoformat(),
            )
            return True
    except OSError:
        pass
    return False


def _extract_session_messages(session_data: dict) -> list:
    """Return raw message dicts/tuples from a session state, 1.x or 2.0."""
    agent_raw = session_data.get("agent", {})
    # 2.0: messages live on agent.state.context
    state_raw = agent_raw.get("state")
    if isinstance(state_raw, dict):
        ctx = state_raw.get("context")
        if isinstance(ctx, list) and ctx:
            return ctx
    # 1.x fallback
    memory_raw = agent_raw.get("memory", {})
    if isinstance(memory_raw, dict):
        return memory_raw.get("memories") or memory_raw.get("content") or []
    return []


def _should_skip_by_content_range(
    session_data: dict,
    start_date_str: str,
    end_date_str: str,
) -> bool:
    memories = _extract_session_messages(session_data)

    if not memories:
        return True

    timestamps: list[str] = []
    for msg_item in memories:
        if isinstance(msg_item, list) and len(msg_item) > 0:
            msg_data = msg_item[0]
        elif isinstance(msg_item, dict):
            msg_data = msg_item
        else:
            continue

        if not isinstance(msg_data, dict):
            continue

        timestamp = msg_data.get("created_at") or msg_data.get("timestamp")
        if timestamp:
            timestamps.append(str(timestamp)[:10])

    if not timestamps:
        return True

    first_date = timestamps[0]
    last_date = timestamps[-1]

    if last_date < start_date_str or first_date > end_date_str:
        logger.debug(
            "Skipping session by content range [%s, %s] "
            "outside target [%s, %s]",
            first_date,
            last_date,
            start_date_str,
            end_date_str,
        )
        return True

    return False


def _extract_local_estimate(msg_data: dict) -> tuple[int, int] | None:
    """Return an explicit local estimate without accepting usage facts."""
    metadata = msg_data.get("metadata")
    turn_metadata = (
        metadata.get(TURN_USAGE_META_KEY)
        if isinstance(metadata, dict)
        else None
    )
    usage = (
        turn_metadata.get("usage") if isinstance(turn_metadata, dict) else None
    )
    if not (
        isinstance(usage, dict)
        and usage.get("measurement") == "local_estimate"
        and usage.get("estimated") is True
    ):
        return None
    try:
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    if prompt_tokens < 0 or completion_tokens < 0:
        return None
    if prompt_tokens == 0 and completion_tokens == 0:
        return None
    return prompt_tokens, completion_tokens


# pylint:disable=too-many-statements,too-many-branches
def _process_session_file(
    session_data: dict,
    start_date_str: str,
    end_date_str: str,
    daily_stats: dict[str, dict],
    channel_stats: dict[str, dict],
    channel: str,
    session_stem: str,
    active_sessions: dict[str, set[str]],
    *,
    chat_id: str | None = None,
    chat_name: str | None = None,
    collect_local_estimates: bool = True,
) -> _SessionStats:
    tool_call_count = 0
    has_messages_in_range = False
    estimated_prompt_tokens = 0
    estimated_completion_tokens = 0
    estimated_turns = 0
    try:
        memories = _extract_session_messages(session_data)

        stats = channel_stats.setdefault(
            channel,
            {
                "session_count": 0,
                "user_messages": 0,
                "assistant_messages": 0,
                "total_messages": 0,
            },
        )

        for msg_item in memories:
            if isinstance(msg_item, list) and len(msg_item) > 0:
                msg_data = msg_item[0]
            elif isinstance(msg_item, dict):
                msg_data = msg_item
            else:
                continue

            if not isinstance(msg_data, dict):
                continue

            timestamp = msg_data.get("created_at") or msg_data.get("timestamp")
            if not timestamp:
                continue

            date_str = str(timestamp)[:10]
            if date_str < start_date_str or date_str > end_date_str:
                continue
            ds = daily_stats.get(date_str)
            if ds is None:
                continue

            has_messages_in_range = True
            active_sessions.setdefault(date_str, set()).add(session_stem)

            role = msg_data.get("role", "")
            content = msg_data.get("content", [])

            if role == "user":
                ds["user_messages"] += 1
                ds["total_messages"] += 1
                stats["user_messages"] += 1
                stats["total_messages"] += 1
            elif role == "assistant":
                ds["assistant_messages"] += 1
                ds["total_messages"] += 1
                stats["assistant_messages"] += 1
                stats["total_messages"] += 1
                estimate = (
                    _extract_local_estimate(msg_data)
                    if collect_local_estimates
                    else None
                )
                if estimate is not None:
                    prompt_tokens, completion_tokens = estimate
                    estimated_prompt_tokens += prompt_tokens
                    estimated_completion_tokens += completion_tokens
                    estimated_turns += 1
                    ds["estimated_prompt_tokens"] += prompt_tokens
                    ds["estimated_completion_tokens"] += completion_tokens
                    ds["estimated_turns"] += 1

            if isinstance(content, list):
                for block in content:
                    btype = (
                        block.get("type")
                        if isinstance(block, dict)
                        else getattr(block, "type", None)
                    )
                    if btype in ("tool_use", "tool_call"):
                        ds["tool_calls"] += 1
                        tool_call_count += 1

    except Exception as e:
        logger.warning("Failed to count messages in session: %s", e)

    if has_messages_in_range and channel in channel_stats:
        channel_stats[channel]["session_count"] += 1

    return _SessionStats(
        tool_calls=tool_call_count,
        has_messages=has_messages_in_range,
        estimated_prompt_tokens=estimated_prompt_tokens,
        estimated_completion_tokens=estimated_completion_tokens,
        estimated_turns=estimated_turns,
        chat_id=chat_id,
        chat_name=chat_name,
    )


class AgentStatsService:
    """Service for computing agent statistics."""

    # pylint: disable=R0912,R0915
    async def get_summary(
        self,
        workspace_dir: Path,
        start_date: date,
        end_date: date,
        *,
        agent_id: str | None = None,
        include_token_overlay: bool = True,
    ) -> AgentStatsSummary:
        """Return Agent Statistics for one workspace.

        Session files provide message/Tool activity and explicitly labelled
        local estimates. Model Call facts provide all authoritative token,
        cache, coverage, and LLM-call statistics. The two layers never mix.
        """
        if include_token_overlay and not (agent_id and agent_id.strip()):
            raise ValueError(
                "agent_id is required for Agent Statistics usage",
            )
        chats_file = workspace_dir / "chats.json"
        sessions_dir = workspace_dir / "sessions"

        daily_stats: dict[str, dict] = {}
        days = (end_date - start_date).days + 1
        for i in range(days):
            date_str = (start_date + timedelta(days=i)).isoformat()
            daily_stats[date_str] = {
                "date": date_str,
                "chats": 0,
                "active_sessions": 0,
                "user_messages": 0,
                "assistant_messages": 0,
                "total_messages": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "llm_calls": 0,
                "tool_calls": 0,
                "agent_prompt_tokens": 0,
                "agent_completion_tokens": 0,
                "agent_llm_calls": 0,
                "agent_cache_read_tokens": 0,
                "estimated_prompt_tokens": 0,
                "estimated_completion_tokens": 0,
                "estimated_turns": 0,
            }

        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()

        channel_stats: dict[str, dict] = {}
        total_tool_calls = 0
        active_sessions: dict[str, set[str]] = {}
        total_active_sessions = 0
        chat_names: dict[str, str] = {}
        chat_by_session_path: dict[str, tuple[str, str]] = {}
        estimates_by_chat: dict[str, ChatUsageStats] = {}
        agent_estimated_prompt_tokens = 0
        agent_estimated_completion_tokens = 0
        agent_estimated_turns = 0

        if chats_file.exists():
            try:
                repo = JsonChatRepository(chats_file)
                chats = await repo.list_chats()
                for chat in chats:
                    chat_names[chat.id] = chat.name
                    for relative_path in session_relative_paths(
                        chat.session_id,
                        chat.user_id,
                        chat.channel,
                    ):
                        chat_by_session_path[relative_path] = (
                            chat.id,
                            chat.name,
                        )
                    if chat.created_at is None:
                        continue
                    chat_date = chat.created_at.date()
                    if start_date <= chat_date <= end_date:
                        date_str = chat_date.isoformat()
                        daily_stats[date_str]["chats"] += 1
            except Exception as e:
                logger.warning("Failed to load chat statistics: %s", e)

        # pylint: disable=too-many-nested-blocks
        if sessions_dir.exists():
            try:
                session_files = []

                # Scan root sessions directory for legacy files
                channel_names = await aiofiles.os.listdir(sessions_dir)
                for channel_name in channel_names:
                    channel_path = sessions_dir / channel_name
                    if await aiofiles.os.path.isdir(channel_path):
                        try:
                            channel_files = await aiofiles.os.listdir(
                                channel_path,
                            )
                            for channel_file in channel_files:
                                session_file = channel_path / channel_file
                                if session_file.name.endswith(".json"):
                                    session_files.append(session_file)
                        except Exception as e:
                            logger.debug(
                                "Failed to scan channel directory %s: %s",
                                channel_path,
                                e,
                            )

                session_fd_sem = asyncio.Semaphore((os.cpu_count() or 4) * 2)

                async def _process_one(
                    session_file: Path,
                ) -> _SessionStats:
                    async with session_fd_sem:
                        if _should_skip_by_mtime(
                            session_file,
                            start_date,
                            end_date,
                        ):
                            return _SessionStats()

                        try:
                            async with aiofiles.open(
                                session_file,
                                "r",
                                encoding="utf-8",
                            ) as f:
                                session_data = orjson.loads(await f.read())
                        except Exception as e:
                            logger.debug(
                                "Failed to read session file %s: %s",
                                session_file,
                                e,
                            )
                            return _SessionStats()

                        if _should_skip_by_content_range(
                            session_data,
                            start_date_str,
                            end_date_str,
                        ):
                            return _SessionStats()

                        stem = session_file.stem
                        # Check if session is in a channel subdirectory
                        channel = session_file.parent.name
                        relative_path = session_file.relative_to(
                            sessions_dir,
                        ).as_posix()
                        chat_identity = chat_by_session_path.get(relative_path)
                        return _process_session_file(
                            session_data,
                            start_date_str,
                            end_date_str,
                            daily_stats,
                            channel_stats,
                            channel,
                            stem,
                            active_sessions,
                            chat_id=(
                                chat_identity[0] if chat_identity else None
                            ),
                            chat_name=(
                                chat_identity[1] if chat_identity else None
                            ),
                            collect_local_estimates=include_token_overlay,
                        )

                tasks = [_process_one(sf) for sf in session_files]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, _SessionStats):
                        total_tool_calls += result.tool_calls
                        if result.has_messages:
                            total_active_sessions += 1
                        agent_estimated_prompt_tokens += (
                            result.estimated_prompt_tokens
                        )
                        agent_estimated_completion_tokens += (
                            result.estimated_completion_tokens
                        )
                        agent_estimated_turns += result.estimated_turns
                        if result.chat_id and result.estimated_turns > 0:
                            row = estimates_by_chat.setdefault(
                                result.chat_id,
                                ChatUsageStats(
                                    chat_id=result.chat_id,
                                    name=result.chat_name,
                                ),
                            )
                            row.estimated_prompt_tokens += (
                                result.estimated_prompt_tokens
                            )
                            row.estimated_completion_tokens += (
                                result.estimated_completion_tokens
                            )
                            row.estimated_turns += result.estimated_turns
                    elif isinstance(result, Exception):
                        logger.debug("Failed to process session: %s", result)
            except Exception as e:
                logger.warning("Failed to load message statistics: %s", e)

        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_llm_calls = 0
        agent_prompt_tokens = 0
        agent_completion_tokens = 0
        agent_llm_calls = 0
        agent_cache_read_tokens = 0
        agent_cache_eligible_input_tokens = 0
        chat_usage: list[ChatUsageStats] = []
        if include_token_overlay:
            token_manager = get_token_usage_manager()
            token_summary = await token_manager.get_summary(
                start_date=start_date,
                end_date=end_date,
                agent_id=agent_id,
            )
            total_prompt_tokens = token_summary.total_prompt_tokens
            total_completion_tokens = token_summary.total_completion_tokens
            total_llm_calls = token_summary.total_calls
            agent_prompt_tokens = token_summary.total_prompt_tokens
            agent_completion_tokens = token_summary.total_completion_tokens
            agent_llm_calls = token_summary.total_calls
            agent_cache_read_tokens = token_summary.total_cache_read_tokens
            agent_cache_eligible_input_tokens = (
                token_summary.total_cache_eligible_input_tokens
            )
            for date_str, ts in token_summary.by_date.items():
                if date_str in daily_stats:
                    daily_stats[date_str]["prompt_tokens"] = ts.prompt_tokens
                    daily_stats[date_str][
                        "completion_tokens"
                    ] = ts.completion_tokens
                    daily_stats[date_str]["llm_calls"] = ts.call_count
                    daily_stats[date_str][
                        "agent_prompt_tokens"
                    ] = ts.prompt_tokens
                    daily_stats[date_str][
                        "agent_completion_tokens"
                    ] = ts.completion_tokens
                    daily_stats[date_str]["agent_llm_calls"] = ts.call_count
                    daily_stats[date_str][
                        "agent_cache_read_tokens"
                    ] = ts.cache_read_tokens
            chat_usage = sorted(
                (
                    ChatUsageStats(
                        chat_id=row.chat_id,
                        name=(
                            chat_names.get(row.chat_id)
                            if row.chat_id is not None
                            else None
                        ),
                        prompt_tokens=row.prompt_tokens,
                        completion_tokens=row.completion_tokens,
                        cache_read_tokens=row.cache_read_tokens,
                        cache_eligible_input_tokens=(
                            row.cache_eligible_input_tokens
                        ),
                        cache_hit_rate=(
                            row.cache_read_tokens
                            / row.cache_eligible_input_tokens
                            * 100
                            if row.cache_eligible_input_tokens > 0
                            else None
                        ),
                        usage_unobserved_calls=(row.usage_unobserved_calls),
                        call_count=row.call_count,
                    )
                    for row in token_summary.scopes.chats
                ),
                key=lambda item: (
                    -(item.prompt_tokens + item.completion_tokens),
                    item.name or "",
                    item.chat_id or "",
                ),
            )

        chat_usage_by_id = {
            row.chat_id: row for row in chat_usage if row.chat_id is not None
        }
        for chat_id, estimate in estimates_by_chat.items():
            row = chat_usage_by_id.get(chat_id)
            if row is None:
                chat_usage.append(estimate)
                chat_usage_by_id[chat_id] = estimate
                continue
            row.estimated_prompt_tokens = estimate.estimated_prompt_tokens
            row.estimated_completion_tokens = (
                estimate.estimated_completion_tokens
            )
            row.estimated_turns = estimate.estimated_turns
        chat_usage.sort(
            key=lambda item: (
                -(
                    item.prompt_tokens
                    + item.completion_tokens
                    + item.estimated_prompt_tokens
                    + item.estimated_completion_tokens
                ),
                item.name or "",
                item.chat_id or "",
            ),
        )

        for date_str, session_set in active_sessions.items():
            if date_str in daily_stats:
                daily_stats[date_str]["active_sessions"] = len(session_set)

        by_date = [daily_stats[d] for d in sorted(daily_stats.keys())]

        total_user_messages = sum(ds["user_messages"] for ds in by_date)
        total_assistant_messages = sum(
            ds["assistant_messages"] for ds in by_date
        )
        total_messages = total_user_messages + total_assistant_messages
        return AgentStatsSummary(
            total_active_sessions=total_active_sessions,
            total_messages=total_messages,
            total_user_messages=total_user_messages,
            total_assistant_messages=total_assistant_messages,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            total_llm_calls=total_llm_calls,
            total_tool_calls=total_tool_calls,
            by_date=[DailyStats.model_validate(ds) for ds in by_date],
            channel_stats=[
                ChannelStats(
                    channel=ch,
                    session_count=cnts["session_count"],
                    user_messages=cnts["user_messages"],
                    assistant_messages=cnts["assistant_messages"],
                    total_messages=cnts["total_messages"],
                )
                for ch, cnts in sorted(channel_stats.items())
            ],
            chat_usage=chat_usage,
            start_date=start_date_str,
            end_date=end_date_str,
            agent_prompt_tokens=agent_prompt_tokens,
            agent_completion_tokens=agent_completion_tokens,
            agent_llm_calls=agent_llm_calls,
            agent_cache_read_tokens=agent_cache_read_tokens,
            agent_cache_eligible_input_tokens=(
                agent_cache_eligible_input_tokens
            ),
            agent_cache_hit_rate=(
                agent_cache_read_tokens
                / agent_cache_eligible_input_tokens
                * 100
                if agent_cache_eligible_input_tokens > 0
                else None
            ),
            agent_estimated_prompt_tokens=agent_estimated_prompt_tokens,
            agent_estimated_completion_tokens=(
                agent_estimated_completion_tokens
            ),
            agent_estimated_turns=agent_estimated_turns,
        )

    async def get_global_llm_tool_by_date(
        self,
        start_date: date,
        end_date: date,
    ) -> list[LlmToolDaily]:
        """Combine global Model Call facts with session Tool activity."""
        if (end_date - start_date).days + 1 > 365:
            start_date = end_date - timedelta(days=364)

        totals: dict[str, dict[str, int]] = {}
        days = (end_date - start_date).days + 1
        for i in range(days):
            totals[(start_date + timedelta(days=i)).isoformat()] = {
                "agent_llm_calls": 0,
                "tool_calls": 0,
            }
        usage = await get_token_usage_manager().get_summary(
            start_date=start_date,
            end_date=end_date,
        )
        for date_str, stats in usage.by_date.items():
            if date_str in totals:
                totals[date_str]["agent_llm_calls"] = stats.call_count
        seen: set[str] = set()
        for path in get_agent_dirs():
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            summary = await self.get_summary(
                path,
                start_date,
                end_date,
                include_token_overlay=False,
            )
            for ds in summary.by_date:
                totals[ds.date]["tool_calls"] += ds.tool_calls
        return [
            LlmToolDaily(date=date_str, **totals[date_str])
            for date_str in sorted(totals)
        ]


_agent_stats_service: AgentStatsService | None = None


def get_agent_stats_service() -> AgentStatsService:
    global _agent_stats_service
    if _agent_stats_service is None:
        _agent_stats_service = AgentStatsService()
    return _agent_stats_service
