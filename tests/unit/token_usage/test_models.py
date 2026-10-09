# -*- coding: utf-8 -*-
"""Contract tests for public token-usage query models."""

from pydantic import ValidationError
import pytest

from qwenpaw.token_usage import TokenUsageRecord as PackageRecord
from qwenpaw.token_usage import TokenUsageSummary as PackageSummary
from qwenpaw.token_usage import TurnModelUsageRoute as PackageTurnRoute
from qwenpaw.token_usage import TurnUsageEvidence as PackageTurnEvidence
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
    assert PackageTurnRoute is models.TurnModelUsageRoute
    assert PackageTurnEvidence is models.TurnUsageEvidence


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
    assert legacy.conversation_id == "chat-a"
    assert legacy.model_dump()["chat_id"] == "chat-a"
    assert "conversation_id" not in legacy.model_dump()
    assert models.TokenUsageByConversation is models.TokenUsageByChat


def test_chat_identity_aliases_must_not_conflict() -> None:
    with pytest.raises(ValidationError, match="must identify one Chat"):
        models.TokenUsageRecord(
            date="2026-10-09",
            model="model-a",
            cost_micros=0,
            cost_unknown_calls=1,
            chat_id="chat-a",
            conversation_id="chat-b",
        )


def test_summary_schema_keeps_usage_coverage_and_scope_fields() -> None:
    schema = models.TokenUsageSummary.model_json_schema()
    properties = schema["properties"]

    assert "usage_observed_calls" in properties
    assert "usage_unobserved_calls" in properties
    assert "scopes" in properties


def test_public_usage_schemas_only_expose_chat_id() -> None:
    for contract in (models.TokenUsageRecord, models.TokenUsageByChat):
        properties = contract.model_json_schema()["properties"]
        assert "chat_id" in properties
        assert "conversation_id" not in properties


def test_turn_usage_evidence_preserves_sparse_compatibility_payload() -> None:
    evidence = models.TurnUsageEvidence(prompt_tokens=3)

    assert evidence.to_payload() == {"prompt_tokens": 3}
    assert "model_routes" in evidence.model_json_schema()["properties"]
