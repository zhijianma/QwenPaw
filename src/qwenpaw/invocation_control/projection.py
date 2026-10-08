# -*- coding: utf-8 -*-
"""Application projection joining conversation control and interactions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..kernel import (
    ACTIVE_SUBMISSION_STATUSES,
    ConversationExecutionChain,
    ConversationExecutionState,
    ConversationRuntimeProjection,
    InteractionMode,
    ObservationPage,
    SubmissionStatus,
)


class ConversationRuntimeProjectionService:
    """Join current runtime facts and recent semantic activity."""

    def __init__(
        self,
        control: Any,
        interactions: Any,
        observations: Any | None = None,
        *,
        activity_limit: int = 50,
        execution_chain_limit: int = 20,
        execution_submission_limit: int = 200,
    ) -> None:
        self._control = control
        self._interactions = interactions
        self._observations = observations
        self._activity_limit = activity_limit
        if execution_chain_limit < 1 or execution_chain_limit > 100:
            raise ValueError(
                "execution_chain_limit must be between 1 and 100",
            )
        self._execution_chain_limit = execution_chain_limit
        if execution_submission_limit < 1 or execution_submission_limit > 999:
            raise ValueError(
                "execution_submission_limit must be between 1 and 999",
            )
        self._execution_submission_limit = execution_submission_limit

    async def _read_activity(self, conversation_id: str) -> ObservationPage:
        if self._observations is None:
            return ObservationPage()
        return await self._observations.page_for_conversation(
            conversation_id,
            limit=self._activity_limit,
        )

    async def read(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> ConversationRuntimeProjection:
        """Read current facts into one ownership-checked projection."""
        queue, opened, activity, submissions = await asyncio.gather(
            self._control.read_queue(
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
            self._interactions.list_open(
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
            self._read_activity(conversation_id),
            self._control.list_submissions_for_conversation(
                agent_id=agent_id,
                conversation_id=conversation_id,
                limit=self._execution_submission_limit + 1,
            ),
        )
        interactions = tuple(
            sorted(opened, key=lambda item: str(item.interaction_id)),
        )
        execution_window_truncated = (
            len(submissions) > self._execution_submission_limit
        )
        bounded_submissions = {
            item.submission_id: item
            for item in submissions[: self._execution_submission_limit]
        }
        bounded_submissions.update(
            {item.submission_id: item for item in queue.submissions},
        )
        execution_window_truncated = execution_window_truncated or (
            len(
                {
                    item.correlation_id
                    for item in bounded_submissions.values()
                },
            )
            > self._execution_chain_limit
        )
        execution_chains = self._execution_chains(
            conversation_id,
            tuple(bounded_submissions.values()),
            interactions,
        )
        return ConversationRuntimeProjection(
            agent_id=agent_id,
            conversation_id=conversation_id,
            queue=queue,
            interactions=interactions,
            execution_chains=execution_chains,
            execution_window_truncated=execution_window_truncated,
            activity=activity,
            cursor=self._cursor(
                queue,
                interactions,
                execution_chains,
                execution_window_truncated,
                activity,
            ),
        )

    def _execution_chains(
        self,
        conversation_id: str,
        submissions: tuple[Any, ...],
        interactions: tuple[Any, ...],
    ) -> tuple[ConversationExecutionChain, ...]:
        """Group authoritative attempts without inferring business outcome."""
        grouped: dict[Any, list[Any]] = {}
        for submission in sorted(submissions, key=lambda item: item.sequence):
            grouped.setdefault(submission.correlation_id, []).append(
                submission,
            )
        ordered = sorted(
            grouped.items(),
            key=lambda item: item[1][-1].sequence,
            reverse=True,
        )[: self._execution_chain_limit]
        result = []
        for correlation_id, chain_submissions in ordered:
            blocking = tuple(
                sorted(
                    (
                        item
                        for item in interactions
                        if item.correlation_id == correlation_id
                        and item.mode is InteractionMode.BLOCKING
                    ),
                    key=lambda item: str(item.interaction_id),
                ),
            )
            invocation_ids = tuple(
                dict.fromkeys(
                    item.invocation_id
                    for item in chain_submissions
                    if item.invocation_id is not None
                ),
            )
            head = chain_submissions[-1]
            head_invocation_id = next(
                (
                    item.invocation_id
                    for item in reversed(chain_submissions)
                    if item.invocation_id is not None
                ),
                None,
            )
            result.append(
                ConversationExecutionChain(
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    state=self._execution_state(
                        chain_submissions,
                        blocking,
                    ),
                    submission_ids=tuple(
                        item.submission_id for item in chain_submissions
                    ),
                    invocation_ids=invocation_ids,
                    head_submission_id=head.submission_id,
                    head_invocation_id=head_invocation_id,
                    latest_submission_status=head.status,
                    open_interaction_ids=tuple(
                        item.interaction_id for item in blocking
                    ),
                    accepted_at=chain_submissions[0].created_at,
                    latest_submission_at=max(
                        item.updated_at for item in chain_submissions
                    ),
                ),
            )
        return tuple(result)

    @staticmethod
    def _execution_state(
        submissions: list[Any],
        blocking_interactions: tuple[Any, ...],
    ) -> ConversationExecutionState:
        if blocking_interactions:
            return ConversationExecutionState.WAITING_USER
        if any(
            item.status in ACTIVE_SUBMISSION_STATUSES
            for item in submissions
        ):
            return ConversationExecutionState.RUNNING
        if any(item.status is SubmissionStatus.QUEUED for item in submissions):
            return ConversationExecutionState.QUEUED
        terminal_states = {
            SubmissionStatus.FAILED: ConversationExecutionState.FAILED,
            SubmissionStatus.INTERRUPTED: (
                ConversationExecutionState.INTERRUPTED
            ),
            SubmissionStatus.CANCELLED: ConversationExecutionState.CANCELLED,
            SubmissionStatus.SUCCEEDED: ConversationExecutionState.INACTIVE,
        }
        return terminal_states[submissions[-1].status]

    async def wait_for_change(
        self,
        *,
        agent_id: str,
        conversation_id: str,
        after_cursor: str,
        timeout: float,
        poll_interval: float = 0.5,
    ) -> ConversationRuntimeProjection | None:
        """Return the latest snapshot after a cursor or None on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            projection = await self.read(
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
            if projection.cursor != after_cursor:
                return projection
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(poll_interval, remaining))

    @staticmethod
    def _cursor(
        queue: Any,
        interactions: tuple[Any, ...],
        execution_chains: tuple[ConversationExecutionChain, ...],
        execution_window_truncated: bool,
        activity: ObservationPage,
    ) -> str:
        payload = {
            "queue": queue.model_dump(
                mode="json",
                exclude={"updated_at"},
            ),
            "interactions": [
                item.model_dump(mode="json") for item in interactions
            ],
            "execution_chains": [
                item.model_dump(mode="json") for item in execution_chains
            ],
            "execution_window_truncated": execution_window_truncated,
            "activity": activity.model_dump(mode="json"),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()[:24]
        return f"v3-{queue.revision}-{digest}"


__all__ = ["ConversationRuntimeProjectionService"]
