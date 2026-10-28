# -*- coding: utf-8 -*-
"""Opaque, tamper-resistant cursors for Chat history pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


CURSOR_SCHEMA_VERSION = 1
_KEY_BYTES = 32


class InvalidChatCursorError(ValueError):
    """A cursor is malformed, forged, or outside the requested scope."""


@dataclass(frozen=True)
class ChatCursorScope:
    """Immutable authorization and history-view scope for one cursor."""

    conversation_id: str
    head_turn_id: str | None
    view_version: str
    level: str
    subject: str


@dataclass(frozen=True)
class DecodedChatCursor:
    """Verified continuation state extracted from an opaque cursor."""

    direction: str
    position: dict[str, Any]


def _encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_part(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            f"{value}{padding}",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise InvalidChatCursorError("Invalid Chat cursor") from exc


def load_or_create_cursor_key(storage_root: Path) -> bytes:
    """Load a workspace key, creating it without overwriting a peer."""
    storage_root.mkdir(parents=True, exist_ok=True)
    key_path = storage_root / "cursor.key"
    try:
        descriptor = os.open(
            key_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        key = key_path.read_bytes()
    else:
        try:
            key = os.urandom(_KEY_BYTES)
            os.write(descriptor, key)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if len(key) != _KEY_BYTES:
        raise RuntimeError("Invalid Chat cursor signing key")
    return key


class ChatCursorCodec:
    """Sign and verify versioned cursor payloads using HMAC-SHA256."""

    def __init__(self, key: bytes) -> None:
        if len(key) < _KEY_BYTES:
            raise ValueError("Chat cursor key must contain at least 32 bytes")
        self._key = key

    def encode(
        self,
        scope: ChatCursorScope,
        *,
        direction: str,
        position: Mapping[str, Any],
    ) -> str:
        """Return an opaque cursor bound to the supplied immutable scope."""
        if direction not in {"older", "newer"}:
            raise ValueError("Unsupported Chat cursor direction")
        payload = {
            "direction": direction,
            "position": dict(position),
            "schema_version": CURSOR_SCHEMA_VERSION,
            "scope": asdict(scope),
        }
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._key, raw, hashlib.sha256).digest()
        return f"{_encode_part(raw)}.{_encode_part(signature)}"

    def decode(
        self,
        token: str,
        *,
        expected_scope: ChatCursorScope,
    ) -> DecodedChatCursor:
        """Verify a token and reject every cross-scope reuse attempt."""
        try:
            payload_part, signature_part = token.split(".", 1)
        except (AttributeError, ValueError) as exc:
            raise InvalidChatCursorError("Invalid Chat cursor") from exc
        raw = _decode_part(payload_part)
        signature = _decode_part(signature_part)
        expected_signature = hmac.new(
            self._key,
            raw,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise InvalidChatCursorError("Invalid Chat cursor")
        try:
            payload = json.loads(raw.decode("utf-8"))
            version = payload["schema_version"]
            scope = payload["scope"]
            direction = payload["direction"]
            position = payload["position"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise InvalidChatCursorError("Invalid Chat cursor") from exc
        if version != CURSOR_SCHEMA_VERSION:
            raise InvalidChatCursorError("Invalid Chat cursor")
        if scope != asdict(expected_scope):
            raise InvalidChatCursorError("Invalid Chat cursor")
        if direction not in {"older", "newer"}:
            raise InvalidChatCursorError("Invalid Chat cursor")
        if not isinstance(position, dict):
            raise InvalidChatCursorError("Invalid Chat cursor")
        return DecodedChatCursor(
            direction=direction,
            position=position,
        )
