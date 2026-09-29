# -*- coding: utf-8 -*-
"""Local-first, single-user QwenPaw Lite profile."""

from ..kernel.slots import CONTRIBUTION_SLOTS
from .models import EditionProfile

LITE_PROFILE = EditionProfile(
    edition="lite",
    tenant_mode="single_user",
    runner="local",
    ledger="sqlite_wal",
    artifact_store="filesystem",
    approval_mode="strict",
    remote_runner=False,
    distributed_scheduler=False,
    skill_evolution=False,
    plugin_default_restart="hot",
    supported_slots=CONTRIBUTION_SLOTS,
)
