# -*- coding: utf-8 -*-
"""Token usage API for console and skill tool."""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query

from ...token_usage import (
    TokenUsageAgentStats,
    TokenUsageSessionStats,
    TokenUsageSummary,
    get_token_usage_manager,
)

router = APIRouter(prefix="/token-usage", tags=["token-usage"])


def _parse_date(s: str | None) -> date | None:
    """Parse YYYY-MM-DD string to date."""
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


@router.get(
    "",
    summary="Get token usage summary",
    description=(
        "Return token usage aggregated by date, model, provider, "
        "session and agent."
    ),
)
async def get_token_usage(
    start_date: str
    | None = Query(
        None,
        description="Start date YYYY-MM-DD (inclusive). Default: 30 days ago",
    ),
    end_date: str
    | None = Query(
        None,
        description="End date YYYY-MM-DD (inclusive). Default: today",
    ),
    model: str
    | None = Query(
        None,
        description="Filter by model name",
    ),
    provider: str
    | None = Query(
        None,
        description="Filter by provider ID",
    ),
) -> TokenUsageSummary:
    """Return token usage summary for the given date range."""
    end_d = _parse_date(end_date) or date.today()
    start_d = _parse_date(start_date) or (end_d - timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    return await get_token_usage_manager().get_summary(
        start_date=start_d,
        end_date=end_d,
        model_name=model,
        provider_id=provider,
    )


@router.get(
    "/sessions/{session_id}",
    summary="Get token usage for a single session",
    description="Return token usage stats for the specified conversation session.",
)
async def get_session_token_usage(
    session_id: str,
) -> TokenUsageSessionStats:
    """Return token usage stats for a single session."""
    stats = await get_token_usage_manager().get_session_stats(session_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No token usage data found for session '{session_id}'",
        )
    return stats


@router.get(
    "/agents",
    summary="Get token usage for all agents",
    description="Return token usage stats aggregated per agent.",
)
async def get_all_agents_token_usage() -> dict[str, TokenUsageAgentStats]:
    """Return token usage stats for every known agent."""
    return await get_token_usage_manager().get_all_agent_stats()


@router.get(
    "/agents/{agent_id}",
    summary="Get token usage for a single agent",
    description="Return token usage stats for the specified agent.",
)
async def get_agent_token_usage(
    agent_id: str,
) -> TokenUsageAgentStats:
    """Return token usage stats for a single agent."""
    stats = await get_token_usage_manager().get_agent_stats(agent_id)
    if stats is None:
        raise HTTPException(
            status_code=404,
            detail=f"No token usage data found for agent '{agent_id}'",
        )
    return stats
