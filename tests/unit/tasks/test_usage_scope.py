# -*- coding: utf-8 -*-
"""Tests for root Task usage scope inheritance."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.agents.tools import agent_management
from qwenpaw.app.agent_context import set_current_usage_scope
from qwenpaw.app.routers.console import (
    _bind_inherited_usage_scope,
    _release_bound_usage_scope,
)
from qwenpaw.kernel.models import UsageDelta, UsageSnapshot
from qwenpaw.tasks.usage_scope import (
    RECORD_MODEL_USAGE_CONTEXT_KEY,
    USAGE_SCOPE_CONTEXT_KEY,
    USAGE_SCOPE_LEASE_CONTEXT_KEY,
    UsageScopeRegistry,
)
from qwenpaw.token_usage.model_wrapper import TokenRecordingModelWrapper


class _Meter:
    def __init__(self) -> None:
        self.deltas: list[UsageDelta] = []

    async def record(
        self,
        delta: UsageDelta,
        *,
        source: str | None = None,
    ) -> UsageSnapshot:
        del source
        self.deltas.append(delta)
        snapshot = UsageSnapshot()
        for item in self.deltas:
            snapshot = snapshot.add(item)
        return snapshot

    async def snapshot(self) -> UsageSnapshot:
        snapshot = UsageSnapshot()
        for item in self.deltas:
            snapshot = snapshot.add(item)
        return snapshot

    async def acquire_concurrency(self) -> None:
        return None

    def release_concurrency(self) -> None:
        return None


def test_scope_lease_survives_root_close_while_child_is_live() -> None:
    registry = UsageScopeRegistry()
    meter = _Meter()
    root = registry.open_root(meter, agent_id="default")
    child = registry.acquire(root.scope_id, agent_id="default")

    root.close()

    assert registry.contains(child.scope_id)
    assert child.meter is meter
    child.close()
    assert not registry.contains(child.scope_id)


def test_scope_is_bound_to_agent_identity() -> None:
    registry = UsageScopeRegistry()
    root = registry.open_root(_Meter(), agent_id="agent-a")

    with pytest.raises(LookupError):
        registry.acquire(root.scope_id, agent_id="agent-b")

    root.close()


def test_spawn_context_propagates_only_opaque_scope() -> None:
    meter = _Meter()
    set_current_usage_scope(
        "scope-1",
        meter,
        record_model_usage=True,
    )
    try:
        context = agent_management._build_spawn_request_context("default")
    finally:
        set_current_usage_scope(None, None, record_model_usage=False)

    assert context[USAGE_SCOPE_CONTEXT_KEY] == "scope-1"
    assert "_task_usage_meter" not in context
    assert USAGE_SCOPE_LEASE_CONTEXT_KEY not in context


def test_console_binding_rejects_scope_outside_child_dispatch() -> None:
    payload = {
        "meta": {
            "request_context": {USAGE_SCOPE_CONTEXT_KEY: "forged"},
        },
    }

    with pytest.raises(HTTPException) as raised:
        _bind_inherited_usage_scope(payload, agent_id="default")

    assert raised.value.status_code == 403


def test_console_binding_restores_meter_and_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from qwenpaw.app.routers import console

    registry = UsageScopeRegistry()
    meter = _Meter()
    root = registry.open_root(meter, agent_id="default")
    monkeypatch.setattr(console, "lite_usage_scope_registry", registry)
    payload = {
        "meta": {
            "request_context": {
                "_spawn_subagent": True,
                USAGE_SCOPE_CONTEXT_KEY: root.scope_id,
            },
        },
    }

    _bind_inherited_usage_scope(payload, agent_id="default")
    context = payload["meta"]["request_context"]

    assert context["_task_usage_meter"] is meter
    assert context[RECORD_MODEL_USAGE_CONTEXT_KEY] is True
    root.close()
    assert registry.contains(context[USAGE_SCOPE_CONTEXT_KEY])
    _release_bound_usage_scope(payload)
    assert not registry.contains(context[USAGE_SCOPE_CONTEXT_KEY])


@pytest.mark.asyncio
async def test_descendant_model_usage_charges_shared_meter() -> None:
    meter = _Meter()
    wrapper = object.__new__(TokenRecordingModelWrapper)
    wrapper._provider_id = "provider-a"
    usage = SimpleNamespace(
        input_tokens=11,
        output_tokens=7,
        cost_micros=None,
    )
    set_current_usage_scope(
        "scope-1",
        meter,
        record_model_usage=True,
    )
    try:
        await wrapper._record_task_budget(usage)
    finally:
        set_current_usage_scope(None, None, record_model_usage=False)

    assert meter.deltas == [
        UsageDelta(
            input_tokens=11,
            output_tokens=7,
            cost_unknown=True,
        ),
    ]
