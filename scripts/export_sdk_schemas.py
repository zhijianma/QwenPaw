# -*- coding: utf-8 -*-
"""Export stable QwenPaw SDK response contracts as JSON Schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from qwenpaw.app.task_http_contracts import TaskProjectionResponse
from qwenpaw.kernel.host import HostHandshake
from qwenpaw.kernel.interactions import (
    InteractionRequest,
    InteractionResolution,
)

_CONTRACTS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("host-handshake", HostHandshake),
    ("interaction-request", InteractionRequest),
    ("interaction-resolution", InteractionResolution),
    ("task-projection", TaskProjectionResponse),
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
    for stem, model in _CONTRACTS:
        schema = model.model_json_schema(
            by_alias=True,
            mode="serialization",
        )
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
