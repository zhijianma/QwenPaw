# -*- coding: utf-8 -*-
"""Persist the shared QwenPaw envelope stream into normalized Chat storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, cast

from ...schemas import MessageType, RunStatus
from .storage import (
    ChatStoragePaths,
    PartialBlockMaterializer,
    RuntimeRunHandle,
    append_completed_message,
    begin_partial_text_block,
    complete_partial_message,
    enqueue_projection_repair,
    finalize_run,
    project_conversation,
    reserve_recall_sequences,
)
from .storage.common import connect_database
from .storage.materializer import ContextRevisionKind


logger = logging.getLogger(__name__)


LIVE_MESSAGE_MAX_BYTES = 1024 * 1024
LIVE_MESSAGE_VIEW_MAX_BYTES = 2 * 1024 * 1024
LIVE_MESSAGE_VIEW_MAX_ITEMS = 256
LIVE_PARTIAL_BLOCK_SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class MaterializedLiveMutation:
    """One normalized live mutation emitted after persistence observes it."""

    kind: str
    payload: Mapping[str, Any]
    entity_id: str | None = None
    entity_version: int | None = None
    expected_offset: int | None = None


@dataclass(frozen=True)
class MaterializedLiveResult:
    """Persistence outcome and immutable active overlay for one event."""

    terminal: bool
    mutations: tuple[MaterializedLiveMutation, ...]
    view: Mapping[str, Any]


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


class ChatEventMaterializer:
    """Consume envelope events before the transport exposes them."""

    def __init__(
        self,
        paths: ChatStoragePaths,
        handle: RuntimeRunHandle,
        *,
        lease_owner: str,
        session_id: str | None = None,
        initial_message: Any | None = None,
    ) -> None:
        self._paths = paths
        self._handle = handle
        self._lease_owner = lease_owner
        self._session_id = session_id
        self._message_types: dict[str, MessageType] = {}
        self._partial: dict[tuple[str, int], PartialBlockMaterializer] = {}
        self._streamed_message_ids: set[str] = set()
        self._completed_message_ids: set[str] = set()
        self._live_blocks: dict[str, Mapping[str, Any]] = {}
        self._live_block_snapshots: dict[str, tuple[int, bytearray]] = {}
        self._live_messages: dict[str, Mapping[str, Any]] = {}
        self._live_message_bytes = 0
        self._run_state = "running"
        self._terminal = False
        if initial_message is not None:
            message_id = str(
                getattr(initial_message, "id", "")
                or f"live-user-{handle.turn_id}",
            )
            self._remember_live_message(initial_message, message_id)

    @property
    def conversation_id(self) -> str:
        """Return the source identity used by checkpoint flush barriers."""
        return self._handle.conversation_id

    @property
    def terminal_state(self) -> str | None:
        """Return the committed terminal state, if finalization completed."""
        return self._run_state if self._terminal else None

    async def flush_partial_blocks(self) -> None:
        """Persist every buffered partial without finalizing the run."""
        if not self._terminal:
            await self._flush_all()

    @property
    def live_view(self) -> Mapping[str, Any]:
        """Return a copy-on-write overlay for data absent from history."""
        return {
            "run_state": self._run_state,
            "blocks": tuple(self._live_blocks.values()),
            "messages": tuple(self._live_messages.values()),
        }

    @staticmethod
    def _wire_message(message: Any, message_id: str) -> Mapping[str, Any]:
        value = (
            message.model_dump(mode="json")
            if hasattr(message, "model_dump")
            else {"value": message}
        )
        if not isinstance(value, dict):
            value = {"value": value}
        return {**value, "id": message_id}

    def _remember_live_message(
        self,
        message: Any,
        message_id: str,
    ) -> Mapping[str, Any]:
        value = self._wire_message(message, message_id)
        encoded_bytes = len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        item: Mapping[str, Any] = {
            "message_id": message_id,
            "deferred": encoded_bytes > LIVE_MESSAGE_MAX_BYTES,
            "message": (
                None if encoded_bytes > LIVE_MESSAGE_MAX_BYTES else value
            ),
            "encoded_bytes": min(encoded_bytes, LIVE_MESSAGE_MAX_BYTES),
        }
        previous = self._live_messages.get(message_id)
        if previous is not None:
            self._live_message_bytes -= int(previous["encoded_bytes"])
        self._live_messages = {**self._live_messages, message_id: item}
        self._live_message_bytes += int(item["encoded_bytes"])
        while self._live_messages and (
            len(self._live_messages) > LIVE_MESSAGE_VIEW_MAX_ITEMS
            or self._live_message_bytes > LIVE_MESSAGE_VIEW_MAX_BYTES
        ):
            oldest_id = next(iter(self._live_messages))
            oldest = self._live_messages[oldest_id]
            self._live_message_bytes -= int(oldest["encoded_bytes"])
            self._live_messages = {
                key: value
                for key, value in self._live_messages.items()
                if key != oldest_id
            }
        return item

    def _update_live_block(self, append: Any, *, index: int) -> None:
        snapshot = append.snapshot
        snapshot_start, snapshot_payload = self._live_block_snapshots.get(
            snapshot.handle.block_id,
            (append.expected_offset, bytearray()),
        )
        snapshot_end = snapshot_start + len(snapshot_payload)
        if append.expected_offset != snapshot_end:
            snapshot_start = append.expected_offset
            snapshot_payload = bytearray()
        snapshot_payload.extend(append.payload)
        if len(snapshot_payload) > LIVE_PARTIAL_BLOCK_SNAPSHOT_MAX_BYTES:
            remove_bytes = (
                len(snapshot_payload)
                - LIVE_PARTIAL_BLOCK_SNAPSHOT_MAX_BYTES
            )
            while (
                remove_bytes < len(snapshot_payload)
                and snapshot_payload[remove_bytes] & 0xC0 == 0x80
            ):
                remove_bytes += 1
            del snapshot_payload[:remove_bytes]
            snapshot_start += remove_bytes
        self._live_block_snapshots[snapshot.handle.block_id] = (
            snapshot_start,
            snapshot_payload,
        )
        pending = tuple(
            {
                "start_byte": segment.start_byte,
                "entity_version": segment.entity_version,
                "text": segment.payload.decode("utf-8", errors="replace"),
            }
            for segment in snapshot.pending
        )
        block = {
            "message_id": snapshot.handle.message_id,
            "block_id": snapshot.handle.block_id,
            "index": index,
            "kind": "text",
            "state": "partial",
            "durable_bytes": snapshot.durable_bytes,
            "total_bytes": snapshot.total_bytes,
            "entity_version": snapshot.entity_version,
            "snapshot": {
                "start_byte": snapshot_start,
                "text": snapshot_payload.decode(
                    "utf-8",
                    errors="replace",
                ),
            },
            "pending": pending,
        }
        self._live_blocks = {
            **self._live_blocks,
            snapshot.handle.block_id: block,
        }

    def _now_us(self) -> int:
        return time.time_ns() // 1_000

    async def _recall_sequences(
        self,
        count: int,
        *,
        dedup_key: str | None = None,
    ) -> list[int]:
        existing: list[int] = []
        if dedup_key and self._session_id and self._paths.recall.is_file():
            with connect_database(
                self._paths.recall,
                readonly=True,
            ) as connection:
                row = connection.execute(
                    "SELECT seq FROM conversation_history "
                    "WHERE session_id = ? AND dedup_key = ?",
                    (self._session_id, dedup_key),
                ).fetchone()
            if row is not None:
                existing.append(int(row["seq"]))
        remaining = count - len(existing)
        if remaining <= 0:
            return existing
        lease = await asyncio.to_thread(
            reserve_recall_sequences,
            self._paths.catalog,
            workspace_id=str(self._paths.workspace.resolve()),
            count=remaining,
            lease_owner=self._lease_owner,
            now_us=self._now_us(),
        )
        return existing + (list(lease.values()) if lease is not None else [])

    async def _start_partial(
        self,
        *,
        message_id: str,
        index: int,
    ) -> PartialBlockMaterializer:
        sequence = (await self._recall_sequences(1, dedup_key=message_id))[0]
        message_type = self._message_types.get(
            message_id,
            MessageType.MESSAGE,
        )
        existing_message_id = next(
            (
                materializer.handle.message_id
                for key, materializer in self._partial.items()
                if key[0] == message_id
            ),
            None,
        )
        handle = await asyncio.to_thread(
            begin_partial_text_block,
            self._paths.conversation(self._handle.conversation_id),
            conversation_id=self._handle.conversation_id,
            run_id=self._handle.run_id,
            fencing_token=self._handle.fencing_token,
            recall_seq=sequence,
            message_type=message_type,
            block_kind="text",
            source_message_id=message_id,
            existing_message_id=existing_message_id,
            now_us=self._now_us(),
        )
        materializer = PartialBlockMaterializer(
            self._paths.conversation(self._handle.conversation_id),
            handle,
        )
        self._partial[(message_id, index)] = materializer
        self._streamed_message_ids.add(message_id)
        return materializer

    async def observe(
        self,
        event: Any,
        *,
        runtime_context: bytes | None = None,
        context_kind: ContextRevisionKind = "turn_commit",
    ) -> bool:
        """Persist an event and return whether it committed run terminal."""
        result = await self.observe_live(
            event,
            runtime_context=runtime_context,
            context_kind=context_kind,
        )
        return result.terminal

    async def observe_live(
        self,
        event: Any,
        *,
        runtime_context: bytes | None = None,
        context_kind: ContextRevisionKind = "turn_commit",
    ) -> MaterializedLiveResult:
        """Persist an event and expose its normalized live mutation."""
        if self._terminal:
            return MaterializedLiveResult(False, (), self.live_view)
        obj = str(getattr(event, "object", "") or "")
        status = _enum_value(getattr(event, "status", None))
        if obj == "message":
            mutations = await self._observe_message(event, status)
            return MaterializedLiveResult(
                False,
                mutations,
                self.live_view,
            )
        if obj == "content":
            mutation = await self._observe_content(event)
            mutations = (mutation,) if mutation is not None else ()
            return MaterializedLiveResult(
                False,
                mutations,
                self.live_view,
            )
        if obj != "response" or status not in {
            RunStatus.Completed.value,
            RunStatus.Failed.value,
            RunStatus.Cancelled.value,
        }:
            return MaterializedLiveResult(False, (), self.live_view)
        terminal_state = cast(
            Literal["completed", "failed", "canceled"],
            {
                RunStatus.Completed.value: "completed",
                RunStatus.Failed.value: "failed",
                RunStatus.Cancelled.value: "canceled",
            }[status],
        )
        await self.finalize(
            state=terminal_state,
            runtime_context=runtime_context,
            context_kind=context_kind,
            response=event,
        )
        mutation = MaterializedLiveMutation(
            kind="terminal",
            payload={"state": terminal_state},
            entity_id=self._handle.run_id,
        )
        return MaterializedLiveResult(True, (mutation,), self.live_view)

    async def _observe_content(
        self,
        event: Any,
    ) -> MaterializedLiveMutation | None:
        if not bool(getattr(event, "delta", False)):
            return None
        text = getattr(event, "text", None)
        message_id = str(getattr(event, "msg_id", "") or "")
        if not isinstance(text, str) or not text or not message_id:
            return None
        index = int(getattr(event, "index", 0) or 0)
        key = (message_id, index)
        materializer = self._partial.get(key)
        if materializer is None:
            materializer = await self._start_partial(
                message_id=message_id,
                index=index,
            )
        append = await materializer.append(text, now_us=self._now_us())
        self._update_live_block(append, index=index)
        live_text = append.payload.decode("utf-8", errors="replace")
        return MaterializedLiveMutation(
            kind="block_append",
            payload={
                "text": live_text,
                "index": index,
                "message_id": materializer.handle.message_id,
            },
            entity_id=materializer.handle.block_id,
            entity_version=append.entity_version,
            expected_offset=append.expected_offset,
        )

    async def _observe_message(
        self,
        event: Any,
        status: str,
    ) -> tuple[MaterializedLiveMutation, ...]:
        message_id = str(
            getattr(event, "id", "") or getattr(event, "msg_id", "") or "",
        )
        raw_type = getattr(event, "type", MessageType.MESSAGE)
        try:
            message_type = MessageType(_enum_value(raw_type))
        except ValueError:
            message_type = MessageType.MESSAGE
        if message_id:
            self._message_types[message_id] = message_type
        if status not in {
            RunStatus.Completed.value,
            RunStatus.Cancelled.value,
        }:
            return ()
        if message_id in self._completed_message_ids:
            return ()
        if message_id in self._streamed_message_ids:
            await self._flush_message(message_id)
            streamed_indexes = {
                key[1] for key in self._partial if key[0] == message_id
            }
            extra_content = [
                block
                for position, block in enumerate(
                    list(getattr(event, "content", None) or []),
                )
                if not (
                    _enum_value(getattr(block, "type", None)) == "text"
                    and int(getattr(block, "index", position) or 0)
                    in streamed_indexes
                )
            ]
            if extra_content:
                sequences = await self._recall_sequences(
                    len(extra_content),
                )
            else:
                sequences = []
            materializer = next(
                value
                for key, value in self._partial.items()
                if key[0] == message_id
            )
            await asyncio.to_thread(
                complete_partial_message,
                self._paths.conversation(self._handle.conversation_id),
                materializer.handle,
                event,
                extra_content=extra_content,
                recall_sequences=sequences,
                now_us=self._now_us(),
            )
            self._completed_message_ids.add(message_id)
            completed_block_ids = {
                value.handle.block_id
                for key, value in self._partial.items()
                if key[0] == message_id
            }
            self._live_blocks = {
                block_id: block
                for block_id, block in self._live_blocks.items()
                if block_id not in completed_block_ids
            }
            self._live_block_snapshots = {
                block_id: block
                for block_id, block in self._live_block_snapshots.items()
                if block_id not in completed_block_ids
            }
            live_message = self._remember_live_message(
                event,
                materializer.handle.message_id,
            )
            return (
                MaterializedLiveMutation(
                    kind="entity_state",
                    payload={
                        "entity": "message",
                        "state": "completed",
                        "message_type": message_type.value,
                        "live_message": live_message,
                    },
                    entity_id=materializer.handle.message_id,
                ),
            )
        content = list(getattr(event, "content", None) or [])
        if not content:
            self._completed_message_ids.add(message_id)
            return ()
        data = getattr(content[0], "data", None) if content else None
        call_id = data.get("call_id") if isinstance(data, dict) else None
        dedup_key = (
            str(call_id)
            if message_type
            in {
                MessageType.PLUGIN_CALL_OUTPUT,
                MessageType.FUNCTION_CALL_OUTPUT,
                MessageType.MCP_TOOL_CALL_OUTPUT,
            }
            and call_id
            else message_id
        )
        sequences = await self._recall_sequences(
            len(content),
            dedup_key=dedup_key,
        )
        appended = await asyncio.to_thread(
            append_completed_message,
            self._paths.conversation(self._handle.conversation_id),
            self._handle,
            event,
            recall_sequences=sequences,
            now_us=self._now_us(),
        )
        self._completed_message_ids.add(message_id)
        live_message = self._remember_live_message(
            event,
            appended.message_id,
        )
        return (
            MaterializedLiveMutation(
                kind="entity_state",
                payload={
                    "entity": "message",
                    "state": "completed",
                    "message_type": message_type.value,
                    "block_ids": appended.block_ids,
                    "live_message": live_message,
                },
                entity_id=appended.message_id,
            ),
        )

    async def _flush_message(self, message_id: str) -> None:
        for key, materializer in tuple(self._partial.items()):
            if key[0] != message_id:
                continue
            await materializer.close(now_us=self._now_us())

    async def _flush_all(self) -> None:
        for materializer in tuple(self._partial.values()):
            await materializer.close(now_us=self._now_us())

    async def finalize(
        self,
        *,
        state: Literal["completed", "failed", "canceled"],
        runtime_context: bytes | None,
        context_kind: ContextRevisionKind = "turn_commit",
        response: Any | None = None,
    ) -> None:
        """Flush all partials and atomically finalize the accepted run."""
        if self._terminal:
            return
        await self._flush_all()
        usage = getattr(response, "usage", None) if response else None
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(mode="json")
        if not isinstance(usage, dict):
            usage = None
        error = getattr(response, "error", None) if response else None
        error_digest = None
        if error is not None:
            error_digest = hashlib.sha256(
                str(error).encode("utf-8"),
            ).hexdigest()
        commit_task = asyncio.create_task(
            asyncio.to_thread(
                finalize_run,
                self._paths.conversation(self._handle.conversation_id),
                self._handle,
                state=state,
                runtime_context=runtime_context,
                context_kind=context_kind,
                usage=usage,
                error_digest=error_digest,
                now_us=self._now_us(),
            ),
        )
        cancellation: asyncio.CancelledError | None = None
        try:
            generation = await asyncio.shield(commit_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
            generation = await asyncio.shield(commit_task)
        self._run_state = state
        self._live_blocks = {}
        self._live_block_snapshots = {}
        self._terminal = True
        try:
            await asyncio.to_thread(
                project_conversation,
                self._paths,
                self._handle.conversation_id,
                now_us=self._now_us(),
            )
        except Exception:
            try:
                await asyncio.to_thread(
                    enqueue_projection_repair,
                    self._paths,
                    self._handle.conversation_id,
                    expected_generation=generation,
                    now_us=self._now_us(),
                )
            except Exception:
                logger.exception(
                    "Chat projection repair could not be queued after "
                    "terminal commit",
                )
            logger.warning(
                "Chat projection deferred after terminal commit",
                exc_info=True,
            )
        if cancellation is not None:
            raise cancellation

    async def abort(
        self,
        error: BaseException,
        *,
        runtime_context: bytes | None = None,
        context_kind: ContextRevisionKind = "turn_commit",
    ) -> None:
        """Persist a failed or canceled terminal when iteration aborts."""
        state: Literal["failed", "canceled"] = (
            "canceled"
            if isinstance(error, asyncio.CancelledError)
            else "failed"
        )
        await self.finalize(
            state=state,
            runtime_context=runtime_context,
            context_kind=context_kind,
            response=None,
        )
