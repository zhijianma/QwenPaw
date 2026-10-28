# -*- coding: utf-8 -*-
"""Deterministic transcript manifests for migration and regression checks."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from ...schemas import Message, MessageType


_TOOL_CALL_TYPES = {
    MessageType.PLUGIN_CALL.value,
    MessageType.FUNCTION_CALL.value,
    MessageType.MCP_TOOL_CALL.value,
}
_TOOL_RESULT_TYPES = {
    MessageType.PLUGIN_CALL_OUTPUT.value,
    MessageType.FUNCTION_CALL_OUTPUT.value,
    MessageType.MCP_TOOL_CALL_OUTPUT.value,
}


@dataclass(frozen=True)
class ToolLinkManifest:
    """Stable positions and names for one tool call identifier."""

    call_id: str
    call_positions: tuple[int, ...]
    result_positions: tuple[int, ...]
    call_names: tuple[str, ...]
    result_names: tuple[str, ...]


@dataclass(frozen=True)
class TranscriptManifest:
    """Compact correctness proof for one ordered transcript."""

    schema_version: int
    message_count: int
    block_count: int
    message_hashes: tuple[str, ...]
    transcript_hash: str
    role_counts: tuple[tuple[str, int], ...]
    type_counts: tuple[tuple[str, int], ...]
    usage_hashes: tuple[str, ...]
    tool_links: tuple[ToolLinkManifest, ...]
    orphan_tool_calls: tuple[str, ...]
    orphan_tool_results: tuple[str, ...]
    duplicate_tool_calls: tuple[str, ...]
    duplicate_tool_results: tuple[str, ...]
    tool_name_mismatches: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def _canonical_json(value: Any) -> bytes:
    """Encode JSON deterministically without losing Unicode content."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_message(
    value: Message | Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize a served message while excluding its derived display ID."""
    message = (
        value
        if isinstance(value, Message)
        else Message.model_validate(dict(value))
    )
    return message.model_dump(
        mode="json",
        exclude={"id"},
        exclude_none=False,
    )


def _tool_payload(message: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the first structured tool payload in a message."""
    for block in message.get("content") or []:
        if not isinstance(block, Mapping):
            continue
        data = block.get("data")
        if isinstance(data, Mapping):
            return data
    return {}


def _usage_payload(message: Mapping[str, Any]) -> Any:
    """Return turn usage from either served or AgentScope metadata shape."""
    metadata = message.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    usage = metadata.get("qwenpaw_turn_usage")
    if usage is not None:
        return usage
    source_metadata = metadata.get("metadata")
    if isinstance(source_metadata, Mapping):
        return source_metadata.get("qwenpaw_turn_usage")
    return None


class TranscriptManifestBuilder:
    """Incrementally build a manifest without retaining message payloads."""

    def __init__(self) -> None:
        self._message_hashes: list[str] = []
        self._transcript_hasher = hashlib.sha256()
        self._role_counts: Counter[str] = Counter()
        self._type_counts: Counter[str] = Counter()
        self._usage_hashes: list[str] = []
        self._block_count = 0
        self._tool_parts: dict[str, dict[str, list[Any]]] = {}

    def add(self, value: Message | Mapping[str, Any]) -> None:
        """Add one message to the ordered manifest."""
        message = _canonical_message(value)
        encoded = _canonical_json(message)
        self._message_hashes.append(_sha256_bytes(encoded))
        self._transcript_hasher.update(len(encoded).to_bytes(8, "big"))
        self._transcript_hasher.update(encoded)
        position = len(self._message_hashes) - 1
        self._role_counts[str(message.get("role"))] += 1
        message_type = str(message.get("type"))
        self._type_counts[message_type] += 1
        content = message.get("content") or []
        self._block_count += len(content)
        usage = _usage_payload(message)
        if usage is not None:
            self._usage_hashes.append(_sha256_bytes(_canonical_json(usage)))
        if message_type not in _TOOL_CALL_TYPES | _TOOL_RESULT_TYPES:
            return
        payload = _tool_payload(message)
        call_id = payload.get("call_id")
        if call_id is None:
            call_id = "<missing>"
        key = str(call_id)
        parts = self._tool_parts.setdefault(
            key,
            {
                "call_positions": [],
                "result_positions": [],
                "call_names": [],
                "result_names": [],
            },
        )
        name = payload.get("name")
        name_value = "" if name is None else str(name)
        if message_type in _TOOL_CALL_TYPES:
            parts["call_positions"].append(position)
            parts["call_names"].append(name_value)
        else:
            parts["result_positions"].append(position)
            parts["result_names"].append(name_value)

    def build(self) -> TranscriptManifest:
        """Finish and return the immutable manifest."""
        links = tuple(
            ToolLinkManifest(
                call_id=call_id,
                call_positions=tuple(parts["call_positions"]),
                result_positions=tuple(parts["result_positions"]),
                call_names=tuple(parts["call_names"]),
                result_names=tuple(parts["result_names"]),
            )
            for call_id, parts in sorted(self._tool_parts.items())
        )
        orphan_calls = tuple(
            link.call_id for link in links if not link.result_positions
        )
        orphan_results = tuple(
            link.call_id for link in links if not link.call_positions
        )
        duplicate_calls = tuple(
            link.call_id for link in links if len(link.call_positions) > 1
        )
        duplicate_results = tuple(
            link.call_id for link in links if len(link.result_positions) > 1
        )
        name_mismatches = tuple(
            link.call_id
            for link in links
            if link.call_names
            and link.result_names
            and set(link.call_names) != set(link.result_names)
        )
        return TranscriptManifest(
            schema_version=1,
            message_count=len(self._message_hashes),
            block_count=self._block_count,
            message_hashes=tuple(self._message_hashes),
            transcript_hash=self._transcript_hasher.hexdigest(),
            role_counts=tuple(sorted(self._role_counts.items())),
            type_counts=tuple(sorted(self._type_counts.items())),
            usage_hashes=tuple(self._usage_hashes),
            tool_links=links,
            orphan_tool_calls=orphan_calls,
            orphan_tool_results=orphan_results,
            duplicate_tool_calls=duplicate_calls,
            duplicate_tool_results=duplicate_results,
            tool_name_mismatches=name_mismatches,
        )


def build_transcript_manifest(
    messages: Iterable[Message | Mapping[str, Any]],
) -> TranscriptManifest:
    """Build an order-sensitive manifest for a visible transcript.

    The runtime ``Message.id`` is excluded because legacy conversion creates
    a fresh display envelope on every read. The persisted AgentScope identity
    remains covered through ``metadata.original_id``.
    """
    builder = TranscriptManifestBuilder()
    for message in messages:
        builder.add(message)
    return builder.build()
