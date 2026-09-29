# -*- coding: utf-8 -*-
"""Multi-tenant QwenPaw Hub composition contract."""

from ..kernel.slots import CONTRIBUTION_SLOTS
from .models import EditionProfile

HUB_PROFILE = EditionProfile(
    edition="hub",
    tenant_mode="multi_user",
    runner="distributed",
    ledger="database",
    artifact_store="object_store",
    approval_mode="policy",
    remote_runner=True,
    distributed_scheduler=True,
    skill_evolution=True,
    plugin_default_restart="scoped",
    supported_slots=CONTRIBUTION_SLOTS,
)
