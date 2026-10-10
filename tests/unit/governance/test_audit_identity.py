# -*- coding: utf-8 -*-
"""Causal identity tests for legacy governance audit persistence."""

import sqlite3
from pathlib import Path

from qwenpaw.governance.audit import AuditLog
from qwenpaw.governance.policy import (
    GovernanceAction,
    GovernanceDecision,
    ToolCallSpec,
)


def _create_pre_identity_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE audit_events ("
        "ts INTEGER NOT NULL, workspace_dir TEXT NOT NULL, "
        "agent_id TEXT NOT NULL, session_id TEXT NOT NULL, "
        "tool_name TEXT NOT NULL, target TEXT NOT NULL, "
        "decision TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', "
        "extra TEXT NOT NULL DEFAULT '{}')",
    )
    connection.commit()
    connection.close()


def test_audit_migration_preserves_rows_and_adds_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "audit.db"
    _create_pre_identity_database(database)
    audit = AuditLog._create(database)  # pylint: disable=protected-access

    columns = {
        row[1]
        for row in audit._conn.execute(  # pylint: disable=protected-access
            "PRAGMA table_info(audit_events)",
        )
    }

    assert {
        "invocation_id",
        "correlation_id",
        "chat_id",
        "action_id",
    }.issubset(columns)
    audit.close()


def test_audit_round_trips_action_causal_identity(
    tmp_path: Path,
) -> None:
    audit = AuditLog._create(  # pylint: disable=protected-access
        tmp_path / "audit.db",
    )
    spec = ToolCallSpec(
        tool_name="Bash",
        target="git status",
        agent_id="default",
        session_id="chat",
        invocation_id="invocation-1",
        correlation_id="correlation-1",
        chat_id="chat-spec-1",
        action_id="action-1",
    )

    audit.record(
        str(tmp_path),
        spec,
        GovernanceDecision(
            action=GovernanceAction.ALLOW,
            reason="test",
        ),
    )
    events, total = audit.query(
        chat_id="chat-spec-1",
        action_id="action-1",
        limit=10,
    )

    assert total == 1
    assert events[0].invocation_id == "invocation-1"
    assert events[0].correlation_id == "correlation-1"
    assert events[0].chat_id == "chat-spec-1"
    assert events[0].action_id == "action-1"
    audit.close()


def test_tool_call_correlation_defaults_to_invocation() -> None:
    spec = ToolCallSpec(
        tool_name="Read",
        target="README.md",
        agent_id="default",
        session_id="chat",
        invocation_id="invocation-1",
    )

    assert spec.correlation_id == "invocation-1"
