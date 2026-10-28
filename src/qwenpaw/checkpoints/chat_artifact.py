# -*- coding: utf-8 -*-
"""Per-Chat SQLite artifacts embedded in checkpoint Git trees."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..app.chats.storage import (
    CONVERSATION_SCHEMA_VERSION,
    ChatStorageSchemaError,
    sqlite_online_backup,
    validate_conversation_database,
)
from ..app.chats.storage.common import connect_database
from .models import CheckpointError


CHAT_ARTIFACT_DATABASE = ".qwenpaw/checkpoint-chat/conversation.db"
CHAT_ARTIFACT_MANIFEST = ".qwenpaw/checkpoint-chat/manifest.json"
CHAT_ARTIFACT_FORMAT = "qwenpaw-chat-checkpoint.v2"


@dataclass(frozen=True)
class ChatCheckpointManifest:
    """Validated identity and preview metadata for one Chat artifact."""

    conversation_id: str
    schema_version: int
    source_generation: int
    head_turn_id: str | None
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": CHAT_ARTIFACT_FORMAT,
            "conversation_id": self.conversation_id,
            "schema_version": self.schema_version,
            "source_generation": self.source_generation,
            "head_turn_id": self.head_turn_id,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_chat_checkpoint_artifact(
    source: Path,
    destination: Path,
    *,
    conversation_id: str,
) -> tuple[Path, Path, ChatCheckpointManifest]:
    """Create a portable SQLite artifact plus canonical manifest."""
    destination.mkdir(parents=True, exist_ok=True)
    database = destination / "conversation.db"
    sqlite_online_backup(source, database)
    validate_conversation_database(
        database,
        expected_conversation_id=conversation_id,
    )
    with connect_database(database, readonly=True) as connection:
        meta = connection.execute(
            "SELECT source_generation, head_turn_id FROM chat_meta "
            "WHERE singleton_id = 1",
        ).fetchone()
    if meta is None:
        raise ChatStorageSchemaError("Chat artifact has no metadata")
    manifest = ChatCheckpointManifest(
        conversation_id=conversation_id,
        schema_version=CONVERSATION_SCHEMA_VERSION,
        source_generation=int(meta["source_generation"]),
        head_turn_id=(
            str(meta["head_turn_id"])
            if meta["head_turn_id"] is not None
            else None
        ),
        bytes=database.stat().st_size,
        sha256=_sha256_file(database),
    )
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return database, manifest_path, manifest


def parse_chat_checkpoint_manifest(
    blob: bytes,
) -> ChatCheckpointManifest:
    """Parse and structurally validate one untrusted manifest blob."""
    try:
        value = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError("Chat checkpoint manifest is invalid") from exc
    if not isinstance(value, dict) or value.get("format") != (
        CHAT_ARTIFACT_FORMAT
    ):
        raise CheckpointError("Chat checkpoint manifest format is unsupported")
    try:
        manifest = ChatCheckpointManifest(
            conversation_id=str(value["conversation_id"]),
            schema_version=int(value["schema_version"]),
            source_generation=int(value["source_generation"]),
            head_turn_id=(
                str(value["head_turn_id"])
                if value.get("head_turn_id") is not None
                else None
            ),
            bytes=int(value["bytes"]),
            sha256=str(value["sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointError(
            "Chat checkpoint manifest fields are invalid",
        ) from exc
    if (
        manifest.schema_version != CONVERSATION_SCHEMA_VERSION
        or manifest.source_generation < 0
        or manifest.bytes < 1
        or len(manifest.sha256) != 64
    ):
        raise CheckpointError("Chat checkpoint manifest contract failed")
    return manifest


def validate_chat_checkpoint_blob(
    database_blob: bytes,
    manifest: ChatCheckpointManifest,
    *,
    expected_conversation_id: str,
) -> None:
    """Validate hash, schema and physical identity before live mutation."""
    if manifest.conversation_id != expected_conversation_id:
        raise CheckpointError(
            "Chat checkpoint belongs to another conversation",
        )
    if len(database_blob) != manifest.bytes:
        raise CheckpointError("Chat checkpoint database size mismatch")
    if _sha256_bytes(database_blob) != manifest.sha256:
        raise CheckpointError("Chat checkpoint database hash mismatch")
    with tempfile.TemporaryDirectory(prefix="qwenpaw-chat-validate-") as tmp:
        database = Path(tmp) / "conversation.db"
        database.write_bytes(database_blob)
        try:
            validate_conversation_database(
                database,
                expected_conversation_id=expected_conversation_id,
            )
        except (OSError, ChatStorageSchemaError) as exc:
            raise CheckpointError(
                f"Chat checkpoint database validation failed: {exc}",
            ) from exc
        with connect_database(database, readonly=True) as connection:
            row = connection.execute(
                "SELECT source_generation, head_turn_id FROM chat_meta "
                "WHERE singleton_id = 1",
            ).fetchone()
        if (
            row is None
            or int(row["source_generation"]) != manifest.source_generation
            or (
                str(row["head_turn_id"])
                if row["head_turn_id"] is not None
                else None
            )
            != manifest.head_turn_id
        ):
            raise CheckpointError(
                "Chat checkpoint manifest does not match its database",
            )


def replace_chat_database(
    target: Path,
    database_blob: bytes,
) -> None:
    """Atomically replace one validated Chat DB and remove stale sidecars."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.checkpoint-restore")
    try:
        temporary.write_bytes(database_blob)
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(target.parent, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
