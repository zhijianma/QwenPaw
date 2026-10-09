# -*- coding: utf-8 -*-
"""Tests for durable, content-free compaction evidence."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from qwenpaw.kernel import (
    CompactionRecord,
    CompactionStatus,
    CompactionTrigger,
    InvocationScope,
)
from qwenpaw.kernel.models import utc_now
from qwenpaw.runtime.compactions import (
    CompactionConflictError,
    CompactionSnapshot,
    RuntimeCompactionRecorder,
    lite_compaction_store,
)


def _scope(tmp_path) -> InvocationScope:
    return InvocationScope(
        agent_id="default",
        chat_id="chat-compaction",
        session_id="console:chat-compaction",
        root_agent_id="default",
        root_session_id="console:chat-compaction",
        workspace_dir=str(tmp_path),
        registry_generation=7,
    )


def test_successful_compaction_requires_a_material_change(tmp_path) -> None:
    scope = _scope(tmp_path)
    now = utc_now()

    with pytest.raises(ValidationError, match="must change context"):
        CompactionRecord(
            agent_id=scope.agent_id,
            chat_id="chat-compaction",
            invocation_id=scope.invocation_id,
            registry_generation=scope.registry_generation,
            strategy_id="qwenpaw.context.scroll",
            trigger=CompactionTrigger.AUTOMATIC,
            status=CompactionStatus.SUCCEEDED,
            before_message_count=2,
            after_message_count=2,
            started_at=now,
            completed_at=now + timedelta(milliseconds=1),
        )


@pytest.mark.asyncio
async def test_recorder_ignores_noop_then_persists_material_change(
    tmp_path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_compaction_store(tmp_path)
    recorder = RuntimeCompactionRecorder(
        scope,
        store,
        strategy_id="qwenpaw.context.scroll",
    )
    before = CompactionSnapshot(
        message_ids=("one", "two"),
        message_count=2,
        summary_fingerprint="same",
    )

    noop = await recorder.record_success(
        trigger=CompactionTrigger.AUTOMATIC,
        before=before,
        after=before,
        stats={"evicted": 0, "folded": 0},
        started_at=utc_now(),
    )
    record = await recorder.record_success(
        trigger=CompactionTrigger.MANUAL,
        before=before,
        after=CompactionSnapshot(
            message_ids=("two",),
            message_count=1,
            summary_fingerprint="changed",
        ),
        stats={"evicted": 1, "folded": 2},
        started_at=utc_now(),
    )
    records = await store.list_for_conversation("chat-compaction")

    assert noop is None
    assert record is not None
    assert records == [record]
    assert record.trigger is CompactionTrigger.MANUAL
    assert record.evicted_messages == 1
    assert record.folded_items == 2
    assert record.context_changed is True
    assert "one" not in record.model_dump_json()


@pytest.mark.asyncio
async def test_recorder_persists_identity_only_context_change(
    tmp_path,
) -> None:
    scope = _scope(tmp_path)
    store = lite_compaction_store(tmp_path)
    recorder = RuntimeCompactionRecorder(
        scope,
        store,
        strategy_id="qwenpaw.context.native",
    )
    before = CompactionSnapshot(
        message_ids=("before",),
        message_count=1,
        summary_fingerprint="same",
    )

    record = await recorder.record_success(
        trigger=CompactionTrigger.AUTOMATIC,
        before=before,
        after=CompactionSnapshot(
            message_ids=("after",),
            message_count=1,
            summary_fingerprint="same",
        ),
        stats={},
        started_at=utc_now(),
    )

    assert record is not None
    assert record.context_changed is True
    assert record.before_message_count == record.after_message_count


@pytest.mark.asyncio
async def test_store_rejects_conflicting_identity(tmp_path) -> None:
    scope = _scope(tmp_path)
    store = lite_compaction_store(tmp_path)
    recorder = RuntimeCompactionRecorder(
        scope,
        store,
        strategy_id="qwenpaw.context.scroll",
    )
    snapshot = CompactionSnapshot(
        message_ids=("one",),
        message_count=1,
        summary_fingerprint="safe",
    )
    record = await recorder.record_failure(
        trigger=CompactionTrigger.OVERFLOW_RECOVERY,
        before=snapshot,
        after=snapshot,
        stats={},
        error_code="ContextOverflowUnchanged",
        started_at=utc_now(),
    )
    conflicting = record.model_copy(
        update={"error_code": "DifferentFailure"},
    )

    with pytest.raises(CompactionConflictError):
        await store.append(conflicting)

    [stored] = await store.list_for_conversation("chat-compaction")
    assert stored.error_code == "ContextOverflowUnchanged"
    payload = stored.model_dump_json()
    assert "private" not in payload
    assert not hasattr(stored, "summary")


@pytest.mark.asyncio
async def test_store_replays_legacy_identity(tmp_path) -> None:
    scope = _scope(tmp_path)
    store = lite_compaction_store(tmp_path)
    now = utc_now()
    record = CompactionRecord(
        agent_id="default",
        chat_id="chat-compaction",
        invocation_id=scope.invocation_id,
        registry_generation=scope.registry_generation,
        strategy_id="qwenpaw.context.scroll",
        trigger=CompactionTrigger.MANUAL,
        status=CompactionStatus.SUCCEEDED,
        before_message_count=2,
        after_message_count=1,
        evicted_messages=1,
        context_changed=True,
        started_at=now,
        completed_at=now + timedelta(milliseconds=1),
    )
    legacy = record.model_dump(mode="json")
    legacy["conversation_id"] = legacy.pop("chat_id")
    owner = hashlib.sha256(b"chat-compaction").hexdigest()
    path = (
        tmp_path
        / ".qwenpaw"
        / "lite"
        / "compactions"
        / owner
        / str(scope.invocation_id)
        / f"{record.compaction_id}.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    await store.append(record)
    [restored] = await store.list_for_conversation("chat-compaction")
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert restored == record
    assert restored.chat_id == "chat-compaction"
    assert "conversation_id" in raw
    assert "chat_id" not in raw


def test_snapshot_uses_identity_without_persisting_content() -> None:
    agent = SimpleNamespace(
        state=SimpleNamespace(
            context=[SimpleNamespace(id="message-1")],
            summary="PRIVATE SUMMARY",
        ),
    )
    from qwenpaw.runtime.compactions import capture_compaction_snapshot

    snapshot = capture_compaction_snapshot(agent)

    assert snapshot.message_ids == ("message-1",)
    assert snapshot.message_count == 1
    assert "PRIVATE SUMMARY" not in repr(snapshot)
