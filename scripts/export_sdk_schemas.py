# -*- coding: utf-8 -*-
"""Export stable QwenPaw SDK response contracts as JSON Schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from qwenpaw.app.chats.models import (
    ChatControlRequest,
    ChatQueueReorderRequest,
    ChatSteerRequest,
    ChatSubmissionRequest,
)
from qwenpaw.app.task_http_contracts import TaskProjectionResponse
from qwenpaw.kernel.artifacts import ConversationArtifactRecord
from qwenpaw.kernel.capability_locks import CapabilityLockManifest
from qwenpaw.kernel.host import HostHandshake
from qwenpaw.kernel.interactions import (
    InteractionRecord,
    InteractionRequest,
    InteractionResolution,
)
from qwenpaw.kernel.invocation_control import (
    ControlRecord,
    ControlReceipt,
    QueueProjection,
)
from qwenpaw.kernel.models import (
    ActionRecord,
    ContextManifest,
    ModelCallRecord,
)
from qwenpaw.kernel.observations import (
    ConversationTrajectoryPage,
    ObservationPage,
)
from qwenpaw.kernel.waits import WaitCondition

_CONTRACTS: tuple[tuple[str, type[BaseModel], bool], ...] = (
    ("host-handshake", HostHandshake, True),
    ("interaction-request", InteractionRequest, True),
    ("interaction-resolution", InteractionResolution, True),
    ("interaction-record", InteractionRecord, True),
    ("chat-control-request", ChatControlRequest, False),
    ("chat-steer-request", ChatSteerRequest, False),
    ("chat-queue-reorder-request", ChatQueueReorderRequest, False),
    ("chat-submission-request", ChatSubmissionRequest, False),
    ("control-record", ControlRecord, True),
    ("control-receipt", ControlReceipt, True),
    ("queue-projection", QueueProjection, True),
    ("task-projection", TaskProjectionResponse, True),
    ("action-record", ActionRecord, True),
    ("conversation-artifact-record", ConversationArtifactRecord, True),
    ("observation-page", ObservationPage, True),
    ("conversation-trajectory-page", ConversationTrajectoryPage, True),
    ("model-call-record", ModelCallRecord, True),
    ("wait-condition", WaitCondition, True),
    ("capability-lock-manifest", CapabilityLockManifest, True),
    ("context-manifest", ContextManifest, True),
)


def _require_serialized_properties(value: Any) -> None:
    """Mark fields emitted by model serialization as required."""
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties)
        for nested in value.values():
            _require_serialized_properties(nested)
    elif isinstance(value, list):
        for nested in value:
            _require_serialized_properties(nested)


def export_sdk_schemas(output_dir: Path) -> tuple[Path, ...]:
    """Write deterministic schemas and return their paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for stem, model, response_contract in _CONTRACTS:
        schema = model.model_json_schema(
            by_alias=True,
            mode="serialization" if response_contract else "validation",
        )
        if response_contract:
            _require_serialized_properties(schema)
        schema["$id"] = f"https://schemas.qwenpaw.dev/sdk/{stem}.v1.json"
        path = output_dir / f"{stem}.schema.json"
        path.write_text(
            f"{json.dumps(schema, indent=2, sort_keys=True)}\n",
            encoding="utf-8",
        )
        written.append(path)
    return tuple(written)


def main() -> None:
    """Run the deterministic schema export command."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("schemas/sdk"),
    )
    args = parser.parse_args()
    for path in export_sdk_schemas(args.output):
        print(path)


if __name__ == "__main__":
    main()
