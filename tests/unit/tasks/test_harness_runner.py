# -*- coding: utf-8 -*-
"""Contract tests for Harness-to-Task signal projection."""

import asyncio

import pytest

from qwenpaw.harnesses.events import HarnessEvent, HarnessEventKind
from qwenpaw.tasks.harness_runner import (
    HarnessRunnerError,
    HarnessSignalProjector,
)


def test_harness_tool_identity_cannot_be_overridden_by_provider_data() -> None:
    projector = HarnessSignalProjector("qwenpaw.system.tasks.codex-harness")

    signal = projector.project(
        HarnessEvent(
            kind=HarnessEventKind.TOOL_STARTED,
            item_id="trusted-call",
            tool_name="shell",
            data={
                "call_id": "forged-call",
                "name": "forged-tool",
                "usage_accounted": True,
            },
        ),
    )[0]

    assert signal.payload["call_id"] == "trusted-call"
    assert signal.payload["name"] == "shell"
    assert signal.payload["usage_accounted"] is False


@pytest.mark.parametrize(
    "event",
    [
        HarnessEvent(
            kind=HarnessEventKind.TOOL_STARTED,
            tool_name="shell",
        ),
        HarnessEvent(
            kind=HarnessEventKind.TOOL_COMPLETED,
            item_id="call-1",
        ),
    ],
)
def test_harness_tool_events_require_stable_identity(
    event: HarnessEvent,
) -> None:
    projector = HarnessSignalProjector("qwenpaw.system.tasks.codex-harness")

    with pytest.raises(HarnessRunnerError):
        projector.project(event)


def test_harness_error_and_cancel_are_terminal_fail_closed_signals() -> None:
    error_projector = HarnessSignalProjector(
        "qwenpaw.system.tasks.codex-harness",
    )
    with pytest.raises(HarnessRunnerError, match="provider failed"):
        error_projector.project(
            HarnessEvent(
                kind=HarnessEventKind.ERROR,
                text="provider failed",
            ),
        )

    cancel_projector = HarnessSignalProjector(
        "qwenpaw.system.tasks.codex-harness",
    )
    with pytest.raises(asyncio.CancelledError):
        cancel_projector.project(
            HarnessEvent(kind=HarnessEventKind.CANCELLED),
        )


def test_harness_completion_emits_one_committed_visible_response() -> None:
    projector = HarnessSignalProjector(
        "qwenpaw.system.tasks.codex-harness",
    )

    signals = []
    signals.extend(
        projector.project(
            HarnessEvent(
                kind=HarnessEventKind.TEXT_DELTA,
                text="Visible ",
            ),
        ),
    )
    signals.extend(
        projector.project(
            HarnessEvent(
                kind=HarnessEventKind.TEXT_DELTA,
                text="answer",
            ),
        ),
    )
    terminal = projector.project(
        HarnessEvent(kind=HarnessEventKind.COMPLETED),
    )

    assert [signal.event_type for signal in signals] == [
        "runner.responding",
        "conversation.assistant.delta",
        "conversation.assistant.delta",
    ]
    assert len(terminal) == 1
    assert terminal[0].event_type == "conversation.assistant.completed"
    assert terminal[0].payload == {
        "role": "assistant",
        "text": "Visible answer",
    }


def test_harness_empty_completion_does_not_invent_a_reply() -> None:
    projector = HarnessSignalProjector(
        "qwenpaw.system.tasks.codex-harness",
    )

    assert not projector.project(
        HarnessEvent(kind=HarnessEventKind.COMPLETED),
    )
    assert projector.completed is True
