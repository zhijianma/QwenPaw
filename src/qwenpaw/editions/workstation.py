# -*- coding: utf-8 -*-
"""Professional local-workstation QwenPaw composition contract."""

from ..kernel.slots import CONTRIBUTION_SLOTS
from .models import EditionProfile

WORKSTATION_PROFILE = EditionProfile(
    edition="workstation",
    tenant_mode="single_user",
    runner="local_pool",
    ledger="sqlite_wal",
    artifact_store="filesystem",
    approval_mode="policy",
    remote_runner=True,
    distributed_scheduler=False,
    skill_evolution=True,
    plugin_default_restart="hot",
    supported_slots=CONTRIBUTION_SLOTS,
)
