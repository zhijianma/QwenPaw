# -*- coding: utf-8 -*-
"""Project public Console SSE into safe durable task signals."""

from __future__ import annotations

import json
from typing import Any

from ..kernel.models import RunnerSignal

_TEXT_CHUNK_SIZE = 512
_TOOL_PREVIEW_LIMIT = 8 * 1024


def _parse_sse_payload(line: str) -> dict[str, Any] | None:
    """Parse one Console SSE data line without accepting other fields."""
    stripped = line.strip()
    if not stripped.startswith("data: "):
        return None
    try:
        payload = json.loads(stripped[6:])
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _completed_response_content(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Extract the final public assistant message from a response."""
    if payload.get("object") != "response":
        return ()
    if str(payload.get("status", "")).lower() != "completed":
        return ()
    output = payload.get("output")
    if not isinstance(output, list):
        return ()
    for message in reversed(output):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in (None, "assistant"):
            continue
        if message.get("type") not in (None, "message"):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        public_parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if not isinstance(part_type, str):
                continue
            fields = {
                "text": ("text",),
                "refusal": ("refusal",),
                "image": ("image_url",),
                "video": ("video_url",),
                "audio": ("data", "format"),
                "file": (
                    "file_url",
                    "file_data",
                    "filename",
                    "media_type",
                ),
            }.get(part_type)
            if fields is None:
                continue
            normalized = {"type": part_type}
            for field in fields:
                value = part.get(field)
                if isinstance(value, str) and value:
                    normalized[field] = value
            if len(normalized) > 1:
                public_parts.append(normalized)
        if public_parts:
            return tuple(public_parts)
    return ()


def _text_from_content(content: tuple[dict[str, Any], ...]) -> str:
    """Join visible text without serializing media payloads."""
    return "\n".join(
        str(part["text"])
        for part in content
        if part.get("type") == "text" and part.get("text")
    )


def _usage_signal(payload: dict[str, Any]) -> RunnerSignal | None:
    """Translate the Console turn telemetry into a canonical delta."""
    if payload.get("type") != "turn_usage":
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    delta = {
        "input_tokens": max(int(usage.get("prompt_tokens", 0) or 0), 0),
        "output_tokens": max(
            int(usage.get("completion_tokens", 0) or 0),
            0,
        ),
        "cost_micros": max(int(usage.get("cost_micros", 0) or 0), 0),
    }
    if "cost_micros" not in usage:
        delta["cost_unknown"] = True
    if not any(delta.values()):
        return None
    return RunnerSignal(
        event_type="usage.recorded",
        source="qwenpaw.system.tasks.console-agent",
        payload={"delta": delta},
    )


def _tool_preview(value: Any) -> tuple[str, bool]:
    """Return a bounded public tool field and an explicit truncation flag."""
    text = value if isinstance(value, str) else str(value or "")
    if len(text) <= _TOOL_PREVIEW_LIMIT:
        return text, False
    return text[:_TOOL_PREVIEW_LIMIT], True


class ConsoleStreamCapture:
    """Convert visible Console text into bounded conversation events."""

    def __init__(self) -> None:
        self._buffer = ""
        self._completed_text = ""
        self._completed_content: tuple[dict[str, Any], ...] = ()
        self._responding_emitted = False
        self._seen_tool_calls: set[str] = set()
        self._seen_tool_results: set[str] = set()

    @property
    def completed_text(self) -> str:
        """Return the final assistant-visible response, if completed."""
        return self._completed_text

    @property
    def completed_content(self) -> tuple[dict[str, Any], ...]:
        """Return the final public content before Artifact extraction."""
        return self._completed_content

    def feed(self, line: str) -> tuple[RunnerSignal, ...]:
        """Consume one SSE line and return newly completed signals."""
        payload = _parse_sse_payload(line)
        if payload is None:
            return ()
        direct_signal = _usage_signal(payload) or self._tool_signal(payload)
        if direct_signal is not None:
            return (direct_signal,)
        signals: tuple[RunnerSignal, ...] = ()
        if (
            payload.get("object") == "content"
            and payload.get("type") == "text"
            and payload.get("delta") is True
        ):
            candidate = payload.get("text")
            if isinstance(candidate, str) and candidate:
                signals = self._mark_responding()
        else:
            content = _completed_response_content(payload)
            if content:
                self._completed_content = content
                text = _text_from_content(content)
                self._completed_text = text
                if text:
                    self._buffer += text
                    signals = self._drain(keep_remainder=False)
        return signals

    def _tool_signal(self, payload: dict[str, Any]) -> RunnerSignal | None:
        """Project each Console function call exactly once."""
        if payload.get("object") != "content" or payload.get("type") != "data":
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        call_id = data.get("call_id")
        name = data.get("name")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
        ):
            return None
        status = str(payload.get("status") or "").lower()
        if "output" in data:
            if status != "completed" or call_id in self._seen_tool_results:
                return None
            self._seen_tool_results.add(call_id)
            output, truncated = _tool_preview(data.get("output"))
            return RunnerSignal(
                event_type="tool.completed",
                source="qwenpaw.system.tasks.console-agent",
                payload={
                    "call_id": call_id,
                    "name": name,
                    "output": output,
                    "output_truncated": truncated,
                },
            )
        if (
            status != "completed"
            or payload.get("delta") is True
            or call_id in self._seen_tool_calls
        ):
            return None
        self._seen_tool_calls.add(call_id)
        arguments, truncated = _tool_preview(data.get("arguments"))
        return RunnerSignal(
            event_type="tool.started",
            source="qwenpaw.system.tasks.console-agent",
            payload={
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "arguments_truncated": truncated,
                "usage_accounted": True,
            },
        )

    def finish(self) -> tuple[RunnerSignal, ...]:
        """Flush the final partial visible-text chunk."""
        return self._drain(keep_remainder=False)

    def _drain(self, *, keep_remainder: bool) -> tuple[RunnerSignal, ...]:
        signals = list(self._mark_responding()) if self._buffer else []
        while len(self._buffer) >= _TEXT_CHUNK_SIZE:
            signals.append(self._pop_text_signal(_TEXT_CHUNK_SIZE))
        if not keep_remainder and self._buffer:
            signals.append(self._pop_text_signal(len(self._buffer)))
        return tuple(signals)

    def _mark_responding(self) -> tuple[RunnerSignal, ...]:
        if self._responding_emitted:
            return ()
        self._responding_emitted = True
        return (RunnerSignal(event_type="runner.responding"),)

    def _pop_text_signal(self, length: int) -> RunnerSignal:
        text = self._buffer[:length]
        self._buffer = self._buffer[length:]
        return RunnerSignal(
            event_type="conversation.assistant.delta",
            payload={"role": "assistant", "text": text},
        )
