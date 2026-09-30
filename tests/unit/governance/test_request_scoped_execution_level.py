# -*- coding: utf-8 -*-
"""Request-scoped governance execution-level contracts."""

from qwenpaw.governance.policy import (
    GovernanceAction,
    ToolCallSpec,
    _create_default_policy,
)


def test_internal_tool_strict_override_asks_without_mutation() -> None:
    """STRICT governs internal tools without changing shared policy state."""
    policy = _create_default_policy(workspace_dir="/tmp/test-workspace")
    tool_call = ToolCallSpec(
        tool_name="GetCurrentTime",
        target="",
        agent_id="agent-1",
        session_id="chat-1",
    )

    strict = policy.evaluate(tool_call, execution_level="strict")

    assert strict.action is GovernanceAction.ASK
    assert strict.source == "STRICT mode"
    assert policy.execution_level == "smart"
    assert policy.evaluate(tool_call).action is GovernanceAction.ALLOW
