# -*- coding: utf-8 -*-
"""UT for the tool_adapter approval-scope consumer (the lever).

``_ask_user_approval`` must pick the recorded rule target from the user's
chosen scope: SIMILAR → the generalized pattern, EXACT/None → the literal
target. This exercises that decision with a fake ApprovalService + governor
so no real model / HTTP / agentscope runtime is needed.
"""
from __future__ import annotations

import time

# pylint: disable=protected-access

from qwenpaw.governance.policy import (
    GovernanceAction,
    GovernanceDecision,
    ToolCallSpec,
)
from qwenpaw.kernel.models import ApprovalSource
from qwenpaw.security.tool_guard.approval import (
    ApprovalDecision,
    ApprovalScope,
)


class _FakePending:
    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.created_at = time.time()
        self.timeout_seconds = 300.0
        self.scope: ApprovalScope | None = None
        self.resolution_hook = None


class _FakeApprovalService:
    """Stand-in for ApprovalService.

    ``wait_for_approval`` resolves APPROVED and stashes the chosen scope on
    the pending record — mirroring what ``resolve_request`` does for real.
    """

    def __init__(self, scope: ApprovalScope | None) -> None:
        self._scope = scope
        self._pending = _FakePending("fake-req-id")
        self.created_timeout_seconds: float | None = None
        self.wait_timeout_seconds: float | None = None

    async def cancel_stale_pending_for_tool_call(
        self,
        *_a,
        **_kw,
    ):  # noqa: ANN
        return 0

    async def create_pending(self, **kwargs):  # noqa: ANN
        # Carry the display payload so we can assert on it too.
        self._pending.extra = kwargs.get("extra", {})
        self.created_timeout_seconds = kwargs.get("timeout_seconds")
        self._pending.timeout_seconds = self.created_timeout_seconds
        return self._pending

    async def wait_for_approval(
        self,
        _request_id,
        timeout_seconds,
    ):  # noqa: ANN
        self.wait_timeout_seconds = timeout_seconds
        self._pending.scope = self._scope
        return ApprovalDecision.APPROVED


class _FakeGovernor:
    def __init__(self) -> None:
        self.audits: list = []
        self.added: list[tuple[ToolCallSpec, str]] = []

    def audit(self, tc_spec, decision):  # noqa: ANN
        self.audits.append((tc_spec, decision))

    async def add_approved_rule(
        self,
        tc_spec,
        *,
        generalized_target,
    ):  # noqa: ANN
        self.added.append((tc_spec, generalized_target))
        return True

    def is_builtin_ask(self, _tc_spec):  # noqa: ANN
        return False


def _tc(target: str = "git status") -> ToolCallSpec:
    return ToolCallSpec(
        tool_name="Bash",
        target=target,
        agent_id="agent-1",
        session_id="session-1",
    )


async def _run_approval(
    scope: ApprovalScope | None,
    monkeypatch,
    *,
    governor=None,
    tc_spec: ToolCallSpec | None = None,
):
    """Drive ``_ask_user_approval`` with fakes; return (governor, pending)."""
    from qwenpaw.governance import tool_adapter

    fake_svc = _FakeApprovalService(scope)
    monkeypatch.setattr(
        tool_adapter,
        "get_approval_service",
        lambda: fake_svc,
        raising=False,
    )
    # Avoid the real LLM generalization round-trip. ``_ask_user_approval``
    # imports this lazily from ``.generalize`` inside the function body, so
    # the patch must land on the generalize module, not tool_adapter.
    import qwenpaw.governance.generalize as generalize_mod

    async def _fake_generalize(
        _tool_name,
        _target,
        _source,
        agent_id=None,
    ):  # noqa: ANN
        del agent_id
        return "git *"

    monkeypatch.setattr(
        generalize_mod,
        "generalize_target_for_approval",
        _fake_generalize,
        raising=False,
    )

    governor = governor or _FakeGovernor()
    # ``_ask_user_approval`` imports get_approval_service lazily from
    # ..app.approvals; patch that path too.
    import qwenpaw.app.approvals as approvals_mod

    monkeypatch.setattr(
        approvals_mod,
        "get_approval_service",
        lambda: fake_svc,
        raising=False,
    )

    decision = await tool_adapter._ask_user_approval(
        governor=governor,
        tc_spec=tc_spec or _tc(),
        request_context={
            "user_id": "u",
            "channel": "console",
            "root_session_id": "session-1",
            "root_agent_id": "agent-1",
            "tool_call_id": "tc-1",
        },
        source="No rule hit",
    )
    return governor, fake_svc._pending, decision


class TestApprovalScopeConsumer:
    """The consumer picks the recorded target from the chosen scope."""

    async def test_similar_records_pattern(self, monkeypatch):
        governor, _pending, _decision = await _run_approval(
            ApprovalScope.SIMILAR,
            monkeypatch,
        )
        assert governor.added, "no rule was recorded"
        _tc_spec, target = governor.added[0]
        assert target == "git *"
        # Audit reason carries the scope label.
        _spec, decision = governor.audits[-1]
        assert "similar" in decision.reason

    async def test_exact_records_literal(self, monkeypatch):
        governor, _pending, _decision = await _run_approval(
            ApprovalScope.EXACT,
            monkeypatch,
        )
        _tc_spec, target = governor.added[0]
        assert target == "git status"
        _spec, decision = governor.audits[-1]
        assert "exact" in decision.reason

    async def test_durable_task_installs_ledger_bridge(
        self,
        monkeypatch,
    ):
        """The active PolicyGuardedTool path persists Task approvals."""
        from qwenpaw.app.approvals import task_bridge
        from qwenpaw.governance import tool_adapter

        fake_svc = _FakeApprovalService(ApprovalScope.EXACT)
        monkeypatch.setattr(
            "qwenpaw.app.approvals.get_approval_service",
            lambda: fake_svc,
        )

        class _FakeBridge:
            def __init__(self) -> None:
                self.requests: list[dict] = []

            async def request(self, **kwargs):  # noqa: ANN
                self.requests.append(kwargs)

            async def resolve(self, *_args, **_kwargs):  # noqa: ANN
                return None

        bridge = _FakeBridge()

        async def _bridge_factory(_context, _request_id):  # noqa: ANN
            return bridge

        monkeypatch.setattr(
            task_bridge,
            "task_approval_bridge_from_context",
            _bridge_factory,
        )
        import qwenpaw.governance.generalize as generalize_mod

        async def _fake_generalize(
            _tool_name,
            target,
            _source,
            agent_id=None,
        ):  # noqa: ANN
            del agent_id
            return target

        monkeypatch.setattr(
            generalize_mod,
            "generalize_target_for_approval",
            _fake_generalize,
        )

        await tool_adapter._ask_user_approval(
            governor=_FakeGovernor(),
            tc_spec=_tc("README.md"),
            request_context={
                "durable_task": True,
                "_task_approval_broker": object(),
                "execution_contract": {
                    "timeout_policy": {"approval_seconds": 12.5},
                },
            },
        )

        assert len(bridge.requests) == 1
        request = bridge.requests[0]
        assert request["agent_id"] == "agent-1"
        assert request["tool_name"] == "Bash"
        assert request["severity"] == "INFO"
        assert request["input_data"] == {}
        assert request["source"] is ApprovalSource.TOOL
        assert request["action"] == "tool.execute"
        assert request["policy"] == "governance_policy"
        assert request["display"].target == "README.md"
        assert fake_svc.created_timeout_seconds == 12.5
        assert fake_svc.wait_timeout_seconds == 12.5
        resolution_hook = fake_svc._pending.resolution_hook
        assert callable(resolution_hook)
        assert resolution_hook.__self__ is bridge

    async def test_none_scope_defaults_to_exact(self, monkeypatch):
        """No scope (IM channel / CLI) → records the literal target."""
        governor, _pending, _decision = await _run_approval(
            None,
            monkeypatch,
        )
        _tc_spec, target = governor.added[0]
        assert target == "git status"
        _spec, decision = governor.audits[-1]
        assert "exact" in decision.reason

    async def test_display_payload_carries_both_targets(self, monkeypatch):
        _governor, pending, _decision = await _run_approval(
            ApprovalScope.SIMILAR,
            monkeypatch,
        )
        display = pending.extra["display"]
        assert display["is_generalized"] is True
        assert display["exact_target"] == "git status"
        assert display["similar_target"] == "git *"

    async def test_approved_side_effect_denied_when_audit_fails(
        self,
        monkeypatch,
    ):
        class _UnavailableAuditGovernor(_FakeGovernor):
            def audit(self, tc_spec, decision):  # noqa: ANN
                super().audit(tc_spec, decision)
                return False

        governor = _UnavailableAuditGovernor()
        tc_spec = _tc()
        tc_spec.effect = "external_write"

        governor, _pending, decision = await _run_approval(
            ApprovalScope.EXACT,
            monkeypatch,
            governor=governor,
            tc_spec=tc_spec,
        )

        assert decision.behavior.value == "deny"
        assert "audit could not be persisted" in decision.message
        assert governor.added == []


class TestRequestScopedApprovalLevel:
    """Approval overrides belong to one invocation, not shared policy state."""

    async def test_strict_is_passed_without_mutating_governor_policy(self):
        from types import SimpleNamespace

        from qwenpaw.governance import tool_adapter

        class _Governor:
            def __init__(self) -> None:
                self.policy = SimpleNamespace(execution_level="smart")
                self.received_level: str | None = None

            def assert_policy(
                self,
                _tc_spec,
                *,
                execution_level=None,
            ):  # noqa: ANN
                self.received_level = execution_level
                return GovernanceDecision(
                    action=GovernanceAction.ALLOW,
                    reason="test",
                )

            def audit(self, _tc_spec, _decision):  # noqa: ANN
                return None

        governor = _Governor()
        tool = SimpleNamespace(
            name="get_current_time",
            _qp_governor=governor,
            _qp_request_context={"approval_level": "strict"},
            _build_tc_spec=lambda: ToolCallSpec(
                tool_name="GetCurrentTime",
                target="",
                agent_id="agent-1",
                session_id="chat-1",
                tool_type="internal",
            ),
        )

        decision = await tool_adapter._policy_tool_check_permissions(tool, {})

        assert decision.behavior.value == "allow"
        assert governor.received_level == "strict"
        assert governor.policy.execution_level == "smart"

    async def test_side_effect_is_denied_when_policy_audit_fails(self):
        from types import SimpleNamespace

        from qwenpaw.governance import tool_adapter

        class _Governor:
            policy = SimpleNamespace(execution_level="smart")

            @staticmethod
            def assert_policy(_tc_spec, *, execution_level=None):
                del execution_level
                return GovernanceDecision(
                    action=GovernanceAction.ALLOW,
                    reason="test",
                )

            @staticmethod
            def audit(_tc_spec, _decision):
                return False

        tool = SimpleNamespace(
            name="write_file",
            _qp_governor=_Governor(),
            _qp_request_context={"approval_level": "strict"},
            _build_tc_spec=lambda: ToolCallSpec(
                tool_name="Write",
                target="README.md",
                agent_id="agent-1",
                session_id="chat-1",
                effect="local_write",
            ),
        )

        decision = await tool_adapter._policy_tool_check_permissions(tool, {})

        assert decision.behavior.value == "deny"
        assert "audit could not be persisted" in decision.message

    async def test_read_only_tool_remains_available_when_audit_fails(self):
        from types import SimpleNamespace

        from qwenpaw.governance import tool_adapter

        class _Governor:
            policy = SimpleNamespace(execution_level="smart")

            @staticmethod
            def assert_policy(_tc_spec, *, execution_level=None):
                del execution_level
                return GovernanceDecision(
                    action=GovernanceAction.ALLOW,
                    reason="test",
                )

            @staticmethod
            def audit(_tc_spec, _decision):
                return False

        tool = SimpleNamespace(
            name="read_file",
            _qp_governor=_Governor(),
            _qp_request_context={"approval_level": "strict"},
            _build_tc_spec=lambda: ToolCallSpec(
                tool_name="Read",
                target="README.md",
                agent_id="agent-1",
                session_id="chat-1",
                effect="none",
            ),
        )

        decision = await tool_adapter._policy_tool_check_permissions(tool, {})

        assert decision.behavior.value == "allow"

    async def test_disabled_approval_still_requires_side_effect_audit(self):
        from types import SimpleNamespace

        from qwenpaw.governance import tool_adapter

        class _Governor:
            policy = SimpleNamespace(execution_level="off")
            sandbox_usable = False

            @staticmethod
            def audit(_tc_spec, _decision):
                return False

        tool = SimpleNamespace(
            name="write_file",
            _qp_governor=_Governor(),
            _qp_request_context={"approval_level": "off"},
            _build_tc_spec=lambda: ToolCallSpec(
                tool_name="Write",
                target="README.md",
                agent_id="agent-1",
                session_id="chat-1",
                effect="local_write",
            ),
        )

        decision = await tool_adapter._policy_tool_check_permissions(tool, {})

        assert decision.behavior.value == "deny"
        assert "even when approval is disabled" in decision.message
