# -*- coding: utf-8 -*-
"""Tests for the public QwenPaw Host handshake contract."""

import pytest
from pydantic import ValidationError

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


def test_host_handshake_rejects_unknown_features() -> None:
    with pytest.raises(ValidationError):
        HostHandshake(
            version="2.2.2b1",
            features=("task.runtime", "unknown.feature"),
        )
