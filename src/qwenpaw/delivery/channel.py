# -*- coding: utf-8 -*-
"""System delivery adapter for existing QwenPaw channels."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..kernel import (
    ArtifactRef,
    DeliveryKind,
    DeliveryMode,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
)
from ..schemas import (
    AudioContent,
    DataContent,
    FileContent,
    FunctionCall,
    FunctionCallOutput,
    ImageContent,
    Message,
    MessageType,
    RefusalContent,
    Role,
    TextContent,
    VideoContent,
)
from ..tasks.artifacts import ArtifactIntegrityError, lite_artifact_store
from ..tasks.ledger import SQLiteExecutionLedger
from ..tasks.service import TaskService

SYSTEM_CHANNEL_DELIVERY_ID = "qwenpaw.system.tasks.channel-delivery"
_ADDRESS_VERSION = 1

WorkspaceResolver = Callable[[str], Awaitable[Any]]


def encode_channel_address(
    *,
    channel: str,
    user_id: str,
    transport_context: str,
) -> str:
    """Encode transport coordinates behind one adapter-owned address."""
    values = (channel.strip(), user_id.strip(), transport_context.strip())
    if not all(values):
        raise ValueError("channel address components must not be empty")
    payload = json.dumps(
        [_ADDRESS_VERSION, *values],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_channel_address(address: str) -> tuple[str, str, str]:
    padding = "=" * (-len(address) % 4)
    try:
        payload = base64.urlsafe_b64decode(address + padding)
        values = json.loads(payload.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError) as error:
        raise ValueError("invalid channel delivery address") from error
    if (
        not isinstance(values, list)
        or len(values) != 4
        or values[0] != _ADDRESS_VERSION
        or not all(isinstance(item, str) and item for item in values[1:])
    ):
        raise ValueError("invalid channel delivery address")
    return values[1], values[2], values[3]


class SystemChannelDeliveryAdapter:
    """Project committed public Task events through ChannelManager."""

    adapter_id = SYSTEM_CHANNEL_DELIVERY_ID

    def __init__(self, resolve_workspace: WorkspaceResolver) -> None:
        self._resolve_workspace = resolve_workspace

    def supports(self, request: DeliveryRequest) -> bool:
        """Accept text projections explicitly addressed to this adapter."""
        matches = (
            request.destination.adapter_id == self.adapter_id
            and request.mode in {DeliveryMode.FINAL, DeliveryMode.STREAM}
            and isinstance(request.payload.get("text"), str)
        )
        if not matches:
            return False
        try:
            _decode_channel_address(request.destination.address)
        except ValueError:
            return False
        return True

    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        """Send one completed message and return an ownership-bound receipt."""
        channel, user_id, transport_context = _decode_channel_address(
            request.destination.address,
        )
        workspace = await self._resolve_workspace(request.agent_id)
        channel_manager = workspace.channel_manager
        configured = await channel_manager.get_channel(channel)
        if configured is None:
            return self._receipt(
                request,
                attempt,
                DeliveryStatus.FAILED,
                error_code="channel_not_found",
            )
        text = request.payload.get("text")
        source = request.payload.get("source")
        has_content = (
            isinstance(source, dict)
            and isinstance(source.get("content"), list)
            and bool(source["content"])
        )
        if not isinstance(text, str) or (
            not text and not has_content and not request.artifact_refs
        ):
            return self._receipt(
                request,
                attempt,
                DeliveryStatus.FAILED,
                error_code="delivery_text_missing",
            )
        try:
            message = await self._message(
                request,
                attempt,
                text,
                workspace,
            )
        except (FileNotFoundError, ArtifactIntegrityError, ValueError):
            return self._receipt(
                request,
                attempt,
                DeliveryStatus.FAILED,
                error_code="artifact_unavailable",
            )
        raw_channel_meta = request.destination.metadata.get("channel_meta")
        channel_meta = (
            dict(raw_channel_meta)
            if isinstance(raw_channel_meta, dict)
            else {}
        )
        channel_meta["source"] = "delivery_projection"
        await channel_manager.send_event(
            channel=channel,
            user_id=user_id,
            session_id=transport_context,
            event=message,
            meta=channel_meta,
        )
        return self._receipt(
            request,
            attempt,
            DeliveryStatus.DELIVERED,
        )

    async def _message(
        self,
        request: DeliveryRequest,
        attempt: int,
        text: str,
        workspace: Any,
    ) -> Message:
        metadata = {
            "delivery_id": str(request.delivery_id),
            "delivery_attempt": attempt,
            "delivery_kind": request.kind.value,
        }
        if request.invocation_id is not None:
            metadata["invocation_id"] = str(request.invocation_id)
        if request.correlation_id is not None:
            metadata["correlation_id"] = str(request.correlation_id)
        if request.kind is not DeliveryKind.ACTIVITY:
            content = await self._public_content(
                request,
                text,
                workspace,
            )
            return Message(
                object="message",
                role=Role.ASSISTANT,
                content=content,
                metadata=metadata,
                artifact_refs=list(request.artifact_refs),
                evidence_refs=list(request.evidence_refs),
            ).completed()
        source = request.payload.get("source")
        event_type = request.payload.get("event_type")
        if not isinstance(source, dict):
            source = {}
        call_id = str(source.get("call_id") or "unknown")
        name = str(source.get("name") or text or "tool")
        if event_type == "tool.completed":
            output = source.get("output", source.get("text", ""))
            content = DataContent(
                data=FunctionCallOutput(
                    call_id=call_id,
                    name=name,
                    output=str(output or ""),
                ).model_dump(),
            ).completed()
            return Message(
                object="message",
                type=MessageType.FUNCTION_CALL_OUTPUT,
                role=Role.TOOL,
                content=[content],
                metadata=metadata,
            ).completed()
        arguments = source.get("arguments", "")
        content = DataContent(
            data=FunctionCall(
                call_id=call_id,
                name=name,
                arguments=str(arguments or ""),
            ).model_dump(),
        ).completed()
        return Message(
            object="message",
            type=MessageType.FUNCTION_CALL,
            role=Role.ASSISTANT,
            content=[content],
            metadata=metadata,
        ).completed()

    async def _public_content(  # pylint: disable=too-many-branches
        self,
        request: DeliveryRequest,
        text: str,
        workspace: Any,
    ) -> list[Any]:
        """Materialize ordered public parts without storing bytes in Ledger."""
        source = request.payload.get("source")
        raw_parts = (
            source.get("content")
            if isinstance(source, dict)
            and isinstance(source.get("content"), list)
            else []
        )
        artifacts = {
            str(artifact.artifact_id): artifact
            for artifact in request.artifact_refs
        }
        artifact_urls = await self._artifact_data_urls(
            workspace,
            request,
        )
        used_artifacts: set[str] = set()
        content: list[Any] = []
        for raw_part in raw_parts:
            if not isinstance(raw_part, dict):
                continue
            part_type = raw_part.get("type")
            artifact_id = str(raw_part.get("artifact_id") or "")
            artifact = artifacts.get(artifact_id)
            value = None
            if artifact is not None:
                used_artifacts.add(artifact_id)
                value = artifact_urls[artifact_id]
            if part_type == "text" and raw_part.get("text"):
                content.append(
                    TextContent(text=str(raw_part["text"])).completed(),
                )
            elif part_type == "refusal" and raw_part.get("refusal"):
                content.append(
                    RefusalContent(
                        refusal=str(raw_part["refusal"]),
                    ).completed(),
                )
            elif part_type == "image":
                value = value or raw_part.get("image_url")
                if isinstance(value, str) and value:
                    content.append(
                        ImageContent(image_url=value).completed(),
                    )
            elif part_type == "video":
                value = value or raw_part.get("video_url")
                if isinstance(value, str) and value:
                    content.append(
                        VideoContent(video_url=value).completed(),
                    )
            elif part_type == "audio":
                value = value or raw_part.get("data")
                if isinstance(value, str) and value:
                    content.append(
                        AudioContent(
                            data=value,
                            format=str(raw_part.get("format") or "") or None,
                        ).completed(),
                    )
            elif part_type == "file":
                value = value or raw_part.get("file_url")
                if isinstance(value, str) and value:
                    content.append(
                        FileContent(
                            file_url=value,
                            filename=(
                                str(raw_part.get("filename") or "") or None
                            ),
                        ).completed(),
                    )
        for artifact_id, artifact in artifacts.items():
            if artifact_id in used_artifacts:
                continue
            content.append(
                self._artifact_content(
                    artifact,
                    artifact_urls[artifact_id],
                ),
            )
        if not content and text:
            content.append(TextContent(text=text).completed())
        return content

    @staticmethod
    def _artifact_content(
        artifact: ArtifactRef,
        data_url: str,
    ) -> Any:
        media_type = artifact.media_type.lower()
        filename = str(artifact.metadata.get("name") or "") or None
        if media_type.startswith("image/"):
            return ImageContent(image_url=data_url).completed()
        if media_type.startswith("video/"):
            return VideoContent(video_url=data_url).completed()
        if media_type.startswith("audio/"):
            return AudioContent(
                data=data_url,
                format=media_type.partition("/")[2] or None,
            ).completed()
        return FileContent(
            file_url=data_url,
            filename=filename,
        ).completed()

    @staticmethod
    async def _artifact_data_urls(  # pylint: disable=too-many-branches
        workspace: Any,
        request: DeliveryRequest,
    ) -> dict[str, str]:
        if not request.artifact_refs:
            return {}
        if request.task_id is None:
            raise ValueError("artifact delivery requires task ownership")
        service = getattr(workspace, "_lite_task_service", None)
        if not isinstance(service, TaskService):
            service = TaskService(
                store=SQLiteExecutionLedger(
                    Path(workspace.workspace_dir)
                    / ".qwenpaw"
                    / "lite"
                    / "tasks.db",
                ),
                registry_generation=request.registry_generation,
            )
        task = await service.get_task(request.task_id)
        if task is None or task.agent_id != request.agent_id:
            raise FileNotFoundError(str(request.task_id))
        required_ids = {
            artifact.artifact_id for artifact in request.artifact_refs
        }
        owned_ids = set()
        after_sequence = 0
        while required_ids - owned_ids:
            events = await service.list_events(
                request.task_id,
                after_sequence=after_sequence,
                limit=200,
            )
            if not events:
                break
            for event in events:
                owned_ids.update(
                    artifact.artifact_id
                    for artifact in event.artifact_refs
                    if artifact.artifact_id in required_ids
                )
            if len(events) < 200:
                break
            after_sequence = events[-1].sequence
        missing_ids = required_ids - owned_ids
        if missing_ids:
            raise FileNotFoundError(str(next(iter(missing_ids))))
        raw_project_dir = task.metadata.get("workspace_dir")
        project_dirs = [
            (
                Path(raw_project_dir)
                if isinstance(raw_project_dir, str) and raw_project_dir
                else None
            ),
            Path(workspace.workspace_dir),
        ]
        urls = {}
        for artifact in request.artifact_refs:
            content = None
            for project_dir in dict.fromkeys(project_dirs):
                if project_dir is None:
                    continue
                try:
                    content = await lite_artifact_store(project_dir).read(
                        artifact,
                    )
                    break
                except FileNotFoundError:
                    continue
            if content is None:
                raise FileNotFoundError(str(artifact.artifact_id))
            encoded = base64.b64encode(content).decode("ascii")
            urls[
                str(artifact.artifact_id)
            ] = f"data:{artifact.media_type};base64,{encoded}"
        return urls

    def _receipt(
        self,
        request: DeliveryRequest,
        attempt: int,
        status: DeliveryStatus,
        *,
        error_code: str = "",
    ) -> DeliveryReceipt:
        delivery_id = request.delivery_id
        if delivery_id is None:  # pragma: no cover - Kernel validator
            raise ValueError("delivery request has no stable identity")
        return DeliveryReceipt(
            delivery_id=delivery_id,
            adapter_id=self.adapter_id,
            status=status,
            attempt=attempt,
            error_code=error_code,
        )


__all__ = [
    "SYSTEM_CHANNEL_DELIVERY_ID",
    "SystemChannelDeliveryAdapter",
    "encode_channel_address",
]
