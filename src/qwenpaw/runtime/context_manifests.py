# -*- coding: utf-8 -*-
"""Privacy-safe manifests for the exact input of each model call."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid5

from agentscope.message import Msg

from ..constant import QWENPAW_INPUT_TRUST_KEY, TRUNCATION_NOTICE_MARKER
from ..kernel import (
    ContextFragment,
    ContextFragmentKind,
    ContextManifest,
    ContextPolicy,
    ContextTrustLevel,
    InvocationScope,
)
from ..utils.io_utils import (
    get_path_lock,
    read_json_async,
    run_sync_io,
    write_json_atomic_async,
)

_RUNTIME_ONLY_FIELDS = {
    "created_at",
    "finished_at",
}


class ContextManifestConflictError(RuntimeError):
    """Raised when one model-call index already has different evidence."""


class ContextManifestLimitError(RuntimeError):
    """Raised when an input cannot be represented without silent omission."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_fingerprint(value: Any, divisor: float) -> tuple[str, int, int]:
    encoded = _canonical_json(value).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    estimate = int(len(encoded) / divisor + 0.5) if encoded else 0
    return f"sha256:{digest}", len(encoded), estimate


def _normalize_message_block(value: Any) -> Any:
    """Remove only AgentScope fields excluded from provider formatting."""
    if not isinstance(value, dict):
        return value
    block_type = value.get("type")
    excluded = set(_RUNTIME_ONLY_FIELDS)
    if block_type not in {"tool_call", "tool_result"}:
        excluded.add("id")
    return {key: item for key, item in value.items() if key not in excluded}


def _contains_text(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, dict):
        return any(_contains_text(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_contains_text(item, marker) for item in value)
    return False


def _message_source(
    role: str,
    block_type: str,
    metadata: dict[str, Any],
) -> tuple[str, ContextTrustLevel]:
    if role == "system":
        return "qwenpaw.context.system-prompt", ContextTrustLevel.SYSTEM
    if block_type == "tool_result":
        return "qwenpaw.context.tool-result", ContextTrustLevel.TOOL
    if role == "user" and metadata.get(QWENPAW_INPUT_TRUST_KEY) == "external":
        return "qwenpaw.context.external-message", ContextTrustLevel.EXTERNAL
    if role == "user":
        return "qwenpaw.context.user-message", ContextTrustLevel.USER
    if role == "assistant":
        return "qwenpaw.context.model-message", ContextTrustLevel.MODEL
    return "qwenpaw.context.external-message", ContextTrustLevel.EXTERNAL


class ContextManifestCompiler:
    """Compile content-free evidence from final AgentScope model inputs."""

    def __init__(
        self,
        scope: InvocationScope,
        *,
        policy: ContextPolicy | None = None,
        tool_owners: dict[str, str] | None = None,
    ) -> None:
        self._scope = scope
        self._policy = policy or ContextPolicy()
        self._tool_owners = dict(tool_owners or {})

    def _message_fragments(
        self,
        messages: Sequence[Msg],
        *,
        model_call_index: int,
        start: int,
    ) -> list[ContextFragment]:
        fragments: list[ContextFragment] = []
        ordinal = start
        for message in messages:
            role = str(getattr(message, "role", "") or "unknown")
            name = str(getattr(message, "name", "") or role)
            content = getattr(message, "content", None)
            raw_metadata = getattr(message, "metadata", None)
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            blocks = content if isinstance(content, list) else [content]
            for raw_block in blocks:
                block = (
                    raw_block.model_dump(mode="json")
                    if hasattr(raw_block, "model_dump")
                    else raw_block
                )
                block_type = (
                    str(block.get("type") or "unknown")
                    if isinstance(block, dict)
                    else "text"
                )
                fragment_id = (
                    f"qwenpaw.context.input.{model_call_index}.{ordinal}"
                )
                ordinal += 1
                if block_type == "thinking":
                    fragments.append(
                        ContextFragment(
                            fragment_id=fragment_id,
                            kind=ContextFragmentKind.REDACTED,
                            source="qwenpaw.context.hidden-reasoning",
                            source_version="agentscope-message.v1",
                            trust_level=ContextTrustLevel.MODEL,
                            selection_reason=(
                                "Present in runtime context but excluded "
                                "from durable audit content"
                            ),
                            transformations=(
                                "qwenpaw.context.hidden-reasoning-redacted",
                            ),
                            redaction_reason=(
                                "qwenpaw.context.hidden-reasoning"
                            ),
                            metadata={
                                "role": role,
                                "block_type": block_type,
                            },
                        ),
                    )
                    continue
                payload = {
                    "role": role,
                    "name": name,
                    "block": _normalize_message_block(block),
                }
                digest, size_bytes, tokens = _content_fingerprint(
                    payload,
                    self._policy.token_estimate_divisor,
                )
                source, trust = _message_source(role, block_type, metadata)
                transformations = ["qwenpaw.context.runtime-normalized"]
                if _contains_text(payload, TRUNCATION_NOTICE_MARKER):
                    transformations.append(
                        "qwenpaw.context.tool-result-pruned",
                    )
                fragments.append(
                    ContextFragment(
                        fragment_id=fragment_id,
                        kind=ContextFragmentKind.MESSAGE,
                        source=source,
                        source_version="agentscope-message.v1",
                        trust_level=trust,
                        selection_reason="Included in final model messages",
                        transformations=tuple(transformations),
                        content_hash=digest,
                        size_bytes=size_bytes,
                        estimated_tokens=tokens,
                        metadata={
                            "role": role,
                            "block_type": block_type,
                        },
                    ),
                )
        return fragments

    def _tool_fragments(
        self,
        tools: Sequence[dict[str, Any]],
        *,
        model_call_index: int,
        start: int,
    ) -> list[ContextFragment]:
        if not self._policy.record_tool_schemas:
            return []
        fragments: list[ContextFragment] = []
        for offset, tool in enumerate(tools):
            function = tool.get("function") if isinstance(tool, dict) else None
            name = (
                str(function.get("name") or "unknown")
                if isinstance(function, dict)
                else "unknown"
            )
            payload = tool
            digest, size_bytes, tokens = _content_fingerprint(
                payload,
                self._policy.token_estimate_divisor,
            )
            owner = self._tool_owners.get(
                name,
                "qwenpaw.system.workspace-tools",
            )
            fragments.append(
                ContextFragment(
                    fragment_id=(
                        f"qwenpaw.context.input.{model_call_index}."
                        f"{start + offset}"
                    ),
                    kind=ContextFragmentKind.TOOL_SCHEMA,
                    source=owner,
                    source_version=(
                        f"registry-epoch-{self._scope.registry_epoch_id}-"
                        f"generation-{self._scope.registry_generation}"
                        if self._scope.registry_epoch_id is not None
                        else f"registry-generation-"
                        f"{self._scope.registry_generation}"
                    ),
                    trust_level=ContextTrustLevel.PROVIDER,
                    selection_reason="Capability disclosed to the model",
                    transformations=(
                        "qwenpaw.context.tool-schema-normalized",
                    ),
                    content_hash=digest,
                    size_bytes=size_bytes,
                    estimated_tokens=tokens,
                    metadata={"tool_name": name},
                ),
            )
        return fragments

    def compile(
        self,
        *,
        messages: Sequence[Msg],
        tools: Sequence[dict[str, Any]],
        model_call_index: int,
        attempt_kind: str,
    ) -> ContextManifest:
        """Create one manifest without retaining model-visible content."""
        fragments = self._message_fragments(
            messages,
            model_call_index=model_call_index,
            start=0,
        )
        fragments.extend(
            self._tool_fragments(
                tools,
                model_call_index=model_call_index,
                start=len(fragments),
            ),
        )
        if len(fragments) > self._policy.max_fragments:
            raise ContextManifestLimitError(
                "model input exceeds context manifest fragment limit",
            )
        fragment_tuple = tuple(fragments)
        total_size = sum(fragment.size_bytes or 0 for fragment in fragments)
        total_tokens = sum(
            fragment.estimated_tokens or 0 for fragment in fragments
        )
        disclosed_tools = sum(
            fragment.kind is ContextFragmentKind.TOOL_SCHEMA
            for fragment in fragments
        )
        identity = {
            "invocation_id": str(self._scope.invocation_id),
            "correlation_id": str(
                self._scope.correlation_id or self._scope.invocation_id,
            ),
            # Keep the v1 digest field name stable across the public
            # InvocationScope identity migration.
            "conversation_id": self._scope.chat_id,
            "registry_generation": self._scope.registry_generation,
            "capability_lock_id": (
                str(self._scope.capability_lock_id)
                if self._scope.capability_lock_id is not None
                else None
            ),
            "capability_lock_hash": self._scope.capability_lock_hash,
            "model_call_index": model_call_index,
            "attempt_kind": attempt_kind,
            "policy_id": self._policy.policy_id,
            "policy_version": self._policy.version,
            "fragments": [
                fragment.model_dump(mode="json") for fragment in fragments
            ],
        }
        if self._scope.registry_epoch_id is not None:
            identity["registry_epoch_id"] = str(
                self._scope.registry_epoch_id,
            )
        identity_digest = hashlib.sha256(
            _canonical_json(identity).encode(),
        ).hexdigest()
        manifest_hash = f"sha256:{identity_digest}"
        return ContextManifest(
            manifest_id=uuid5(
                self._scope.invocation_id,
                f"model-call:{model_call_index}:{attempt_kind}",
            ),
            invocation_id=self._scope.invocation_id,
            correlation_id=(
                self._scope.correlation_id or self._scope.invocation_id
            ),
            chat_id=self._scope.chat_id,
            registry_epoch_id=self._scope.registry_epoch_id,
            registry_generation=self._scope.registry_generation,
            capability_lock_id=self._scope.capability_lock_id,
            capability_lock_hash=self._scope.capability_lock_hash,
            model_call_index=model_call_index,
            attempt_kind=attempt_kind,
            policy_id=self._policy.policy_id,
            policy_version=self._policy.version,
            fragments=fragment_tuple,
            total_size_bytes=total_size,
            total_estimated_tokens=total_tokens,
            disclosed_tool_count=disclosed_tools,
            manifest_hash=manifest_hash,
        )


class FilesystemContextManifestStore:
    """Owner-only Lite store for immutable context manifests."""

    def __init__(self, workspace_dir: Path) -> None:
        self._root = (
            Path(workspace_dir) / ".qwenpaw" / "lite" / "context-manifests"
        )

    @staticmethod
    def _conversation_key(conversation_id: str) -> str:
        return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()

    def _path(self, manifest: ContextManifest) -> Path:
        owner = manifest.conversation_id or (
            f"invocation:{manifest.invocation_id}"
        )
        return (
            self._root
            / self._conversation_key(owner)
            / str(manifest.invocation_id)
            / f"{manifest.model_call_index:06d}.json"
        )

    async def append(self, manifest: ContextManifest) -> None:
        """Persist one model call exactly once."""
        path = self._path(manifest)
        async with get_path_lock(path):
            try:
                existing_payload = await read_json_async(path)
            except FileNotFoundError:
                existing_payload = None
            if existing_payload is not None:
                existing = ContextManifest.model_validate(existing_payload)
                existing_evidence = existing.model_dump(
                    mode="json",
                    exclude={"created_at"},
                )
                incoming_evidence = manifest.model_dump(
                    mode="json",
                    exclude={"created_at"},
                )
                if existing_evidence != incoming_evidence:
                    raise ContextManifestConflictError(
                        "model-call manifest already has different evidence",
                    )
                return
            await write_json_atomic_async(
                path,
                manifest.model_dump(mode="json"),
                sort_keys=True,
            )

    def _list_sync(
        self,
        conversation_id: str,
        limit: int,
    ) -> list[ContextManifest]:
        root = self._root / self._conversation_key(conversation_id)
        paths = sorted(root.glob("*/*.json"), reverse=True)
        manifests = [
            ContextManifest.model_validate(
                json.loads(path.read_text(encoding="utf-8")),
            )
            for path in paths
        ]
        manifests.sort(
            key=lambda item: (item.created_at, item.model_call_index),
            reverse=True,
        )
        return manifests[:limit]

    async def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
    ) -> Sequence[ContextManifest]:
        """Read newest manifests without blocking the event loop."""
        if not conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await run_sync_io(
            self._list_sync,
            conversation_id,
            limit,
        )


def lite_context_manifest_store(
    workspace_dir: Path,
) -> FilesystemContextManifestStore:
    """Return the Lite context audit store for one agent workspace."""
    return FilesystemContextManifestStore(workspace_dir)


__all__ = [
    "ContextManifestCompiler",
    "ContextManifestConflictError",
    "ContextManifestLimitError",
    "FilesystemContextManifestStore",
    "lite_context_manifest_store",
]
