# -*- coding: utf-8 -*-
"""Application projection joining conversation control and interactions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from ..kernel import ConversationRuntimeProjection


class ConversationRuntimeProjectionService:
    """Build and follow one recoverable conversation runtime snapshot."""

    def __init__(self, control: Any, interactions: Any) -> None:
        self._control = control
        self._interactions = interactions

    async def read(
        self,
        *,
        agent_id: str,
        conversation_id: str,
    ) -> ConversationRuntimeProjection:
        """Read Queue and open Interactions into one ownership-checked view."""
        queue, opened = await asyncio.gather(
            self._control.read_queue(
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
            self._interactions.list_open(
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
        )
        interactions = tuple(
            sorted(opened, key=lambda item: str(item.interaction_id)),
        )
        return ConversationRuntimeProjection(
            agent_id=agent_id,
            conversation_id=conversation_id,
            queue=queue,
            interactions=interactions,
            cursor=self._cursor(queue, interactions),
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
    def _cursor(queue: Any, interactions: tuple[Any, ...]) -> str:
        payload = {
            "queue": queue.model_dump(
                mode="json",
                exclude={"updated_at"},
            ),
            "interactions": [
                item.model_dump(mode="json") for item in interactions
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()[:24]
        return f"v1-{queue.revision}-{digest}"


__all__ = ["ConversationRuntimeProjectionService"]
