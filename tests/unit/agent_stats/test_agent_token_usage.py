# -*- coding: utf-8 -*-
"""Tests for Model-Call-backed usage in Agent Statistics."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from qwenpaw.agent_stats.models import AgentStatsSummary, DailyStats
from qwenpaw.agent_stats.service import (
    AgentStatsService,
    _process_session_file,
)
from qwenpaw.app.chats.session import session_filename
from qwenpaw.token_usage.models import (
    TokenUsageByChat,
    TokenUsageScopeRows,
    TokenUsageStats,
    TokenUsageSummary,
)
from qwenpaw.token_usage.turn_usage import TURN_USAGE_META_KEY


def _empty_daily(date_str: str) -> dict:
    return {
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


def _assistant_message(*, prompt_tokens: int = 999) -> dict:
    return {
        "role": "assistant",
        "created_at": "2026-07-23T08:00:01Z",
        "content": [
            {"type": "text", "text": "done"},
            {"type": "tool_use", "id": "tool-1", "name": "read"},
        ],
        "metadata": {
            TURN_USAGE_META_KEY: {
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 99,
                    "total_tokens": prompt_tokens + 99,
                },
            },
        },
    }


def _assistant_local_estimate(
    *,
    prompt_tokens: int = 578,
    completion_tokens: int = 24,
) -> dict:
    message = _assistant_message(prompt_tokens=prompt_tokens)
    usage = message["metadata"][TURN_USAGE_META_KEY]["usage"]
    usage.update(
        {
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "measurement": "local_estimate",
            "estimated": True,
        },
    )
    return message


def _usage_summary() -> TokenUsageSummary:
    by_date = TokenUsageStats(
        prompt_tokens=120,
        completion_tokens=30,
        cache_read_tokens=40,
        cache_eligible_input_tokens=80,
        cache_observed_calls=2,
        usage_observed_calls=2,
        usage_unobserved_calls=1,
        call_count=3,
    )
    chat = TokenUsageByChat(
        agent_id="agent-a",
        chat_id="chat-a",
        prompt_tokens=120,
        completion_tokens=30,
        cache_read_tokens=40,
        cache_eligible_input_tokens=80,
        cache_observed_calls=2,
        usage_observed_calls=2,
        usage_unobserved_calls=1,
        call_count=3,
    )
    return TokenUsageSummary(
        total_prompt_tokens=120,
        total_completion_tokens=30,
        total_cache_read_tokens=40,
        total_cache_eligible_input_tokens=80,
        cache_observed_calls=2,
        cache_hit_rate=50,
        total_calls=3,
        usage_observed_calls=2,
        usage_unobserved_calls=1,
        by_date={"2026-07-23": by_date},
        scopes=TokenUsageScopeRows(chats=[chat]),
    )


def _write_workspace(path: Path) -> None:
    path.mkdir()
    (path / "chats.json").write_text(
        json.dumps(
            {
                "version": 1,
                "chats": [
                    {
                        "id": "chat-a",
                        "name": "Architecture review",
                        "session_id": "console:chat-a",
                        "user_id": "console",
                        "channel": "console",
                        "created_at": "2026-07-23T08:00:00Z",
                        "updated_at": "2026-07-23T08:00:00Z",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    sessions = path / "sessions" / "console"
    sessions.mkdir(parents=True)
    messages = [
        {
            "role": "user",
            "created_at": "2026-07-23T08:00:00Z",
            "content": [{"type": "text", "text": "review"}],
        },
        _assistant_message(),
    ]
    (sessions / session_filename("console:chat-a", "console")).write_text(
        json.dumps({"agent": {"state": {"context": messages}}}),
        encoding="utf-8",
    )


def _empty_agent_summary(*, tool_calls: int = 0) -> AgentStatsSummary:
    return AgentStatsSummary(
        total_active_sessions=0,
        total_messages=0,
        total_user_messages=0,
        total_assistant_messages=0,
        total_prompt_tokens=0,
        total_completion_tokens=0,
        total_llm_calls=0,
        total_tool_calls=tool_calls,
        by_date=[
            DailyStats(
                date="2026-07-23",
                chats=0,
                active_sessions=0,
                user_messages=0,
                assistant_messages=0,
                total_messages=0,
                prompt_tokens=0,
                completion_tokens=0,
                llm_calls=0,
                tool_calls=tool_calls,
            ),
        ],
        channel_stats=[],
        start_date="2026-07-23",
        end_date="2026-07-23",
    )


def test_message_usage_is_not_an_accounting_fact() -> None:
    daily = {"2026-07-23": _empty_daily("2026-07-23")}
    result = _process_session_file(
        {"agent": {"state": {"context": [_assistant_message()]}}},
        "2026-07-23",
        "2026-07-23",
        daily,
        {},
        "console",
        "session-a",
        {},
    )

    assert result.has_messages is True
    assert result.tool_calls == 1
    assert daily["2026-07-23"]["assistant_messages"] == 1
    assert daily["2026-07-23"]["agent_prompt_tokens"] == 0
    assert daily["2026-07-23"]["agent_llm_calls"] == 0
    assert daily["2026-07-23"]["estimated_turns"] == 0


def test_session_scan_keeps_explicit_local_estimate_separate() -> None:
    daily = {"2026-07-23": _empty_daily("2026-07-23")}
    result = _process_session_file(
        {"agent": {"state": {"context": [_assistant_local_estimate()]}}},
        "2026-07-23",
        "2026-07-23",
        daily,
        {},
        "console",
        "session-a",
        {},
        chat_id="chat-a",
    )

    assert result.estimated_prompt_tokens == 578
    assert result.estimated_completion_tokens == 24
    assert result.estimated_turns == 1
    assert daily["2026-07-23"]["agent_prompt_tokens"] == 0
    assert daily["2026-07-23"]["estimated_prompt_tokens"] == 578
    assert daily["2026-07-23"]["estimated_completion_tokens"] == 24
    assert daily["2026-07-23"]["estimated_turns"] == 1


@pytest.mark.asyncio
async def test_agent_usage_comes_from_filtered_model_call_projection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "agent-a"
    _write_workspace(workspace)
    manager = AsyncMock()
    manager.get_summary = AsyncMock(return_value=_usage_summary())

    with patch(
        "qwenpaw.agent_stats.service.get_token_usage_manager",
        return_value=manager,
    ):
        summary = await AgentStatsService().get_summary(
            workspace_dir=workspace,
            start_date=date(2026, 7, 23),
            end_date=date(2026, 7, 24),
            agent_id="agent-a",
        )

    manager.get_summary.assert_awaited_once_with(
        start_date=date(2026, 7, 23),
        end_date=date(2026, 7, 24),
        agent_id="agent-a",
    )
    assert summary.total_prompt_tokens == 120
    assert summary.agent_prompt_tokens == 120
    assert summary.agent_completion_tokens == 30
    assert summary.agent_llm_calls == 3
    assert summary.agent_cache_hit_rate == 50
    assert summary.total_messages == 2
    assert summary.total_tool_calls == 1
    assert summary.by_date[0].agent_prompt_tokens == 120
    assert summary.by_date[0].agent_llm_calls == 3
    assert summary.by_date[1].agent_prompt_tokens == 0
    assert len(summary.chat_usage) == 1
    chat = summary.chat_usage[0]
    assert chat.chat_id == "chat-a"
    assert chat.name == "Architecture review"
    assert chat.prompt_tokens == 120
    assert chat.usage_unobserved_calls == 1
    assert chat.call_count == 3
    assert not hasattr(chat, "turn_id")


@pytest.mark.asyncio
async def test_model_facts_and_local_estimates_never_mix(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "agent-a"
    _write_workspace(workspace)
    session_path = (
        workspace
        / "sessions"
        / "console"
        / session_filename("console:chat-a", "console")
    )
    session_path.write_text(
        json.dumps(
            {
                "agent": {
                    "state": {
                        "context": [
                            _assistant_local_estimate(
                                prompt_tokens=578,
                                completion_tokens=24,
                            ),
                        ],
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    manager = AsyncMock()
    manager.get_summary = AsyncMock(return_value=_usage_summary())

    with patch(
        "qwenpaw.agent_stats.service.get_token_usage_manager",
        return_value=manager,
    ):
        summary = await AgentStatsService().get_summary(
            workspace_dir=workspace,
            start_date=date(2026, 7, 23),
            end_date=date(2026, 7, 23),
            agent_id="agent-a",
        )

    assert summary.agent_prompt_tokens == 120
    assert summary.agent_completion_tokens == 30
    assert summary.agent_llm_calls == 3
    assert summary.agent_estimated_prompt_tokens == 578
    assert summary.agent_estimated_completion_tokens == 24
    assert summary.agent_estimated_turns == 1
    assert summary.by_date[0].agent_prompt_tokens == 120
    assert summary.by_date[0].estimated_prompt_tokens == 578
    chat = summary.chat_usage[0]
    assert chat.prompt_tokens == 120
    assert chat.completion_tokens == 30
    assert chat.call_count == 3
    assert chat.estimated_prompt_tokens == 578
    assert chat.estimated_completion_tokens == 24
    assert chat.estimated_turns == 1


@pytest.mark.asyncio
async def test_agent_usage_requires_explicit_agent_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="agent_id is required"):
        await AgentStatsService().get_summary(
            workspace_dir=tmp_path,
            start_date=date(2026, 7, 23),
            end_date=date(2026, 7, 23),
        )


@pytest.mark.asyncio
async def test_activity_only_scan_does_not_query_usage_projection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "agent-a"
    _write_workspace(workspace)
    with patch(
        "qwenpaw.agent_stats.service.get_token_usage_manager",
    ) as manager:
        summary = await AgentStatsService().get_summary(
            workspace_dir=workspace,
            start_date=date(2026, 7, 23),
            end_date=date(2026, 7, 23),
            include_token_overlay=False,
        )

    manager.assert_not_called()
    assert summary.agent_prompt_tokens == 0
    assert summary.agent_llm_calls == 0
    assert summary.total_tool_calls == 1


@pytest.mark.asyncio
async def test_global_trend_uses_model_calls_and_session_tools() -> None:
    service = AgentStatsService()
    usage = TokenUsageSummary(
        by_date={"2026-07-23": TokenUsageStats(call_count=3)},
    )
    manager = AsyncMock()
    manager.get_summary = AsyncMock(return_value=usage)
    service.get_summary = AsyncMock(
        side_effect=[
            _empty_agent_summary(tool_calls=4),
            _empty_agent_summary(tool_calls=2),
        ],
    )

    with (
        patch(
            "qwenpaw.agent_stats.service.get_token_usage_manager",
            return_value=manager,
        ),
        patch(
            "qwenpaw.agent_stats.service.get_agent_dirs",
            return_value=[Path("/a"), Path("/a"), Path("/b")],
        ),
    ):
        rows = await service.get_global_llm_tool_by_date(
            start_date=date(2026, 7, 23),
            end_date=date(2026, 7, 24),
        )

    manager.get_summary.assert_awaited_once_with(
        start_date=date(2026, 7, 23),
        end_date=date(2026, 7, 24),
    )
    assert [(row.agent_llm_calls, row.tool_calls) for row in rows] == [
        (3, 6),
        (0, 0),
    ]


@pytest.mark.asyncio
async def test_global_trend_clamps_to_365_days() -> None:
    manager = AsyncMock()
    manager.get_summary = AsyncMock(return_value=TokenUsageSummary())
    with (
        patch(
            "qwenpaw.agent_stats.service.get_token_usage_manager",
            return_value=manager,
        ),
        patch(
            "qwenpaw.agent_stats.service.get_agent_dirs",
            return_value=[],
        ),
    ):
        rows = await AgentStatsService().get_global_llm_tool_by_date(
            start_date=date(2025, 1, 1),
            end_date=date(2026, 8, 1),
        )

    assert len(rows) == 365
    assert rows[0].date == "2025-08-02"
    assert rows[-1].date == "2026-08-01"
