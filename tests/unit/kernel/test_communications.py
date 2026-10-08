# -*- coding: utf-8 -*-
"""Tests for transport-neutral communication capability contracts."""

from pydantic import ValidationError
import pytest

from qwenpaw.kernel import (
    CommunicationBackpressure,
    CommunicationCapability,
    CommunicationContract,
    CommunicationCursorSemantics,
    CommunicationDeliveryMode,
    CommunicationDisconnectPolicy,
    CommunicationIdempotency,
    CommunicationOrderingScope,
    CommunicationRetention,
)


def _snapshot_stream() -> CommunicationCapability:
    return CommunicationCapability(
        capability_id="qwenpaw.test.snapshot-stream.v1",
        delivery_mode=CommunicationDeliveryMode.REQUEST_STREAM,
        ordering_scope=CommunicationOrderingScope.CONVERSATION,
        idempotency=CommunicationIdempotency.NOT_APPLICABLE,
        cursor_semantics=CommunicationCursorSemantics.SNAPSHOT_CHANGE,
        retention=CommunicationRetention.LATEST_STATE,
        backpressure=CommunicationBackpressure.COALESCE_LATEST,
        disconnect_policy=(
            CommunicationDisconnectPolicy.RECONNECT_SNAPSHOT
        ),
    )


def test_contract_accepts_snapshot_reconnect_without_replay_claim() -> None:
    contract = CommunicationContract(
        contract_id="qwenpaw.test.chat-runtime.v1",
        capabilities=(_snapshot_stream(),),
    )

    capability = contract.capabilities[0]
    assert (
        capability.cursor_semantics
        is CommunicationCursorSemantics.SNAPSHOT_CHANGE
    )
    assert capability.retention is CommunicationRetention.LATEST_STATE


def test_snapshot_reconnect_requires_snapshot_cursor() -> None:
    with pytest.raises(
        ValidationError,
        match="snapshot reconnect requires",
    ):
        CommunicationCapability(
            capability_id="qwenpaw.test.invalid-stream.v1",
            delivery_mode=CommunicationDeliveryMode.REQUEST_STREAM,
            ordering_scope=CommunicationOrderingScope.CONNECTION,
            idempotency=CommunicationIdempotency.NOT_APPLICABLE,
            cursor_semantics=CommunicationCursorSemantics.NONE,
            retention=CommunicationRetention.EPHEMERAL,
            backpressure=CommunicationBackpressure.NONE,
            disconnect_policy=(
                CommunicationDisconnectPolicy.RECONNECT_SNAPSHOT
            ),
        )


def test_durable_channel_cannot_omit_acknowledged_replay() -> None:
    with pytest.raises(
        ValidationError,
        match="durable channel requires",
    ):
        CommunicationCapability(
            capability_id="qwenpaw.test.false-channel.v1",
            delivery_mode=CommunicationDeliveryMode.DURABLE_CHANNEL,
            ordering_scope=CommunicationOrderingScope.CONVERSATION,
            idempotency=CommunicationIdempotency.REQUIRED,
            cursor_semantics=CommunicationCursorSemantics.REPLAY_OFFSET,
            retention=CommunicationRetention.DURABLE,
            backpressure=CommunicationBackpressure.SERVER_QUEUE,
            disconnect_policy=CommunicationDisconnectPolicy.RESUME_CURSOR,
        )


def test_durable_handle_requires_idempotent_disconnected_execution() -> None:
    with pytest.raises(
        ValidationError,
        match="durable handle must outlive",
    ):
        CommunicationCapability(
            capability_id="qwenpaw.test.false-handle.v1",
            delivery_mode=CommunicationDeliveryMode.DURABLE_HANDLE,
            ordering_scope=CommunicationOrderingScope.CONVERSATION,
            idempotency=CommunicationIdempotency.OPTIONAL,
            cursor_semantics=CommunicationCursorSemantics.NONE,
            retention=CommunicationRetention.DURABLE,
            backpressure=CommunicationBackpressure.SERVER_QUEUE,
            disconnect_policy=CommunicationDisconnectPolicy.CONTINUE,
        )


def test_contract_rejects_duplicate_capability_identity() -> None:
    capability = _snapshot_stream()

    with pytest.raises(
        ValidationError,
        match="communication capabilities must be unique",
    ):
        CommunicationContract(
            contract_id="qwenpaw.test.chat-runtime.v1",
            capabilities=(capability, capability),
        )
