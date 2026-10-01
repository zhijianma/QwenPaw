# -*- coding: utf-8 -*-
"""Content-free, durable evidence for material context compactions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..kernel import (
    CompactionRecord,
    CompactionStatus,
    CompactionStore,
    CompactionTrigger,
    InvocationScope,
)
from ..kernel.models import utc_now
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)


class CompactionConflictError(RuntimeError):
    """Raised when one compaction identity has conflicting evidence."""


class CompactionPersistenceError(RuntimeError):
    """Raised when material compaction evidence cannot be persisted."""


@dataclass(frozen=True)
class CompactionSnapshot:
    """Ephemeral state used only to determine whether context changed."""

    message_ids: tuple[str, ...]
    message_count: int
    summary_fingerprint: str


def capture_compaction_snapshot(agent: Any) -> CompactionSnapshot:
    """Capture content-free comparison state from one live Agent."""
    state = getattr(agent, "state", None)
    context = tuple(getattr(state, "context", None) or ())
    message_ids = tuple(
        str(getattr(message, "id", f"index:{index}"))
        for index, message in enumerate(context)
    )
    summary = repr(getattr(state, "summary", None)).encode("utf-8")
    return CompactionSnapshot(
        message_ids=message_ids,
        message_count=len(context),
        summary_fingerprint=hashlib.sha256(summary).hexdigest(),
    )


def _stat(stats: Mapping[str, Any] | None, key: str) -> int:
    value = (stats or {}).get(key, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class FilesystemCompactionStore(CompactionStore):
    """Owner-only Lite store for immutable compaction evidence."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = Path(workspace_dir) / ".qwenpaw" / "lite" / "compactions"

    @staticmethod
    def _owner_key(conversation_id: str) -> str:
        return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()

    def _record_path(self, record: CompactionRecord) -> Path:
        return (
            self._root
            / self._owner_key(record.conversation_id)
            / str(record.invocation_id)
            / f"{record.compaction_id}.json"
        )

    async def append(self, record: CompactionRecord) -> None:
        """Persist one immutable terminal record."""
        path = self._record_path(record)
        async with get_path_lock(path):
            try:
                existing = CompactionRecord.model_validate(
                    await read_json_async(path),
                )
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing != record:
                    raise CompactionConflictError(
                        "compaction already has different evidence",
                    )
                return
            try:
                await write_json_atomic_async(
                    path,
                    record.model_dump(mode="json"),
                    sort_keys=True,
                )
            except OSError as exc:
                raise CompactionPersistenceError(
                    "failed to persist compaction evidence",
                ) from exc

    def _list_sync(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[CompactionRecord]:
        root = self._root / self._owner_key(conversation_id)
        records = [
            CompactionRecord.model_validate_json(
                path.read_text(encoding="utf-8"),
            )
            for path in root.glob("*/*.json")
        ]
        records.sort(
            key=lambda item: (item.completed_at, str(item.compaction_id)),
            reverse=True,
        )
        return records[:limit]

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[CompactionRecord]:
        """Return newest material compactions for one ChatSpec."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await run_sync_io(self._list_sync, conversation_id, limit)


class RuntimeCompactionRecorder:
    """Bind content-free compaction evidence to one pinned invocation."""

    def __init__(
        self,
        scope: InvocationScope,
        store: CompactionStore,
        *,
        strategy_id: str,
    ) -> None:
        if scope.conversation_id is None:
            raise ValueError("compaction recording requires conversation_id")
        self._scope = scope
        self._store = store
        self._strategy_id = strategy_id

    async def record_success(
        self,
        *,
        trigger: CompactionTrigger,
        before: CompactionSnapshot,
        after: CompactionSnapshot,
        stats: Mapping[str, Any] | None,
        started_at,
    ) -> CompactionRecord | None:
        """Persist a successful material change, ignoring true no-ops."""
        evicted = _stat(stats, "evicted")
        folded = _stat(stats, "folded")
        summary_changed = (
            before.summary_fingerprint != after.summary_fingerprint
        )
        context_changed = before.message_ids != after.message_ids
        if (
            not context_changed
            and not evicted
            and not folded
            and not summary_changed
        ):
            return None
        record = self._record(
            trigger=trigger,
            status=CompactionStatus.SUCCEEDED,
            before=before,
            after=after,
            evicted=evicted,
            folded=folded,
            context_changed=context_changed,
            summary_changed=summary_changed,
            error_code=None,
            started_at=started_at,
        )
        await self._store.append(record)
        return record

    async def record_failure(
        self,
        *,
        trigger: CompactionTrigger,
        before: CompactionSnapshot,
        after: CompactionSnapshot,
        stats: Mapping[str, Any] | None,
        error_code: str,
        started_at,
    ) -> CompactionRecord:
        """Persist a failed attempt without storing exception text."""
        record = self._record(
            trigger=trigger,
            status=CompactionStatus.FAILED,
            before=before,
            after=after,
            evicted=_stat(stats, "evicted"),
            folded=_stat(stats, "folded"),
            context_changed=before.message_ids != after.message_ids,
            summary_changed=(
                before.summary_fingerprint != after.summary_fingerprint
            ),
            error_code=error_code,
            started_at=started_at,
        )
        await self._store.append(record)
        return record

    def _record(
        self,
        *,
        trigger: CompactionTrigger,
        status: CompactionStatus,
        before: CompactionSnapshot,
        after: CompactionSnapshot,
        evicted: int,
        folded: int,
        context_changed: bool,
        summary_changed: bool,
        error_code: str | None,
        started_at,
    ) -> CompactionRecord:
        scope = self._scope
        assert scope.conversation_id is not None
        return CompactionRecord(
            agent_id=scope.agent_id,
            conversation_id=scope.conversation_id,
            invocation_id=scope.invocation_id,
            correlation_id=scope.correlation_id,
            registry_generation=scope.registry_generation,
            strategy_id=self._strategy_id,
            trigger=trigger,
            status=status,
            before_message_count=before.message_count,
            after_message_count=after.message_count,
            evicted_messages=evicted,
            folded_items=folded,
            context_changed=context_changed,
            summary_changed=summary_changed,
            error_code=error_code,
            started_at=started_at,
            completed_at=utc_now(),
        )


def lite_compaction_store(workspace_dir: Path) -> FilesystemCompactionStore:
    """Return the Lite compaction store for one workspace."""
    return FilesystemCompactionStore(workspace_dir)


__all__ = [
    "CompactionConflictError",
    "CompactionPersistenceError",
    "CompactionSnapshot",
    "FilesystemCompactionStore",
    "RuntimeCompactionRecorder",
    "capture_compaction_snapshot",
    "lite_compaction_store",
]
