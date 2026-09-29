# -*- coding: utf-8 -*-
"""Tests for Host-authored durable approval deadlines."""

from qwenpaw.app.approvals.timeouts import approval_timeout_seconds


def test_durable_task_uses_execution_contract_approval_timeout() -> None:
    context = {
        "durable_task": True,
        "_task_approval_broker": object(),
        "execution_contract": {
            "timeout_policy": {"approval_seconds": 12.5},
        },
    }

    assert approval_timeout_seconds(context, default=300) == 12.5


def test_untrusted_or_invalid_timeout_uses_process_default() -> None:
    forged = {
        "durable_task": True,
        "execution_contract": {
            "timeout_policy": {"approval_seconds": 1},
        },
    }
    invalid = {
        "durable_task": True,
        "_task_approval_broker": object(),
        "execution_contract": {
            "timeout_policy": {"approval_seconds": -1},
        },
    }

    assert approval_timeout_seconds(forged, default=300) == 300
    assert approval_timeout_seconds(invalid, default=300) == 300
