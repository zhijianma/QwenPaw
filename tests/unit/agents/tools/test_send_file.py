# -*- coding: utf-8 -*-
"""Tests for canonical artifact declarations from send_file_to_user."""

import pytest

from qwenpaw.agents.tools.send_file import send_file_to_user
from qwenpaw.runtime.tool_artifacts import TOOL_ARTIFACT_OUTPUTS_KEY


@pytest.mark.asyncio
async def test_successful_send_declares_host_captured_artifact(
    tmp_path,
) -> None:
    path = tmp_path / "report.md"
    path.write_text("report", encoding="utf-8")

    result = await send_file_to_user(str(path))

    [output] = result.metadata[TOOL_ARTIFACT_OUTPUTS_KEY]
    assert output["path_parameter"] == "file_path"
    assert output["path_normalization"] == "url"
    assert output["media_type"] == result.content[0].source.media_type


@pytest.mark.asyncio
async def test_failed_send_does_not_declare_artifact(tmp_path) -> None:
    result = await send_file_to_user(str(tmp_path / "missing.md"))

    assert TOOL_ARTIFACT_OUTPUTS_KEY not in result.metadata
