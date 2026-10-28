# -*- coding: utf-8 -*-
"""Normalize immutable legacy snapshots into the served Message contract."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from agentscope.message import Msg

from ..integrity import TranscriptManifest, build_transcript_manifest
from ..storage.paths import ChatStoragePaths
from ..utils import agentscope_msg_to_message
from ....constant import QWENPAW_USER_CONTENT_KEY
from ....schemas import Message, MessageType, Role, RunStatus
from .snapshot import LegacySnapshotManifest, validate_legacy_snapshot


class LegacyNormalizationError(RuntimeError):
    """A legacy source is readable but cannot be normalized safely."""


@dataclass(frozen=True)
class NormalizedTranscript:
    """Ordered, deduplicated served messages plus its correctness proof."""

    messages: tuple[Message, ...]
    manifest: TranscriptManifest
    history_parts: int
    session_parts: int
    deduplicated_parts: int
    runtime_context: bytes | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _LegacyPart:
    """One normalized legacy part plus proven equivalent representations."""

    key: str
    message: Message
    equivalent_fingerprints: frozenset[str] = frozenset()


_KNOWN_AGENT_BLOCK_TYPES = {
    "text",
    "thinking",
    "hint",
    "tool_use",
    "tool_call",
    "tool_result",
    "data",
    "image",
    "audio",
    "video",
    "file",
}
_TOOL_TYPES = {
    MessageType.PLUGIN_CALL,
    MessageType.PLUGIN_CALL_OUTPUT,
    MessageType.FUNCTION_CALL,
    MessageType.FUNCTION_CALL_OUTPUT,
    MessageType.MCP_TOOL_CALL,
    MessageType.MCP_TOOL_CALL_OUTPUT,
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_fingerprint(message: Message) -> str:
    payload = {
        "type": message.type.value,
        "role": message.role.value if message.role else None,
        "content": [
            (
                block.model_dump(mode="json")
                if hasattr(block, "model_dump")
                else block
            )
            for block in message.content
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _tool_call_id(message: Message) -> str | None:
    for block in message.content:
        data = getattr(block, "data", None)
        if isinstance(data, Mapping) and data.get("call_id") is not None:
            return str(data["call_id"])
    return None


def _stable_part_key(
    source_id: str,
    message: Message,
    occurrence: int,
) -> str:
    if message.type in _TOOL_TYPES:
        call_id = _tool_call_id(message)
        if call_id:
            return f"tool:{message.type.value}:{call_id}"
    return f"message:{source_id}:{message.type.value}:{occurrence}"


def _raw_context(data: dict[str, Any]) -> list[Any]:
    agent = data.get("agent")
    if not isinstance(agent, dict):
        agent = data
    state = agent.get("state")
    if isinstance(state, dict) and isinstance(state.get("context"), list):
        return list(state["context"])
    memory = agent.get("memory")
    if not isinstance(memory, dict):
        return []
    result: list[Any] = []
    for item in memory.get("content") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            result.append(item[0])
        else:
            result.append(item)
    return result


def _unknown_blocks(raw: Mapping[str, Any]) -> list[Any]:
    content = raw.get("content")
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, Mapping)
        and str(block.get("type", "text")) not in _KNOWN_AGENT_BLOCK_TYPES
    ]


def _convert_raw_message(
    raw: Any,
    *,
    fallback_id: str,
    source: str,
) -> list[_LegacyPart]:
    if not isinstance(raw, Mapping):
        raise LegacyNormalizationError(
            f"Legacy message is not an object at {source}",
        )
    try:
        normalized_raw = dict(raw)
        if not isinstance(normalized_raw.get("created_at"), str):
            normalized_raw["created_at"] = "1970-01-01T00:00:00"
        unknown = _unknown_blocks(raw)
        content = normalized_raw.get("content")
        if unknown and isinstance(content, list):
            normalized_raw["content"] = [
                (
                    {
                        "type": "text",
                        "text": _canonical_json(
                            {"legacy_unknown_block": block},
                        ),
                    }
                    if isinstance(block, Mapping)
                    and str(block.get("type", "text"))
                    not in _KNOWN_AGENT_BLOCK_TYPES
                    else block
                )
                for block in content
            ]
        agent_message = Msg.model_validate(normalized_raw)
    except Exception as exc:
        raise LegacyNormalizationError(
            f"Legacy message cannot be decoded at {source}",
        ) from exc
    converted = agentscope_msg_to_message(agent_message)
    rendered_fingerprints: dict[tuple[MessageType, int], str] = {}
    raw_metadata = normalized_raw.get("metadata")
    if (
        str(normalized_raw.get("role") or "") == Role.USER.value
        and isinstance(raw_metadata, dict)
        and QWENPAW_USER_CONTENT_KEY in raw_metadata
    ):
        rendered_raw = dict(normalized_raw)
        rendered_metadata = dict(raw_metadata)
        rendered_metadata.pop(QWENPAW_USER_CONTENT_KEY, None)
        rendered_raw["metadata"] = rendered_metadata
        try:
            rendered_message = Msg.model_validate(rendered_raw)
            rendered = agentscope_msg_to_message(rendered_message)
        except Exception as exc:
            raise LegacyNormalizationError(
                f"Legacy rendered message cannot be decoded at {source}",
            ) from exc
        rendered_occurrences: dict[MessageType, int] = {}
        for message in rendered:
            occurrence = rendered_occurrences.get(message.type, 0)
            rendered_occurrences[message.type] = occurrence + 1
            rendered_fingerprints[
                (message.type, occurrence)
            ] = _content_fingerprint(message)
    source_id = str(raw.get("id") or fallback_id)
    occurrences: dict[MessageType, int] = {}
    result: list[_LegacyPart] = []
    for message in converted:
        occurrence = occurrences.get(message.type, 0)
        occurrences[message.type] = occurrence + 1
        metadata = dict(message.metadata or {})
        metadata["migration_source"] = source
        if source.startswith("history:") and not result:
            try:
                metadata["legacy_recall_seq"] = int(source.split(":", 1)[1])
            except ValueError:
                pass
        if unknown and not result:
            metadata["legacy_unknown_blocks"] = unknown
        message.metadata = metadata
        fingerprint = _content_fingerprint(message)
        rendered_fingerprint = rendered_fingerprints.get(
            (message.type, occurrence),
        )
        equivalents = frozenset(
            {rendered_fingerprint}
            if rendered_fingerprint and rendered_fingerprint != fingerprint
            else (),
        )
        result.append(
            _LegacyPart(
                key=_stable_part_key(source_id, message, occurrence),
                message=message,
                equivalent_fingerprints=equivalents,
            ),
        )
    return result


def _history_raw_message(row: Mapping[str, Any]) -> dict[str, Any]:
    blocks: Any = None
    raw_blocks = row.get("blocks")
    if isinstance(raw_blocks, str) and raw_blocks:
        try:
            blocks = json.loads(raw_blocks)
        except json.JSONDecodeError as exc:
            raise LegacyNormalizationError(
                f"Legacy history row {row.get('seq')} has invalid blocks",
            ) from exc
    if not isinstance(blocks, list) or not blocks:
        blocks = [{"type": "text", "text": str(row.get("content") or "")}]
    metadata: dict[str, Any] = {}
    raw_metadata = row.get("metadata")
    if isinstance(raw_metadata, str) and raw_metadata:
        try:
            decoded_metadata = json.loads(raw_metadata)
        except json.JSONDecodeError as exc:
            raise LegacyNormalizationError(
                f"Legacy history row {row.get('seq')} has invalid metadata",
            ) from exc
        if isinstance(decoded_metadata, dict):
            metadata = decoded_metadata
    role = str(row.get("role") or "assistant")
    return {
        "id": str(row.get("dedup_key") or f"history:{row.get('seq')}"),
        "name": str(row.get("name") or role),
        "role": role,
        "content": blocks,
        "metadata": metadata,
        "created_at": row.get("created_at"),
    }


def _iter_history_parts(
    root: Path,
    manifest: LegacySnapshotManifest,
) -> Iterable[_LegacyPart]:
    if manifest.history_rows_path is None:
        return
    path = root / manifest.history_rows_path
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LegacyNormalizationError(
                    f"Legacy history snapshot line {line_number} is invalid",
                ) from exc
            if not isinstance(row, dict):
                raise LegacyNormalizationError(
                    f"Legacy history snapshot line {line_number} "
                    f"is not an object",
                )
            yield from _convert_raw_message(
                _history_raw_message(row),
                fallback_id=f"history:{row.get('seq')}",
                source=f"history:{row.get('seq')}",
            )


def _iter_session_parts(
    root: Path,
    manifest: LegacySnapshotManifest,
) -> Iterable[_LegacyPart]:
    for source_file in manifest.files:
        path = root / source_file.snapshot_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LegacyNormalizationError(
                f"Legacy session snapshot is invalid: "
                f"{source_file.relative_path}",
            ) from exc
        if not isinstance(data, dict):
            raise LegacyNormalizationError(
                f"Legacy session snapshot is not an object: "
                f"{source_file.relative_path}",
            )
        for index, raw in enumerate(_raw_context(data)):
            fallback_hash = hashlib.sha256(
                _canonical_json(raw).encode("utf-8"),
            ).hexdigest()
            yield from _convert_raw_message(
                raw,
                fallback_id=(
                    f"{source_file.relative_path}:message:{index}:"
                    f"{fallback_hash}"
                ),
                source=f"session:{source_file.relative_path}:{index}",
            )


def _eviction_ranges(value: Any) -> list[tuple[int, int]]:
    if not isinstance(value, dict):
        return []
    agent = value.get("agent", value)
    if not isinstance(agent, dict):
        return []
    scroll = agent.get("scroll")
    if not isinstance(scroll, dict):
        return []
    index = scroll.get("index")
    if not isinstance(index, dict):
        return []
    tiers = index.get("tiers", index.get("levels", []))
    ranges: list[tuple[int, int]] = []
    if not isinstance(tiers, list):
        return ranges
    for tier in tiers:
        if not isinstance(tier, list):
            continue
        for block in tier:
            if not isinstance(block, dict):
                continue
            try:
                lower = int(block["seq_lo"])
                upper = int(block["seq_hi"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < lower <= upper:
                ranges.append((lower, upper))
    return ranges


def _history_sequences(
    root: Path,
    manifest: LegacySnapshotManifest,
) -> list[int]:
    if manifest.history_rows_path is None:
        return []
    path = root / manifest.history_rows_path
    sequences: list[int] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                sequences.append(int(row["seq"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return sorted(set(sequences))


def _missing_ranges(
    expected: list[tuple[int, int]],
    available: list[int],
) -> list[tuple[int, int]]:
    missing: list[tuple[int, int]] = []
    for lower, upper in sorted(set(expected)):
        cursor = lower
        for sequence in available:
            if sequence < lower:
                continue
            if sequence > upper:
                break
            if sequence > cursor:
                missing.append((cursor, sequence - 1))
            cursor = max(cursor, sequence + 1)
        if cursor <= upper:
            missing.append((cursor, upper))
    merged: list[tuple[int, int]] = []
    for lower, upper in missing:
        if merged and lower <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
        else:
            merged.append((lower, upper))
    return merged


def _source_gap_diagnostics(
    root: Path,
    manifest: LegacySnapshotManifest,
) -> tuple[dict[str, Any], ...]:
    expected: list[tuple[int, int]] = []
    for source_file in manifest.files:
        path = root / source_file.snapshot_path
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        expected.extend(_eviction_ranges(value))
    gaps = _missing_ranges(expected, _history_sequences(root, manifest))
    if not gaps:
        return ()
    return (
        {
            "code": "legacy_source_gap",
            "evidence": "scroll_eviction_index",
            "missing_ranges": [list(item) for item in gaps],
        },
    )


def _match_parts(
    history_parts: list[_LegacyPart],
    session_parts: list[_LegacyPart],
) -> dict[int, int]:
    history_by_key: dict[str, list[int]] = defaultdict(list)
    session_by_key: dict[str, list[int]] = defaultdict(list)
    history_fingerprints: list[str] = []
    session_fingerprints: list[str] = []
    for index, part in enumerate(history_parts):
        history_by_key[part.key].append(index)
        history_fingerprints.append(_content_fingerprint(part.message))
    for index, part in enumerate(session_parts):
        session_by_key[part.key].append(index)
        session_fingerprints.append(_content_fingerprint(part.message))

    matches: dict[int, int] = {}
    used_history: set[int] = set()
    for session_index, part in enumerate(session_parts):
        fingerprints = {
            session_fingerprints[session_index],
            *part.equivalent_fingerprints,
        }
        candidates = [
            history_index
            for history_index in history_by_key.get(part.key, ())
            if history_index not in used_history
            and history_fingerprints[history_index] in fingerprints
        ]
        if not candidates:
            continue
        exact = [
            history_index
            for history_index in candidates
            if history_fingerprints[history_index]
            == session_fingerprints[session_index]
        ]
        history_index = (exact or candidates)[0]
        matches[session_index] = history_index
        used_history.add(history_index)

    for key in history_by_key.keys() & session_by_key.keys():
        unmatched_history = set(history_by_key[key]) - used_history
        if unmatched_history:
            raise LegacyNormalizationError(
                f"Conflicting legacy message identity: {key}",
            )
    return matches


def _inherit_recall_sequences(
    history_parts: list[_LegacyPart],
    session_parts: list[_LegacyPart],
    matches: Mapping[int, int],
) -> None:
    for session_index, history_index in matches.items():
        history_metadata = history_parts[history_index].message.metadata or {}
        legacy_sequence = history_metadata.get("legacy_recall_seq")
        if legacy_sequence is None:
            continue
        message = session_parts[session_index].message
        metadata = dict(message.metadata or {})
        metadata["legacy_recall_seq"] = legacy_sequence
        message.metadata = metadata


def _assemble_aligned_parts(
    history_parts: list[_LegacyPart],
    session_parts: list[_LegacyPart],
    matches: Mapping[int, int],
) -> list[Message]:
    anchor_sessions = _increasing_match_indexes(matches)
    anchor_history = {
        matches[session_index]: session_index
        for session_index in anchor_sessions
    }
    removed_history = {
        history_index
        for session_index, history_index in matches.items()
        if session_index not in anchor_sessions
    }
    before_anchor: dict[int, list[Message]] = {}
    pending: list[Message] = []
    for session_index, part in enumerate(session_parts):
        history_index = matches.get(session_index)
        if session_index in anchor_sessions and history_index is not None:
            before_anchor[history_index] = pending
            pending = []
            continue
        pending.append(part.message)

    merged: list[Message] = []
    for history_index, part in enumerate(history_parts):
        if history_index in removed_history:
            continue
        merged.extend(before_anchor.get(history_index, ()))
        session_index = anchor_history.get(history_index)
        if session_index is None:
            merged.append(part.message)
        else:
            merged.append(session_parts[session_index].message)
    merged.extend(pending)
    return merged


def _merge_parts(
    history_parts: list[_LegacyPart],
    session_parts: list[_LegacyPart],
) -> tuple[list[Message], int]:
    matches = _match_parts(history_parts, session_parts)
    _inherit_recall_sequences(history_parts, session_parts, matches)
    return (
        _assemble_aligned_parts(history_parts, session_parts, matches),
        len(matches),
    )


def _increasing_match_indexes(matches: Mapping[int, int]) -> set[int]:
    """Return session indexes in a longest increasing history alignment."""
    ordered = sorted(matches.items())
    if not ordered:
        return set()
    tails: list[int] = []
    tail_positions: list[int] = []
    previous = [-1] * len(ordered)
    for position, (_session_index, history_index) in enumerate(ordered):
        slot = bisect_left(tails, history_index)
        if slot == len(tails):
            tails.append(history_index)
            tail_positions.append(position)
        else:
            tails[slot] = history_index
            tail_positions[slot] = position
        if slot:
            previous[position] = tail_positions[slot - 1]
    selected: set[int] = set()
    position = tail_positions[-1]
    while position >= 0:
        selected.add(ordered[position][0])
        position = previous[position]
    return selected


def normalize_legacy_snapshot(
    workspace: Path,
    manifest: LegacySnapshotManifest,
) -> NormalizedTranscript:
    """Normalize and deduplicate one validated immutable source snapshot."""
    validate_legacy_snapshot(workspace, manifest)
    root = (
        ChatStoragePaths(workspace).root
        / "migration"
        / manifest.conversation_id
        / "snapshot"
    )
    history_parts = list(_iter_history_parts(root, manifest))
    session_parts = list(_iter_session_parts(root, manifest))
    runtime_context = None
    if manifest.files:
        latest = max(manifest.files, key=lambda item: item.mtime_ns)
        context_path = root / latest.snapshot_path
        try:
            context_value = json.loads(
                context_path.read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise LegacyNormalizationError(
                f"Legacy runtime context is invalid: {latest.relative_path}",
            ) from exc
        if isinstance(context_value, dict):
            agent_context = context_value.get("agent", context_value)
            if isinstance(agent_context, dict):
                runtime_context = _canonical_json(agent_context).encode(
                    "utf-8",
                )
    messages, deduplicated = _merge_parts(history_parts, session_parts)
    for message in messages:
        message.status = RunStatus.Completed
        if message.role is None:
            message.role = Role.ASSISTANT
    return NormalizedTranscript(
        messages=tuple(messages),
        manifest=build_transcript_manifest(messages),
        history_parts=len(history_parts),
        session_parts=len(session_parts),
        deduplicated_parts=deduplicated,
        runtime_context=runtime_context,
        diagnostics=_source_gap_diagnostics(root, manifest),
    )
