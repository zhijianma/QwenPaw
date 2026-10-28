# -*- coding: utf-8 -*-
"""Tests for scoped Chat pagination cursors."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.app.chats.pagination_cursor import (
    ChatCursorCodec,
    ChatCursorScope,
    InvalidChatCursorError,
    load_or_create_cursor_key,
)


def _scope(**changes: str) -> ChatCursorScope:
    values = {
        "conversation_id": "conversation",
        "head_turn_id": "head",
        "view_version": "view-version",
        "level": "turn",
        "subject": "agent",
    }
    values.update(changes)
    return ChatCursorScope(**values)


def test_cursor_round_trips_verified_position() -> None:
    codec = ChatCursorCodec(b"k" * 32)
    token = codec.encode(
        _scope(),
        direction="older",
        position={"turn_id": "turn-7"},
    )

    decoded = codec.decode(token, expected_scope=_scope())

    assert decoded.direction == "older"
    assert decoded.position == {"turn_id": "turn-7"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("conversation_id", "another-conversation"),
        ("head_turn_id", "another-head"),
        ("view_version", "another-view-version"),
        ("level", "message"),
        ("subject", "another-agent"),
    ],
)
def test_cursor_rejects_cross_scope_reuse(field: str, value: str) -> None:
    codec = ChatCursorCodec(b"k" * 32)
    token = codec.encode(
        _scope(),
        direction="older",
        position={"turn_id": "turn-7"},
    )

    with pytest.raises(InvalidChatCursorError):
        codec.decode(token, expected_scope=_scope(**{field: value}))


def test_cursor_rejects_tampering() -> None:
    codec = ChatCursorCodec(b"k" * 32)
    token = codec.encode(
        _scope(),
        direction="newer",
        position={"turn_id": "turn-7"},
    )
    payload, signature = token.split(".")
    replacement = "A" if payload[-1] != "A" else "B"
    tampered = f"{payload[:-1]}{replacement}.{signature}"

    with pytest.raises(InvalidChatCursorError):
        codec.decode(tampered, expected_scope=_scope())


def test_cursor_key_is_persistent_and_not_overwritten(tmp_path: Path) -> None:
    first = load_or_create_cursor_key(tmp_path)
    second = load_or_create_cursor_key(tmp_path)

    assert len(first) == 32
    assert second == first
    assert (tmp_path / "cursor.key").read_bytes() == first
