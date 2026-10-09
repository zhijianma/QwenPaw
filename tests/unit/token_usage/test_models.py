# -*- coding: utf-8 -*-
"""Contract tests for public token-usage query models."""

from qwenpaw.token_usage import TokenUsageRecord as PackageRecord
from qwenpaw.token_usage import TokenUsageSummary as PackageSummary
from qwenpaw.token_usage.manager import TokenUsageRecord as LegacyRecord
from qwenpaw.token_usage.manager import TokenUsageSummary as LegacySummary
from qwenpaw.token_usage.models import TokenUsageRecord, TokenUsageSummary


def test_query_models_keep_public_and_legacy_import_identity() -> None:
    """Extensions can migrate imports without changing runtime types."""
    assert PackageRecord is TokenUsageRecord
    assert LegacyRecord is TokenUsageRecord
    assert PackageSummary is TokenUsageSummary
    assert LegacySummary is TokenUsageSummary


def test_summary_schema_keeps_usage_coverage_and_scope_fields() -> None:
    schema = TokenUsageSummary.model_json_schema()
    properties = schema["properties"]

    assert "usage_observed_calls" in properties
    assert "usage_unobserved_calls" in properties
    assert "scopes" in properties
