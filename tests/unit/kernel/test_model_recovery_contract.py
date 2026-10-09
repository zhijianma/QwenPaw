# -*- coding: utf-8 -*-
"""Tests for the provider-neutral Kernel model recovery contract."""

import pytest
from pydantic import ValidationError

from qwenpaw.kernel import (
    ModelFailureClass,
    ModelRecoveryDecision,
    ModelRecoveryDisposition,
)


@pytest.mark.parametrize(
    ("failure_class", "disposition"),
    [
        (failure, disposition)
        for failure, dispositions in {
            ModelFailureClass.TRANSPORT_UNAVAILABLE: (
                ModelRecoveryDisposition.RETRY_TRANSPORT,
            ),
            ModelFailureClass.STREAM_INTERRUPTED: (
                ModelRecoveryDisposition.CONTINUE_MODEL_STEP,
                ModelRecoveryDisposition.RECONCILE_SIDE_EFFECT,
            ),
            ModelFailureClass.PROVIDER_OVERLOADED: (
                ModelRecoveryDisposition.RETRY_TRANSPORT,
            ),
            ModelFailureClass.RATE_LIMITED: (
                ModelRecoveryDisposition.WAIT_RESOURCE,
            ),
            ModelFailureClass.QUOTA_EXHAUSTED: (
                ModelRecoveryDisposition.WAIT_RESOURCE,
            ),
            ModelFailureClass.BUDGET_EXHAUSTED: (
                ModelRecoveryDisposition.FAIL_TERMINAL,
            ),
            ModelFailureClass.AUTHENTICATION_REQUIRED: (
                ModelRecoveryDisposition.FAIL_TERMINAL,
            ),
            ModelFailureClass.POLICY_DENIED: (
                ModelRecoveryDisposition.FAIL_TERMINAL,
            ),
            ModelFailureClass.INVALID_REQUEST: (
                ModelRecoveryDisposition.FAIL_TERMINAL,
            ),
            ModelFailureClass.CONTEXT_OVERFLOW: (
                ModelRecoveryDisposition.FAIL_TERMINAL,
            ),
            ModelFailureClass.PROVIDER_UNAVAILABLE: (
                ModelRecoveryDisposition.RETRY_TRANSPORT,
            ),
            ModelFailureClass.USER_INTERRUPTED: (
                ModelRecoveryDisposition.STOP_INTERRUPTED,
            ),
            ModelFailureClass.UNKNOWN: (
                ModelRecoveryDisposition.FAIL_TERMINAL,
            ),
        }.items()
        for disposition in dispositions
    ],
)
def test_kernel_accepts_every_declared_recovery_pair(
    failure_class: ModelFailureClass,
    disposition: ModelRecoveryDisposition,
) -> None:
    decision = ModelRecoveryDecision(
        failure_class=failure_class,
        disposition=disposition,
    )

    assert decision.failure_class is failure_class
    assert decision.disposition is disposition


def test_kernel_rejects_undeclared_recovery_pair() -> None:
    with pytest.raises(
        ValidationError,
        match="disposition is not allowed for failure class",
    ):
        ModelRecoveryDecision(
            failure_class=ModelFailureClass.POLICY_DENIED,
            disposition=ModelRecoveryDisposition.RETRY_TRANSPORT,
        )


def test_kernel_limits_retry_after_to_rate_limit_wait() -> None:
    decision = ModelRecoveryDecision(
        failure_class=ModelFailureClass.RATE_LIMITED,
        disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
        retry_after_seconds=2.5,
    )
    assert decision.retry_after_seconds == 2.5

    with pytest.raises(
        ValidationError,
        match="retry-after hint requires rate-limited resource wait",
    ):
        ModelRecoveryDecision(
            failure_class=ModelFailureClass.QUOTA_EXHAUSTED,
            disposition=ModelRecoveryDisposition.WAIT_RESOURCE,
            retry_after_seconds=2.5,
        )
