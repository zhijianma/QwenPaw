# -*- coding: utf-8 -*-
"""Immutable product edition configuration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class EditionProfile(BaseModel):
    """Composition choices that must not fork kernel contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    edition: Literal["lite", "workstation", "hub"]
    tenant_mode: Literal["single_user", "multi_user"]
    runner: Literal["local", "local_pool", "distributed"]
    ledger: Literal["sqlite_wal", "database"]
    artifact_store: Literal["filesystem", "object_store"]
    approval_mode: Literal["strict", "policy"]
    remote_runner: bool
    distributed_scheduler: bool
    skill_evolution: bool
    plugin_default_restart: Literal["hot", "scoped", "full"]
    supported_slots: frozenset[str]
