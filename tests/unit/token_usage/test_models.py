# -*- coding: utf-8 -*-
"""Contract tests for public token-usage query models."""

from qwenpaw.token_usage import TokenUsageRecord as PackageRecord
from qwenpaw.token_usage import TokenUsageSummary as PackageSummary
from qwenpaw.token_usage import manager as legacy_manager
from qwenpaw.token_usage import models


def test_query_models_keep_public_and_legacy_import_identity() -> None:
    """Extensions can migrate imports without changing runtime types."""
    names = (
        "TokenUsageByAgent",
        "TokenUsageByChat",
        "TokenUsageByConversation",
        "TokenUsageByDateModel",
        "TokenUsageByModel",
        "TokenUsageByTurn",
        "TokenUsageRecord",
        "TokenUsageScopeRows",
        "TokenUsageStats",
        "TokenUsageSummary",
    )
    for name in names:
        assert getattr(legacy_manager, name) is getattr(models, name)
    assert PackageRecord is models.TokenUsageRecord
    assert PackageSummary is models.TokenUsageSummary


def test_chat_id_is_canonical_with_consistent_legacy_alias() -> None:
    canonical = models.TokenUsageRecord(
        date="2026-10-09",
        model="model-a",
        cost_micros=0,
        cost_unknown_calls=1,
        chat_id="chat-a",
    )
    legacy = models.TokenUsageRecord(
        date="2026-10-09",
        model="model-a",
        cost_micros=0,
        cost_unknown_calls=1,
        conversation_id="chat-a",
    )

    assert canonical.chat_id == "chat-a"
    assert canonical.conversation_id == "chat-a"
    assert legacy.chat_id == "chat-a"
    assert models.TokenUsageByConversation is models.TokenUsageByChat


def test_summary_schema_keeps_usage_coverage_and_scope_fields() -> None:
    schema = models.TokenUsageSummary.model_json_schema()
    properties = schema["properties"]

    assert "usage_observed_calls" in properties
    assert "usage_unobserved_calls" in properties
    assert "scopes" in properties
