# -*- coding: utf-8 -*-
"""Delivery projection and dispatch adapters for QwenPaw editions."""

from .dispatch import (
    DeliveryDispatchAccountingError,
    DeliveryDispatchDisposition,
    DeliveryDispatchResult,
    DeliveryDispatcher,
)
from .channel import (
    SYSTEM_CHANNEL_DELIVERY_ID,
    SystemChannelDeliveryAdapter,
    encode_channel_address,
)
from .inbox import (
    SYSTEM_INBOX_ADDRESS,
    SYSTEM_INBOX_DELIVERY_ID,
    SystemInboxDeliveryAdapter,
)
from .sqlite import SQLiteDeliveryProjectionStore
from .projector import TaskDeliveryProjector
from .worker import TaskDeliveryWorker

__all__ = [
    "DeliveryDispatchAccountingError",
    "DeliveryDispatchDisposition",
    "DeliveryDispatchResult",
    "DeliveryDispatcher",
    "SYSTEM_CHANNEL_DELIVERY_ID",
    "SYSTEM_INBOX_ADDRESS",
    "SYSTEM_INBOX_DELIVERY_ID",
    "SQLiteDeliveryProjectionStore",
    "SystemChannelDeliveryAdapter",
    "SystemInboxDeliveryAdapter",
    "TaskDeliveryProjector",
    "TaskDeliveryWorker",
    "encode_channel_address",
]
