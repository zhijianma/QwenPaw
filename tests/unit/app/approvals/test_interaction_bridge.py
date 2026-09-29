# -*- coding: utf-8 -*-
"""Tests for the legacy Approval to unified Interaction migration bridge."""

# pylint: disable=protected-access

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from qwenpaw.app.approvals.interaction_bridge import (
    attach_pending_to_interaction,
)
from qwenpaw.app.approvals.models import ApprovalRequestSummary
from qwenpaw.app.approvals.service import ApprovalService
from qwenpaw.governance import tool_adapter
from qwenpaw.governance.policy import ToolCallSpec
from qwenpaw.interactions import InteractionService
from qwenpaw.kernel import (
    ActorRef,
    ActorType,
    InteractionResponse,
    InteractionStatus,
)
from qwenpaw.security.tool_guard.approval import (
    ApprovalDecision,
    ApprovalScope,
)

INVOCATION_ID = UUID("00000000-0000-0000-0000-000000000101")


class _Governor:
    """Minimal approval consumer used by the native-tool bridge test."""

    def __init__(self) -> None:
        self.audits: list[tuple[ToolCallSpec, object]] = []
        self.approved: list[tuple[ToolCallSpec, str]] = []

    def audit(self, spec: ToolCallSpec, decision: object) -> None:
        self.audits.append((spec, decision))

    async def add_approved_rule(
        self,
        spec: ToolCallSpec,
        *,
        generalized_target: str,
    ) -> bool:
        self.approved.append((spec, generalized_target))
        return True


async def _pending(service: ApprovalService):
    return await service.create_pending_summary(
        session_id="console:legacy",
        root_session_id="console:legacy",
        owner_agent_id="default",
        user_id="local-user",
        channel="console",
        agent_id="default",
        summary=ApprovalRequestSummary(
            source_type="tool_guard",
            name="run_shell",
            severity="high",
            result_summary="Shell execution requires approval.",
        ),
    )


def _context(interactions: InteractionService) -> dict:
    return {
        "_interaction_service": interactions,
        "os_conversation_id": "chat-spec-1",
        "os_invocation_id": str(INVOCATION_ID),
    }


@pytest.mark.asyncio
async def test_native_tool_approval_is_visible_on_chat_plane(
    tmp_path,
    monkeypatch,
) -> None:
    """PolicyGuardedTool must publish its approval to InteractionService."""
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    governor = _Governor()
    monkeypatch.setattr(
        "qwenpaw.app.approvals.get_approval_service",
        lambda: approvals,
    )

    import qwenpaw.governance.generalize as generalize_module

    async def _generalize(
        _tool_name,
        target,
        _source,
        agent_id=None,
    ):  # noqa: ANN
        del agent_id
        return target

    monkeypatch.setattr(
        generalize_module,
        "generalize_target_for_approval",
        _generalize,
    )
    context = {
        **_context(interactions),
        "agent_id": "default",
        "session_id": "console:fork-child",
        "root_session_id": "console:fork-child",
        "root_agent_id": "default",
        "user_id": "local-user",
        "channel": "console",
    }
    approval_task = asyncio.create_task(
        tool_adapter._ask_user_approval(
            governor=governor,
            tc_spec=ToolCallSpec(
                tool_name="Grep",
                target="README.md",
                agent_id="default",
                session_id="console:fork-child",
                raw_params={"pattern": "QwenPaw"},
                invocation_id=str(INVOCATION_ID),
            ),
            request_context=context,
            source="STRICT mode",
        ),
    )

    for _ in range(20):
        opened = await interactions.list_open(
            agent_id="default",
            conversation_id="chat-spec-1",
        )
        if opened:
            break
        await asyncio.sleep(0)
    assert len(opened) == 1
    await interactions.resolve(
        InteractionResponse(
            interaction_id=opened[0].interaction_id,
            idempotency_key="approve-native-tool",
            expected_revision=opened[0].revision,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("approve_exact",),
        ),
    )
    decision = await approval_task

    assert decision.behavior.value == "allow"
    assert governor.approved[0][1] == "README.md"


@pytest.mark.asyncio
async def test_bridge_opens_redacted_chat_owned_interaction(tmp_path) -> None:
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    pending = await _pending(approvals)

    attached = await attach_pending_to_interaction(
        _context(interactions),
        pending,
        approvals,
        source="tool_guard",
        input_data={"command": "echo ok", "api_key": "do-not-store"},
    )
    opened = await interactions.list_open(
        agent_id="default",
        conversation_id="chat-spec-1",
    )

    assert attached is True
    assert len(opened) == 1
    assert opened[0].interaction_id == UUID(pending.request_id)
    assert opened[0].invocation_id == INVOCATION_ID
    assert opened[0].metadata["arguments"] == {
        "command": "echo ok",
        "api_key": "[REDACTED]",
    }
    assert "session_id" not in opened[0].model_dump()

    await approvals.resolve_request(
        pending.request_id,
        ApprovalDecision.DENIED,
    )
    assert pending.interaction_id == UUID(pending.request_id)


@pytest.mark.asyncio
async def test_legacy_decision_commits_interaction_before_future_wakes(
    tmp_path,
) -> None:
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    pending = await _pending(approvals)
    downstream = AsyncMock()
    pending.resolution_hook = downstream
    await attach_pending_to_interaction(
        _context(interactions),
        pending,
        approvals,
        source="tool_guard",
        input_data={},
    )

    resolved = await approvals.resolve_request(
        pending.request_id,
        ApprovalDecision.APPROVED,
        scope=ApprovalScope.EXACT,
    )
    durable = await interactions.get_resolution(UUID(pending.request_id))

    assert resolved is pending
    assert durable is not None
    assert durable.status is InteractionStatus.RESOLVED
    assert durable.response is not None
    assert durable.response.selected_option_ids == ("approve_exact",)
    assert pending.future.result() is ApprovalDecision.APPROVED
    downstream.assert_awaited_once()
    assert pending.interaction_id == UUID(pending.request_id)


@pytest.mark.asyncio
async def test_runtime_cancellation_releases_legacy_approval_waiter(
    tmp_path,
) -> None:
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    pending = await _pending(approvals)
    downstream = AsyncMock()
    pending.resolution_hook = downstream
    await attach_pending_to_interaction(
        _context(interactions),
        pending,
        approvals,
        source="tool_guard",
        input_data={},
    )

    await interactions.cancel_invocation(
        INVOCATION_ID,
        detail="invocation interrupted",
    )

    assert pending.status == "cancelled"
    assert pending.future.result() is ApprovalDecision.DENIED
    assert await approvals.get_request(pending.request_id) is None
    downstream.assert_awaited_once_with(
        ApprovalDecision.DENIED,
        None,
        None,
    )


@pytest.mark.asyncio
async def test_unified_decision_wakes_legacy_waiter_with_scope(
    tmp_path,
) -> None:
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    pending = await _pending(approvals)
    downstream = AsyncMock()
    pending.resolution_hook = downstream
    await attach_pending_to_interaction(
        _context(interactions),
        pending,
        approvals,
        source="tool_guard",
        input_data={},
    )

    await interactions.resolve(
        InteractionResponse(
            interaction_id=UUID(pending.request_id),
            idempotency_key="unified-console-decision",
            expected_revision=1,
            actor=ActorRef(type=ActorType.USER, id="local-user"),
            selected_option_ids=("approve_similar",),
        ),
    )

    assert pending.status == ApprovalDecision.APPROVED.value
    assert pending.scope is ApprovalScope.SIMILAR
    assert pending.future.result() is ApprovalDecision.APPROVED
    downstream.assert_awaited_once_with(
        ApprovalDecision.APPROVED,
        ApprovalScope.SIMILAR,
        None,
    )


@pytest.mark.asyncio
async def test_bridge_closes_projection_after_fast_legacy_resolution(
    tmp_path,
) -> None:
    approvals = ApprovalService()
    interactions = InteractionService(tmp_path / "interactions.sqlite3")
    pending = await _pending(approvals)
    await approvals.resolve_request(
        pending.request_id,
        ApprovalDecision.DENIED,
    )

    attached = await attach_pending_to_interaction(
        _context(interactions),
        pending,
        approvals,
        source="tool_guard",
        input_data={},
    )
    resolution = await interactions.get_resolution(
        UUID(pending.request_id),
    )

    assert attached is True
    assert resolution is not None
    assert resolution.status is InteractionStatus.RESOLVED
    assert resolution.response is not None
    assert resolution.response.selected_option_ids == ("deny",)
