# -*- coding: utf-8 -*-
"""Stable, resumable read-only snapshots of one legacy Chat's sources."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..storage.paths import ChatStoragePaths


class LegacySnapshotError(RuntimeError):
    """Legacy sources could not be captured without ambiguity or mutation."""


@dataclass(frozen=True)
class SnapshotFile:
    """One byte-for-byte session source in a migration snapshot."""

    relative_path: str
    snapshot_path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class LegacySnapshotManifest:
    """Auditable source boundary used by the normalization phase."""

    version: int
    conversation_id: str
    session_id: str
    files: tuple[SnapshotFile, ...]
    history_rows_path: str | None
    history_rows: int
    history_min_seq: int | None
    history_max_seq: int | None
    history_sha256: str | None
    manifest_sha256: str


_HISTORY_COLUMNS = (
    "seq",
    "session_id",
    "agent_id",
    "kind",
    "role",
    "name",
    "content",
    "tool_call_id",
    "tool_input",
    "tool_state",
    "headline",
    "blocks",
    "metadata",
    "created_at",
    "dedup_key",
)
_REQUIRED_HISTORY_COLUMNS = {"seq", "session_id", "kind"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source(sessions_dir: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise LegacySnapshotError(
            f"Legacy session path is not safe: {relative_path!r}",
        )
    source = sessions_dir.joinpath(*pure.parts)
    try:
        source.relative_to(sessions_dir)
    except ValueError as exc:
        raise LegacySnapshotError(
            f"Legacy session path escapes workspace: {relative_path!r}",
        ) from exc
    return source


def _safe_snapshot_artifact(root: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise LegacySnapshotError(
            f"Legacy snapshot artifact path is unsafe: {relative_path!r}",
        )
    return root.joinpath(*pure.parts)


def _copy_stable(source: Path, target: Path) -> SnapshotFile:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.part")
    for _attempt in range(3):
        try:
            before = source.stat()
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as reader, part.open("wb") as writer:
                while chunk := reader.read(64 * 1024):
                    writer.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            after = source.stat()
        except OSError as exc:
            raise LegacySnapshotError(
                f"Could not snapshot legacy session: {source}",
            ) from exc
        stable = (
            before.st_size == after.st_size == size
            and before.st_mtime_ns == after.st_mtime_ns
        )
        if stable:
            os.replace(part, target)
            return SnapshotFile(
                relative_path="",
                snapshot_path=target.name,
                size=size,
                mtime_ns=after.st_mtime_ns,
                sha256=digest.hexdigest(),
            )
    try:
        part.unlink(missing_ok=True)
    except OSError:
        pass
    raise LegacySnapshotError(
        f"Legacy session changed repeatedly while snapshotting: {source}",
    )


def _history_database_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _snapshot_history_rows(
    history_path: Path,
    target: Path,
    *,
    session_id: str,
) -> tuple[int, int | None, int | None]:
    if not history_path.is_file():
        return 0, None, None
    connection = sqlite3.connect(
        _history_database_uri(history_path),
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    part = target.with_name(f".{target.name}.part")
    count = 0
    minimum: int | None = None
    maximum: int | None = None
    try:
        connection.execute("BEGIN")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]) != "ok":
            raise LegacySnapshotError(
                f"Legacy history quick_check failed: {history_path}",
            )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(conversation_history)",
            ).fetchall()
        }
        if not columns:
            connection.rollback()
            return 0, None, None
        missing = _REQUIRED_HISTORY_COLUMNS - columns
        if missing:
            raise LegacySnapshotError(
                f"Legacy history is missing columns: {sorted(missing)}",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        selected_columns = (
            column if column in columns else f"NULL AS {column}"
            for column in _HISTORY_COLUMNS
        )
        query = (
            f"SELECT {', '.join(selected_columns)} "
            "FROM conversation_history WHERE session_id = ? ORDER BY seq"
        )
        with part.open("w", encoding="utf-8", newline="\n") as stream:
            for row in connection.execute(query, (session_id,)):
                payload: dict[str, Any] = {
                    column: row[column] for column in _HISTORY_COLUMNS
                }
                sequence = int(payload["seq"])
                minimum = (
                    sequence if minimum is None else min(minimum, sequence)
                )
                maximum = (
                    sequence if maximum is None else max(maximum, sequence)
                )
                stream.write(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
                stream.write("\n")
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        connection.commit()
        os.replace(part, target)
        return count, minimum, maximum
    except sqlite3.DatabaseError as exc:
        raise LegacySnapshotError(
            f"Could not read legacy history snapshot: {history_path}",
        ) from exc
    finally:
        connection.close()


def _manifest_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_legacy_snapshot(
    workspace: Path,
    *,
    conversation_id: str,
    session_id: str,
    source_paths: list[str],
) -> LegacySnapshotManifest:
    """Capture immutable migration inputs without mutating legacy stores."""
    paths = ChatStoragePaths(workspace)
    paths.conversation(conversation_id)
    snapshot_dir = paths.root / "migration" / conversation_id / "snapshot"
    sources_dir = snapshot_dir / "sessions"
    sessions_dir = workspace / "sessions"
    captured: list[SnapshotFile] = []
    for index, relative_path in enumerate(sorted(source_paths)):
        source = _safe_source(sessions_dir, relative_path)
        target = sources_dir / f"{index:04d}.json"
        copied = _copy_stable(source, target)
        captured.append(
            SnapshotFile(
                relative_path=relative_path,
                snapshot_path=target.relative_to(snapshot_dir).as_posix(),
                size=copied.size,
                mtime_ns=copied.mtime_ns,
                sha256=copied.sha256,
            ),
        )

    history_target = snapshot_dir / "history_rows.jsonl"
    history_count, minimum, maximum = _snapshot_history_rows(
        workspace / "history.db",
        history_target,
        session_id=session_id,
    )
    history_relative = (
        history_target.relative_to(snapshot_dir).as_posix()
        if history_count > 0
        else None
    )
    if history_count == 0:
        history_target.unlink(missing_ok=True)
    history_sha256 = _sha256(history_target) if history_count > 0 else None
    unsigned: dict[str, Any] = {
        "version": 1,
        "conversation_id": conversation_id,
        "session_id": session_id,
        "files": [asdict(item) for item in captured],
        "history_rows_path": history_relative,
        "history_rows": history_count,
        "history_min_seq": minimum,
        "history_max_seq": maximum,
        "history_sha256": history_sha256,
    }
    manifest_hash = _manifest_hash(unsigned)
    payload = {**unsigned, "manifest_sha256": manifest_hash}
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = snapshot_dir / "manifest.json"
    manifest_part = manifest_path.with_name(".manifest.json.part")
    with manifest_part.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(
            payload,
            stream,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(manifest_part, manifest_path)
    return LegacySnapshotManifest(
        version=1,
        conversation_id=conversation_id,
        session_id=session_id,
        files=tuple(captured),
        history_rows_path=history_relative,
        history_rows=history_count,
        history_min_seq=minimum,
        history_max_seq=maximum,
        history_sha256=history_sha256,
        manifest_sha256=manifest_hash,
    )


def validate_legacy_snapshot(
    workspace: Path,
    manifest: LegacySnapshotManifest,
) -> None:
    """Verify every snapshot artifact and the signed manifest payload."""
    paths = ChatStoragePaths(workspace)
    paths.conversation(manifest.conversation_id)
    root = paths.root / "migration" / manifest.conversation_id / "snapshot"
    for item in manifest.files:
        path = _safe_snapshot_artifact(root, item.snapshot_path)
        if path.stat().st_size != item.size or _sha256(path) != item.sha256:
            raise LegacySnapshotError(
                f"Legacy snapshot file failed validation: {path}",
            )
    if manifest.history_rows_path is not None:
        history_path = _safe_snapshot_artifact(
            root,
            manifest.history_rows_path,
        )
        if (
            manifest.history_sha256 is None
            or _sha256(history_path) != manifest.history_sha256
        ):
            raise LegacySnapshotError(
                f"Legacy history snapshot failed validation: {history_path}",
            )
    unsigned = asdict(manifest)
    unsigned.pop("manifest_sha256")
    unsigned["files"] = [asdict(item) for item in manifest.files]
    if _manifest_hash(unsigned) != manifest.manifest_sha256:
        raise LegacySnapshotError("Legacy snapshot manifest hash is invalid")
