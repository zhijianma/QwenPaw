# -*- coding: utf-8 -*-
"""Tests for the application-owned Task runtime composition root."""

import gc
from types import SimpleNamespace
from weakref import ref

import pytest

from qwenpaw.app.task_runtime import (
    TaskApplicationHost,
    canonical_task_capability_id,
    task_application_host,
)
from qwenpaw.capabilities import GenerationRegistry
from qwenpaw.kernel.models import (
    CapabilityBundle,
    CapabilityContribution,
    CapabilityProviderKind,
    TaskSource,
)
from qwenpaw.tasks.application import CreateTaskCommand


def test_task_capability_migration_only_rewrites_known_ids() -> None:
    assert (
        canonical_task_capability_id(
            "qwenpaw.system.console-agent",
        )
        == "qwenpaw.system.tasks.console-agent"
    )
    assert (
        canonical_task_capability_id(
            "qwenpaw.system.unrelated",
        )
        == "qwenpaw.system.unrelated"
    )
    assert (
        canonical_task_capability_id(
            "vendor.runner",
        )
        == "vendor.runner"
    )


@pytest.mark.asyncio
async def test_host_composes_one_coherent_application_surface(
    tmp_path,
) -> None:
    registry = GenerationRegistry()
    workspace = SimpleNamespace(
        agent_id="default",
        workspace_dir=tmp_path,
    )
    host = TaskApplicationHost(registry)

    first = await host.compose(workspace)
    created = await first.tasks.create(
        CreateTaskCommand(
            objective="Verify the Task application host",
            source=TaskSource.USER,
        ),
    )
    await registry.ensure_bundle(
        CapabilityBundle(
            provider_id="tests.host",
            provider_kind=CapabilityProviderKind.PLUGIN,
            version="1.0.0",
            contributions=(
                CapabilityContribution(
                    contribution_id="probe",
                    slot="test.probe",
                    entrypoint="tests:probe",
                ),
            ),
        ),
        lambda _declaration: object(),
    )
    second = await host.compose(workspace)
    detail = await second.tasks.detail(created.task_id)
    events = await second.events.page(created.task_id)

    assert first.workspace is workspace
    assert first.runtime.capability_resolver is registry
    assert first.runtime.task_service is second.runtime.task_service
    assert first.orchestrator is second.orchestrator
    assert first.execution is second.execution
    assert first.capabilities is second.capabilities
    assert first.side_effects is second.side_effects
    assert detail.task == created
    assert events.items[0].event_type == "task.created"
    refreshed = await second.tasks.create(
        CreateTaskCommand(
            objective="Use the refreshed generation",
            source=TaskSource.USER,
        ),
    )
    refreshed_events = await second.events.page(refreshed.task_id)
    assert refreshed_events.items[0].registry_generation == (
        registry.generation
    )
    assert host.supervisor is not None


def test_registry_selects_one_process_local_host() -> None:
    registry = GenerationRegistry()

    first = task_application_host(registry)
    second = task_application_host(registry)

    assert first is second
    assert first.supervisor is second.supervisor


def test_registry_host_cache_does_not_retain_retired_catalogs() -> None:
    registry = GenerationRegistry()
    host = task_application_host(registry)
    registry_ref = ref(registry)
    host_ref = ref(host)

    del host
    del registry
    gc.collect()

    assert host_ref() is None
    assert registry_ref() is None
