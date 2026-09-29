# -*- coding: utf-8 -*-
"""Tests for the safe Console-to-task conversation projection."""

import json

from qwenpaw.tasks.console_stream import ConsoleStreamCapture


def _line(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def test_capture_aggregates_visible_deltas_without_final_duplication() -> None:
    capture = ConsoleStreamCapture()

    first = capture.feed(
        _line(
            {
                "object": "content",
                "type": "text",
                "delta": True,
                "text": "Hello ",
            },
        ),
    )
    second = capture.feed(
        _line(
            {
                "object": "content",
                "type": "text",
                "delta": True,
                "text": "world",
            },
        ),
    )
    final = capture.feed(
        _line(
            {
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "Process-only explanation",
                            },
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Hello world"},
                        ],
                    },
                ],
            },
        ),
    )
    flushed = capture.finish()

    assert [signal.event_type for signal in first] == ["runner.responding"]
    assert not second
    assert [signal.event_type for signal in final] == [
        "conversation.assistant.delta",
    ]
    assert [signal.payload["text"] for signal in final] == [
        "Hello world",
    ]
    assert capture.completed_text == "Hello world"
    assert capture.completed_content == (
        {"type": "text", "text": "Hello world"},
    )
    assert not flushed


def test_capture_ignores_reasoning_and_uses_completed_text_fallback() -> None:
    capture = ConsoleStreamCapture()

    assert not capture.feed(
        _line(
            {
                "object": "content",
                "type": "reasoning",
                "delta": True,
                "text": "private thought",
            },
        ),
    )
    signals = capture.feed(
        _line(
            {
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Visible answer"},
                        ],
                    },
                ],
            },
        ),
    )
    signals += capture.finish()

    assert [signal.event_type for signal in signals] == [
        "runner.responding",
        "conversation.assistant.delta",
    ]
    assert signals[-1].payload["text"] == "Visible answer"


def test_capture_keeps_only_public_completed_message_content() -> None:
    capture = ConsoleStreamCapture()

    signals = capture.feed(
        _line(
            {
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "private thought"},
                        ],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Visible"},
                            {
                                "type": "image",
                                "image_url": "data:image/png;base64,aW1hZ2U=",
                            },
                            {
                                "type": "audio",
                                "data": "YXVkaW8=",
                                "format": "mpeg",
                            },
                            {
                                "type": "video",
                                "video_url": "https://example.test/video.mp4",
                            },
                            {
                                "type": "file",
                                "file_url": "https://example.test/report.pdf",
                                "filename": "report.pdf",
                            },
                        ],
                    },
                ],
            },
        ),
    )

    assert [signal.event_type for signal in signals] == [
        "runner.responding",
        "conversation.assistant.delta",
    ]
    assert capture.completed_text == "Visible"
    assert capture.completed_content == (
        {"type": "text", "text": "Visible"},
        {
            "type": "image",
            "image_url": "data:image/png;base64,aW1hZ2U=",
        },
        {"type": "audio", "data": "YXVkaW8=", "format": "mpeg"},
        {
            "type": "video",
            "video_url": "https://example.test/video.mp4",
        },
        {
            "type": "file",
            "file_url": "https://example.test/report.pdf",
            "filename": "report.pdf",
        },
    )


def test_capture_projects_turn_usage_without_context_estimates() -> None:
    capture = ConsoleStreamCapture()

    signals = capture.feed(
        _line(
            {
                "type": "turn_usage",
                "session_id": "task-1",
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "cost_micros": 45,
                },
                "context_usage": {
                    "estimated_tokens": 999,
                },
            },
        ),
    )

    assert len(signals) == 1
    assert signals[0].event_type == "usage.recorded"
    assert signals[0].payload == {
        "delta": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cost_micros": 45,
        },
    }


def test_capture_marks_missing_provider_price_as_unknown() -> None:
    capture = ConsoleStreamCapture()

    signals = capture.feed(
        _line(
            {
                "type": "turn_usage",
                "session_id": "task-1",
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                },
            },
        ),
    )

    assert len(signals) == 1
    assert signals[0].payload["delta"] == {
        "input_tokens": 12,
        "output_tokens": 3,
        "cost_micros": 0,
        "cost_unknown": True,
    }


def test_capture_projects_each_console_tool_call_once() -> None:
    capture = ConsoleStreamCapture()
    partial_call = {
        "object": "content",
        "type": "data",
        "status": "in_progress",
        "delta": True,
        "data": {
            "call_id": "call-1",
            "name": "read_file",
            "arguments": "",
        },
    }

    partial = capture.feed(_line(partial_call))
    completed_call = {
        **partial_call,
        "status": "completed",
        "delta": False,
        "data": {
            "call_id": "call-1",
            "name": "read_file",
            "arguments": '{"path":"README.md"}',
        },
    }
    first = capture.feed(_line(completed_call))
    duplicate = capture.feed(_line(completed_call))
    completed_output = {
        **partial_call,
        "status": "completed",
        "delta": False,
        "data": {
            "call_id": "call-1",
            "name": "read_file",
            "output": "done",
        },
    }
    output = capture.feed(_line(completed_output))
    duplicate_output = capture.feed(_line(completed_output))

    assert not partial
    assert len(first) == 1
    assert first[0].event_type == "tool.started"
    assert first[0].payload == {
        "call_id": "call-1",
        "name": "read_file",
        "arguments": '{"path":"README.md"}',
        "arguments_truncated": False,
        "usage_accounted": True,
    }
    assert not duplicate
    assert len(output) == 1
    assert output[0].event_type == "tool.completed"
    assert output[0].payload == {
        "call_id": "call-1",
        "name": "read_file",
        "output": "done",
        "output_truncated": False,
    }
    assert not duplicate_output


def test_capture_bounds_public_tool_previews() -> None:
    capture = ConsoleStreamCapture()
    long_value = "x" * (8 * 1024 + 1)

    started = capture.feed(
        _line(
            {
                "object": "content",
                "type": "data",
                "status": "completed",
                "data": {
                    "call_id": "call-long",
                    "name": "shell",
                    "arguments": long_value,
                },
            },
        ),
    )
    completed = capture.feed(
        _line(
            {
                "object": "content",
                "type": "data",
                "status": "completed",
                "data": {
                    "call_id": "call-long",
                    "name": "shell",
                    "output": long_value,
                },
            },
        ),
    )

    assert len(started[0].payload["arguments"]) == 8 * 1024
    assert started[0].payload["arguments_truncated"] is True
    assert len(completed[0].payload["output"]) == 8 * 1024
    assert completed[0].payload["output_truncated"] is True
