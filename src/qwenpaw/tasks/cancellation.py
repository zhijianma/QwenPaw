# -*- coding: utf-8 -*-
"""Cooperative cancellation primitive owned by the task runtime."""

from __future__ import annotations

import asyncio


class RuntimeCancellationToken:
    """Expose cancellation state without granting capabilities host control."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        """Return the first cancellation reason."""
        return self._reason

    def cancel(self, reason: str = "Task cancellation requested") -> None:
        """Signal cancellation exactly once from the host side."""
        if self._event.is_set():
            return
        self._reason = reason.strip() or "Task cancellation requested"
        self._event.set()

    async def wait(self) -> None:
        """Wait for cancellation without polling."""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """Raise the standard asyncio cancellation signal when requested."""
        if self.cancelled:
            raise asyncio.CancelledError(self._reason)
