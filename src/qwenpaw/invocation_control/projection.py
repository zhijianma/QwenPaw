# -*- coding: utf-8 -*-
"""Application projection joining conversation control and interactions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..kernel import ConversationRuntimeProjection, ObservationPage


class ConversationRuntimeProjectionService:
    """Join current runtime facts and recent semantic activity."""

    def __init__(
        self,
        control: Any,
        interactions: Any,
        observations: Any | None = None,
        *,
        activity_limit: int = 50,
    ) -> None:
        self._control = control
        self._interactions = interactions
        self._observations = observations
        self._activity_limit = activity_limit

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
        queue, opened, activity = await asyncio.gather(
            self._control.read_queue(
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
            self._interactions.list_open(
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
            self._read_activity(conversation_id),
        )
        interactions = tuple(
            sorted(opened, key=lambda item: str(item.interaction_id)),
        )
        return ConversationRuntimeProjection(
            agent_id=agent_id,
            conversation_id=conversation_id,
            queue=queue,
            interactions=interactions,
            activity=activity,
            cursor=self._cursor(queue, interactions, activity),
        )

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
            "activity": activity.model_dump(mode="json"),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()[:24]
        return f"v2-{queue.revision}-{digest}"


__all__ = ["ConversationRuntimeProjectionService"]
