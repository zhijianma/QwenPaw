# -*- coding: utf-8 -*-
"""Tests for the public QwenPaw Host handshake contract."""

from qwenpaw.kernel.host import HOST_FEATURES, HostHandshake


def test_host_handshake_serializes_stable_protocol_contract() -> None:
    handshake = HostHandshake(version="2.2.2b1")

    assert handshake.model_dump(mode="json") == {
        "schema": "qwenpaw.host-handshake.v1",
        "product": "qwenpaw",
        "version": "2.2.2b1",
        "protocol_version": 1,
        "features": list(HOST_FEATURES),
    }


def test_host_handshake_publishes_public_chat_capabilities() -> None:
    assert HOST_FEATURES == tuple(sorted(set(HOST_FEATURES)))
    assert {
        "chat.control.v1",
        "chat.evidence.v1",
        "chat.execution-manifests.v1",
        "chat.fork.v1",
        "chat.interactions",
        "chat.runtime.v1",
    }.issubset(HOST_FEATURES)


def test_host_handshake_accepts_future_optional_features() -> None:
    handshake = HostHandshake(
        version="2.2.2b1",
        features=("task.runtime", "future.feature"),
    )

    assert handshake.features == ("task.runtime", "future.feature")
