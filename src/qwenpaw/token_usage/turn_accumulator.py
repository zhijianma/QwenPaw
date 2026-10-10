# -*- coding: utf-8 -*-
"""Process-local live usage projection for active Chat turns."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .models import TurnUsageEvidence


@dataclass(frozen=True)
class TurnUsageKey:
    """Stable ownership key for one live Chat turn."""

    chat_id: str
    invocation_id: str | None = None


def _merge_usage(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Return one cumulative snapshot without mutating either input."""
    merged = copy.deepcopy(current)
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for item in [
        *previous.get("model_routes", []),
        *current.get("model_routes", []),
    ]:
        if not isinstance(item, dict):
            continue
        route_key = (
            str(item.get("provider_id", "")),
            str(item.get("model_name", "")),
        )
        route = routes.setdefault(
            route_key,
            {
                "provider_id": route_key[0],
                "model_name": route_key[1],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "call_count": 0,
            },
        )
        route["prompt_tokens"] += int(
            item.get("prompt_tokens", 0) or 0,
        )
        route["completion_tokens"] += int(
            item.get("completion_tokens", 0) or 0,
        )
        route["total_tokens"] = (
            route["prompt_tokens"] + route["completion_tokens"]
        )
        route["call_count"] += int(item.get("call_count", 1) or 1)
        unavailable_calls = int(
            item.get("usage_unobserved_calls", 0) or 0,
        )
        if unavailable_calls > 0:
            route["usage_unobserved_calls"] = (
                int(
                    route.get("usage_unobserved_calls", 0) or 0,
                )
                + unavailable_calls
            )
    merged["model_routes"] = list(routes.values())
    for field_name in (
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_eligible_input_tokens",
    ):
        merged[field_name] = int(
            previous.get(field_name, 0) or 0,
        ) + int(current.get(field_name, 0) or 0)
    merged["total_tokens"] = (
        merged["prompt_tokens"] + merged["completion_tokens"]
    )
    merged["cache_observed"] = bool(
        previous.get("cache_observed", False)
        or current.get("cache_observed", False),
    )
    cache_eligible = merged["cache_eligible_input_tokens"]
    merged["cache_hit_rate"] = (
        merged["cache_read_tokens"] / cache_eligible * 100
        if cache_eligible > 0
        else None
    )
    unavailable_calls = int(
        previous.get("usage_unobserved_calls", 0) or 0,
    ) + int(current.get("usage_unobserved_calls", 0) or 0)
    if unavailable_calls > 0:
        merged["usage_unobserved_calls"] = unavailable_calls
        merged["measurement"] = (
            "partial" if merged["total_tokens"] > 0 else "unavailable"
        )
    return merged


class TurnUsageAccumulator:
    """Own live, content-free usage snapshots until a turn is committed."""

    def __init__(self) -> None:
        self._snapshots: dict[TurnUsageKey, TurnUsageEvidence] = {}
        self._lock = RLock()

    def record(
        self,
        chat_id: str,
        usage: dict[str, Any],
        *,
        invocation_id: str | None = None,
    ) -> None:
        """Accumulate one model-call usage delta for a live turn."""
        if not chat_id or not usage:
            return
        current = TurnUsageEvidence.model_validate(usage)
        if current.chat_id is not None and current.chat_id != chat_id:
            raise ValueError("turn usage chat_id does not match its owner")
        effective_invocation_id = invocation_id or current.turn_id
        if (
            current.turn_id is not None
            and effective_invocation_id != current.turn_id
        ):
            raise ValueError("turn usage turn_id does not match its owner")
        key = TurnUsageKey(chat_id, effective_invocation_id or None)
        with self._lock:
            previous = self._snapshots.get(key)
            self._snapshots[key] = (
                current
                if previous is None
                else TurnUsageEvidence.model_validate(
                    _merge_usage(previous.to_payload(), current.to_payload()),
                )
            )

    def peek(
        self,
        chat_id: str,
        *,
        invocation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read one live turn without consuming it."""
        with self._lock:
            key = self._resolve_key(chat_id, invocation_id)
            snapshot = self._snapshots.get(key) if key is not None else None
            return snapshot.to_payload() if snapshot is not None else None

    def pop(
        self,
        chat_id: str,
        *,
        invocation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Consume one exact or unambiguous live turn."""
        with self._lock:
            key = self._resolve_key(chat_id, invocation_id)
            snapshot = self._snapshots.pop(key, None) if key else None
            return snapshot.to_payload() if snapshot is not None else None

    def discard_chat(self, chat_id: str) -> None:
        """Discard all staged usage for one ChatSpec identity."""
        if not chat_id:
            return
        with self._lock:
            stale = [key for key in self._snapshots if key.chat_id == chat_id]
            for key in stale:
                del self._snapshots[key]

    def clear(self) -> None:
        """Discard all staged usage, primarily for process shutdown/tests."""
        with self._lock:
            self._snapshots.clear()

    def _resolve_key(
        self,
        chat_id: str,
        invocation_id: str | None,
    ) -> TurnUsageKey | None:
        if not chat_id:
            return None
        if invocation_id:
            return TurnUsageKey(chat_id, invocation_id)
        candidates = [key for key in self._snapshots if key.chat_id == chat_id]
        return candidates[0] if len(candidates) == 1 else None


_TURN_USAGE_ACCUMULATOR = TurnUsageAccumulator()


def get_turn_usage_accumulator() -> TurnUsageAccumulator:
    """Return the process-local live turn projection."""
    return _TURN_USAGE_ACCUMULATOR


__all__ = [
    "TurnUsageAccumulator",
    "TurnUsageKey",
    "get_turn_usage_accumulator",
]
