# -*- coding: utf-8 -*-
"""Tests for deterministic SDK contract schema publication."""

import json
from pathlib import Path

from scripts.export_sdk_schemas import export_sdk_schemas
from qwenpaw.kernel.host import HostHandshake

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_sdk_schema_export_is_deterministic_and_complete(tmp_path) -> None:
    first = export_sdk_schemas(tmp_path)
    first_content = {path.name: path.read_bytes() for path in first}
    second = export_sdk_schemas(tmp_path)

    assert first_content == {path.name: path.read_bytes() for path in second}
    assert set(first_content) == {
        "host-handshake.schema.json",
        "interaction-request.schema.json",
        "interaction-resolution.schema.json",
        "interaction-record.schema.json",
        "chat-control-request.schema.json",
        "chat-steer-request.schema.json",
        "chat-queue-reorder-request.schema.json",
        "chat-submission-request.schema.json",
        "control-record.schema.json",
        "control-receipt.schema.json",
        "queue-projection.schema.json",
        "task-projection.schema.json",
        "action-record.schema.json",
        "conversation-artifact-record.schema.json",
        "observation-page.schema.json",
        "conversation-trajectory-page.schema.json",
        "model-call-record.schema.json",
        "wait-condition.schema.json",
        "capability-lock-manifest.schema.json",
        "context-manifest.schema.json",
    }
    task_schema = json.loads(first_content["task-projection.schema.json"])
    assert task_schema["$id"].endswith("/task-projection.v1.json")
    assert "last_sequence" in task_schema["required"]
    assert (
        "registry_generation"
        in task_schema["$defs"]["ExecutionEvent"]["required"]
    )
    queue_schema = json.loads(first_content["queue-projection.schema.json"])
    assert queue_schema["$id"].endswith("/queue-projection.v1.json")
    assert "chat_id" in queue_schema["required"]
    assert "session_id" not in queue_schema["properties"]
    control_schema = json.loads(first_content["control-record.schema.json"])
    assert control_schema["$id"].endswith("/control-record.v1.json")
    assert "chat_id" in control_schema["$defs"]["ControlCommand"]["required"]
    assert (
        "conversation_id"
        not in control_schema["$defs"]["ControlCommand"]["properties"]
    )
    lock_schema = json.loads(
        first_content["capability-lock-manifest.schema.json"],
    )
    assert "chat_id" in lock_schema["required"]
    assert "conversation_id" not in lock_schema["properties"]
    context_schema = json.loads(first_content["context-manifest.schema.json"])
    assert "chat_id" in context_schema["required"]
    assert "conversation_id" not in context_schema["properties"]


def test_committed_sdk_schemas_match_kernel_models(tmp_path) -> None:
    exported = export_sdk_schemas(tmp_path)
    committed_root = _REPOSITORY_ROOT / "schemas" / "sdk"

    for generated_path in exported:
        committed_path = committed_root / generated_path.name
        assert (
            committed_path.read_bytes() == generated_path.read_bytes()
        ), f"{committed_path} is stale; run scripts/export_sdk_schemas.py"


def test_host_handshake_accepts_future_optional_features() -> None:
    handshake = HostHandshake(
        version="2.2.2b1",
        features=("task.runtime", "future.feature"),
    )

    assert handshake.features == ("task.runtime", "future.feature")
