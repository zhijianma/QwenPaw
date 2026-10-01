# -*- coding: utf-8 -*-
"""Host-owned Artifact/Evidence publication for governed tool outputs."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import Any
from urllib.parse import unquote
from uuid import UUID

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse
from pydantic import ValidationError

from ..config.context import get_tool_base_dir
from ..kernel import (
    EvidenceRef,
    TOOL_ARTIFACT_OUTPUTS_METADATA_KEY,
    ToolArtifactOutput,
)
from ..tasks.artifacts import lite_artifact_store
from ..tasks.conversation_artifacts import conversation_artifact_receipts
from ..tool_calls._context import ToolCallContext

TOOL_ARTIFACT_OUTPUTS_KEY = TOOL_ARTIFACT_OUTPUTS_METADATA_KEY
TOOL_ARTIFACT_LINKS_KEY = "qwenpaw_artifact_links"
TOOL_ARTIFACT_ERRORS_KEY = "qwenpaw_artifact_errors"
MAX_TOOL_ARTIFACTS = 16
MAX_TOOL_ARTIFACT_BYTES = 50 * 1024 * 1024
_HOST_TOOL_ARTIFACT_OUTPUTS_KEY = "qwenpaw_host_artifact_outputs"

logger = logging.getLogger(__name__)


def _optional_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class _HostToolArtifactOutput:
    """One Host-generated file that never crosses the Plugin SDK."""

    path: Path
    kind: str
    evidence_claim: str
    media_type: str | None
    name: str | None
    metadata: dict[str, Any]


def register_host_tool_artifact(
    context: ToolCallContext,
    *,
    path: Path,
    kind: str,
    evidence_claim: str,
    media_type: str | None = None,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Declare a Host-generated workspace file for immutable capture.

    This is deliberately separate from ``ToolArtifactOutput``. Plugins must
    continue to reference an already-governed input path parameter; only
    Host code executing inside the supervised tool context may declare a
    generated path.
    """
    outputs = context.extra.setdefault(_HOST_TOOL_ARTIFACT_OUTPUTS_KEY, [])
    if not isinstance(outputs, list):
        raise RuntimeError("host artifact declaration state is invalid")
    outputs.append(
        _HostToolArtifactOutput(
            path=Path(path),
            kind=kind,
            evidence_claim=evidence_claim,
            media_type=media_type,
            name=name,
            metadata=dict(metadata or {}),
        ),
    )


def tool_artifact_output(
    *,
    path_parameter: str,
    kind: str,
    evidence_claim: str,
    media_type: str | None = None,
    name: str | None = None,
    path_normalization: str = "native",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one JSON-safe output declaration for ToolChunk metadata."""
    return ToolArtifactOutput(
        path_parameter=path_parameter,
        kind=kind,
        evidence_claim=evidence_claim,
        media_type=media_type,
        name=name,
        path_normalization=path_normalization,
        metadata=metadata or {},
    ).model_dump(mode="json", by_alias=True)


class ConversationToolArtifactPublisher:
    """Convert tool output declarations into Chat-owned immutable links."""

    def __init__(self, request_context: dict[str, Any]) -> None:
        self._workspace_dir = str(request_context.get("workspace_dir") or "")
        self._chat_id = str(
            request_context.get("os_conversation_id") or "",
        )
        self._generation = request_context.get("os_registry_generation")
        self._invocation_id = _optional_uuid(
            request_context.get("os_invocation_id"),
        )
        self._correlation_id = _optional_uuid(
            request_context.get("os_correlation_id"),
        )
        raw_owners = request_context.get("_tool_provider_owners")
        self._owners = dict(raw_owners) if isinstance(raw_owners, dict) else {}

    async def __call__(
        self,
        response: ToolResponse,
        context: ToolCallContext,
    ) -> ToolResponse:
        """Publish valid declarations and replace all reserved metadata."""
        metadata = dict(response.metadata or {})
        raw_outputs = metadata.pop(TOOL_ARTIFACT_OUTPUTS_KEY, None)
        metadata.pop(TOOL_ARTIFACT_LINKS_KEY, None)
        metadata.pop(TOOL_ARTIFACT_ERRORS_KEY, None)
        response.metadata = metadata
        if not self._workspace_dir or not self._chat_id:
            return response

        outputs: list[ToolArtifactOutput] = []
        errors: list[dict[str, str]] = []
        if (
            response.state is ToolResultState.SUCCESS
            and raw_outputs is not None
        ):
            outputs, errors = self._validate_outputs(raw_outputs)
        host_outputs, host_errors = self._take_host_outputs(
            context,
            response.state,
        )
        errors.extend(host_errors)
        if not outputs and not host_outputs and not errors:
            return response

        links: list[dict[str, Any]] = []
        for output in outputs:
            try:
                links.append(
                    await self._publish_one(output, context),
                )
            except (
                FileNotFoundError,
                IsADirectoryError,
                OSError,
                ValueError,
            ) as exc:
                logger.warning(
                    "tool artifact capture failed for %s.%s (%s)",
                    context.tool_name,
                    output.path_parameter,
                    type(exc).__name__,
                )
                errors.append(
                    {
                        "code": "artifact_capture_failed",
                        "path_parameter": output.path_parameter,
                    },
                )
        for output in host_outputs:
            try:
                links.append(
                    await self._publish_host_one(output, context),
                )
            except (
                FileNotFoundError,
                IsADirectoryError,
                OSError,
                ValueError,
            ) as exc:
                logger.warning(
                    "host tool artifact capture failed for %s (%s)",
                    context.tool_name,
                    type(exc).__name__,
                )
                errors.append(
                    {
                        "code": "artifact_capture_failed",
                        "source": "host",
                    },
                )
        if links:
            metadata[TOOL_ARTIFACT_LINKS_KEY] = links
        if errors:
            metadata[TOOL_ARTIFACT_ERRORS_KEY] = errors
            response.content.append(
                TextBlock(
                    type="text",
                    text=(
                        " Artifact capture did not complete for every "
                        "declared output; do not claim an uncaptured file as "
                        "a verified deliverable."
                    ),
                ),
            )
        response.metadata = metadata
        return response

    @staticmethod
    def _validate_outputs(
        raw_outputs: Any,
    ) -> tuple[list[ToolArtifactOutput], list[dict[str, str]]]:
        if not isinstance(raw_outputs, list):
            return [], [{"code": "artifact_declaration_invalid"}]
        outputs: list[ToolArtifactOutput] = []
        errors: list[dict[str, str]] = []
        for raw_output in raw_outputs[:MAX_TOOL_ARTIFACTS]:
            try:
                outputs.append(ToolArtifactOutput.model_validate(raw_output))
            except ValidationError:
                errors.append({"code": "artifact_declaration_invalid"})
        if len(raw_outputs) > MAX_TOOL_ARTIFACTS:
            errors.append({"code": "artifact_declaration_limit_exceeded"})
        return outputs, errors

    @staticmethod
    def _take_host_outputs(
        context: ToolCallContext,
        state: ToolResultState,
    ) -> tuple[list[_HostToolArtifactOutput], list[dict[str, str]]]:
        raw_outputs = context.extra.pop(
            _HOST_TOOL_ARTIFACT_OUTPUTS_KEY,
            [],
        )
        if not raw_outputs:
            return [], []
        if state not in {ToolResultState.SUCCESS, ToolResultState.ERROR}:
            return [], [{"code": "host_artifact_state_invalid"}]
        if not isinstance(raw_outputs, list):
            return [], [{"code": "host_artifact_declaration_invalid"}]
        outputs = [
            output
            for output in raw_outputs[:MAX_TOOL_ARTIFACTS]
            if isinstance(output, _HostToolArtifactOutput)
        ]
        errors: list[dict[str, str]] = []
        if len(outputs) != min(len(raw_outputs), MAX_TOOL_ARTIFACTS):
            errors.append({"code": "host_artifact_declaration_invalid"})
        if len(raw_outputs) > MAX_TOOL_ARTIFACTS:
            errors.append({"code": "artifact_declaration_limit_exceeded"})
        return outputs, errors

    async def _publish_one(
        self,
        output: ToolArtifactOutput,
        context: ToolCallContext,
    ) -> dict[str, Any]:
        tool_input = context.extra.get("tool_input")
        if not isinstance(tool_input, dict):
            raise ValueError("tool input is unavailable")
        raw_path = tool_input.get(output.path_parameter)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("artifact path parameter is unavailable")
        if output.path_normalization == "url":
            raw_path = unicodedata.normalize("NFC", unquote(raw_path))
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = get_tool_base_dir() / path
        return await self._publish_path(
            path,
            context,
            kind=output.kind,
            evidence_claim=output.evidence_claim,
            media_type=output.media_type,
            name=output.name,
            output_metadata=output.metadata,
        )

    async def _publish_host_one(
        self,
        output: _HostToolArtifactOutput,
        context: ToolCallContext,
    ) -> dict[str, Any]:
        workspace = Path(self._workspace_dir).expanduser().resolve()
        path = output.path.expanduser().resolve()
        if not path.is_relative_to(workspace):
            raise ValueError("host-generated artifact is outside workspace")
        return await self._publish_path(
            path,
            context,
            kind=output.kind,
            evidence_claim=output.evidence_claim,
            media_type=output.media_type,
            name=output.name,
            output_metadata=output.metadata,
        )

    async def _publish_path(
        self,
        path: Path,
        context: ToolCallContext,
        *,
        kind: str,
        evidence_claim: str,
        media_type: str | None,
        name: str | None,
        output_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        file_stat = await asyncio.to_thread(path.stat)
        if not S_ISREG(file_stat.st_mode):
            raise IsADirectoryError(str(path))
        if file_stat.st_size > MAX_TOOL_ARTIFACT_BYTES:
            raise ValueError("tool artifact exceeds capture limit")
        content = await asyncio.to_thread(path.read_bytes)
        if len(content) > MAX_TOOL_ARTIFACT_BYTES:
            raise ValueError("tool artifact exceeds capture limit")

        provider_id = str(
            self._owners.get(context.tool_name)
            or "qwenpaw.system.workspace-tools",
        )
        artifact_metadata = {
            **output_metadata,
            "name": name or path.name,
            "source": "chat.tool-output",
            "chat_id": self._chat_id,
            "tool_call_id": context.tool_call_id,
            "tool_name": context.tool_name,
            "provider_id": provider_id,
        }
        if self._generation is not None:
            artifact_metadata["registry_generation"] = self._generation
        if self._invocation_id is not None:
            artifact_metadata["invocation_id"] = str(self._invocation_id)
        media_type = (
            media_type
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        artifact = await lite_artifact_store(
            Path(self._workspace_dir),
        ).put(
            kind=kind,
            media_type=media_type,
            content=content,
            metadata=artifact_metadata,
        )
        evidence = EvidenceRef(
            artifact_id=artifact.artifact_id,
            claim=evidence_claim,
            producer=f"{provider_id}.{context.tool_name}",
            metadata={
                "chat_id": self._chat_id,
                "tool_call_id": context.tool_call_id,
            },
        )
        receipt = await conversation_artifact_receipts(
            Path(self._workspace_dir),
        ).create_owned(
            artifact,
            evidence,
            chat_id=self._chat_id,
            invocation_id=self._invocation_id,
            correlation_id=self._correlation_id,
            registry_generation=(
                self._generation
                if isinstance(self._generation, int)
                and not isinstance(self._generation, bool)
                and self._generation > 0
                else None
            ),
        )
        return {
            "chat_id": self._chat_id,
            "artifact_ref": artifact.model_dump(mode="json"),
            "evidence_ref": evidence.model_dump(mode="json"),
            "artifact_receipt": receipt,
        }


__all__ = [
    "ConversationToolArtifactPublisher",
    "MAX_TOOL_ARTIFACT_BYTES",
    "TOOL_ARTIFACT_ERRORS_KEY",
    "TOOL_ARTIFACT_LINKS_KEY",
    "TOOL_ARTIFACT_OUTPUTS_KEY",
    "register_host_tool_artifact",
    "tool_artifact_output",
]
