# -*- coding: utf-8 -*-
"""Resolve Host-authored approval deadlines for durable Task runs."""

from __future__ import annotations

from typing import Any


def approval_timeout_seconds(
    request_context: dict[str, Any],
    *,
    default: float,
) -> float:
    """Return a trusted Task contract deadline or the process default."""
    if (
        request_context.get("durable_task") is not True
        or request_context.get("_task_approval_broker") is None
    ):
        return default
    contract = request_context.get("execution_contract")
    if not isinstance(contract, dict):
        return default
    timeout_policy = contract.get("timeout_policy")
    if not isinstance(timeout_policy, dict):
        return default
    value = timeout_policy.get("approval_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value <= 0:
        return default
    return float(value)


__all__ = ["approval_timeout_seconds"]
