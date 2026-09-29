# -*- coding: utf-8 -*-
"""Tests for the mandatory proposal approval gate."""

from uuid import uuid4

import pytest

from qwenpaw.kernel.models import (
    ActorRef,
    ActorType,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ExecutionContract,
    Proposal,
    RiskLevel,
)
from qwenpaw.kernel.proposals import (
    ProposalNotApprovedError,
    approved_task_order,
)


def _proposal_flow(decision_value: ApprovalDecisionValue):
    proposal = Proposal(
        source="proactive.memory",
        objective="Review new issue",
        rationale_summary="A relevant issue was detected",
        risk=RiskLevel.MEDIUM,
    )
    approval = ApprovalRequest(
        task_id=uuid4(),
        action="proposal.execute",
        risk=proposal.risk,
        requester=ActorRef(type=ActorType.SYSTEM, id="proactive"),
        policy="strict",
        redacted_arguments={"proposal_id": str(proposal.proposal_id)},
    )
    decision = ApprovalDecision(
        approval_id=approval.approval_id,
        decision=decision_value,
        actor=ActorRef(type=ActorType.USER, id="local-user"),
        reason="Explicit user decision",
    )
    return proposal, approval, decision


def test_approved_proposal_becomes_task_order() -> None:
    proposal, approval, decision = _proposal_flow(
        ApprovalDecisionValue.APPROVED,
    )

    order = approved_task_order(
        proposal,
        approval,
        decision,
        task_id=approval.task_id,
    )

    assert order.objective == proposal.objective
    assert order.approval_ids == (approval.approval_id,)


def test_approved_proposal_preserves_execution_contract() -> None:
    contract = ExecutionContract(
        goal="Review new issue",
        acceptance=("Review is stored",),
    )
    original, approval, decision = _proposal_flow(
        ApprovalDecisionValue.APPROVED,
    )
    proposal = original.model_copy(
        update={"execution_contract": contract},
    )

    order = approved_task_order(
        proposal,
        approval,
        decision,
        task_id=approval.task_id,
    )

    assert order.execution_contract == contract
    assert order.acceptance_criteria == contract.acceptance


@pytest.mark.parametrize(
    "decision_value",
    [
        ApprovalDecisionValue.DENIED,
        ApprovalDecisionValue.CANCELLED,
        ApprovalDecisionValue.EXPIRED,
    ],
)
def test_unapproved_proposal_cannot_become_task_order(
    decision_value: ApprovalDecisionValue,
) -> None:
    proposal, approval, decision = _proposal_flow(decision_value)

    with pytest.raises(ProposalNotApprovedError):
        approved_task_order(
            proposal,
            approval,
            decision,
            task_id=approval.task_id,
        )
