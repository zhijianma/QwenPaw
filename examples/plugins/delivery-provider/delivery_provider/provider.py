# -*- coding: utf-8 -*-
"""Local JSONL Delivery Adapter implemented only with the public SDK."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

from qwenpaw.plugins.sdk import (
    DeliveryMode,
    DeliveryReceipt,
    DeliveryRequest,
    DeliveryStatus,
)

ADAPTER_ID = "delivery-provider.local-jsonl"
DELIVERY_ADDRESS = "local-jsonl"
_WRITE_LOCK = threading.Lock()


class LocalJsonlDeliveryAdapter:
    """Append committed public delivery projections to a local JSONL file."""

    adapter_id = ADAPTER_ID

    def __init__(self, output_path: Path) -> None:
        self._output_path = Path(output_path)

    async def health_check(self) -> bool:
        """Report readiness without creating the output file."""
        return True

    def supports(self, request: DeliveryRequest) -> bool:
        """Accept non-silent requests addressed to this Adapter."""
        return (
            request.destination.adapter_id == self.adapter_id
            and request.destination.address == DELIVERY_ADDRESS
            and request.mode is not DeliveryMode.SILENT
        )

    async def deliver(
        self,
        request: DeliveryRequest,
        *,
        attempt: int,
    ) -> DeliveryReceipt:
        """Write one host-admitted attempt and return its exact receipt."""
        delivery_id = request.delivery_id
        if delivery_id is None:  # pragma: no cover - Kernel validation
            raise ValueError("delivery request has no stable identity")
        if not self.supports(request):
            return DeliveryReceipt(
                delivery_id=delivery_id,
                adapter_id=self.adapter_id,
                status=DeliveryStatus.FAILED,
                attempt=attempt,
                error_code="jsonl_destination_unsupported",
            )
        record = {
            "attempt": attempt,
            "request": request.model_dump(mode="json"),
        }
        encoded = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        await asyncio.to_thread(self._append, encoded)
        return DeliveryReceipt(
            delivery_id=delivery_id,
            adapter_id=self.adapter_id,
            status=DeliveryStatus.DELIVERED,
            attempt=attempt,
            metadata={"format": "jsonl"},
        )

    def _append(self, encoded: str) -> None:
        """Append one complete line outside the event loop."""
        with _WRITE_LOCK:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with self._output_path.open("a", encoding="utf-8") as output:
                output.write(f"{encoded}\n")


def create_adapter() -> LocalJsonlDeliveryAdapter:
    """Create the example Adapter from one explicit output location."""
    configured = os.environ.get("QWENPAW_EXAMPLE_DELIVERY_LOG", "").strip()
    output_path = (
        Path(configured).expanduser()
        if configured
        else Path.cwd() / ".qwenpaw" / "example-delivery.jsonl"
    )
    return LocalJsonlDeliveryAdapter(output_path)
