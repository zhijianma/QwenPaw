# -*- coding: utf-8 -*-
"""Safety gate from proactive proposals to executable task orders."""

from uuid import UUID

from .models import (
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    Proposal,
    TaskOrder,
)


class ProposalNotApprovedError(PermissionError):
    """Raised when a proposal has no matching positive decision."""


def approved_task_order(
    proposal: Proposal,
    approval: ApprovalRequest,
    decision: ApprovalDecision,
    *,
    task_id: UUID,
) -> TaskOrder:
    """Create an executable order only after an exact approval decision."""
    if decision.approval_id != approval.approval_id:
        raise ProposalNotApprovedError("approval decision does not match")
    if decision.decision is not ApprovalDecisionValue.APPROVED:
        raise ProposalNotApprovedError("proposal was not approved")
    if approval.action != "proposal.execute":
        raise ProposalNotApprovedError("approval does not authorize execution")
    proposal_id = approval.redacted_arguments.get("proposal_id")
    if proposal_id != str(proposal.proposal_id):
        raise ProposalNotApprovedError("approval targets another proposal")
    return TaskOrder(
        task_id=task_id,
        objective=proposal.objective,
        acceptance_criteria=(
            proposal.execution_contract.acceptance
            if proposal.execution_contract is not None
            else ()
        ),
        requested_capabilities=proposal.requested_capabilities,
        approval_ids=(approval.approval_id,),
        execution_contract=proposal.execution_contract,
        metadata={"proposal_id": str(proposal.proposal_id)},
    )
