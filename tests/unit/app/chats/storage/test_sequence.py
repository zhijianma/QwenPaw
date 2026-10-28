# -*- coding: utf-8 -*-
"""Workspace recall sequence allocation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from qwenpaw.app.chats.storage import (
    ChatStoragePaths,
    reserve_recall_sequences,
)
from qwenpaw.app.chats.storage.common import connect_database


def test_sequence_leases_only_advance_and_honor_legacy_floor(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)
    first = reserve_recall_sequences(
        paths.catalog,
        workspace_id="workspace",
        count=3,
        high_water_floor=100,
        lease_owner="first",
        now_us=1,
    )
    second = reserve_recall_sequences(
        paths.catalog,
        workspace_id="workspace",
        count=2,
        high_water_floor=50,
        lease_owner="second",
        now_us=2,
    )

    assert first is not None
    assert list(first.values()) == [101, 102, 103]
    assert second is not None
    assert list(second.values()) == [104, 105]
    with connect_database(paths.catalog, readonly=True) as connection:
        row = connection.execute(
            "SELECT high_water, lease_owner, lease_end "
            "FROM chat_recall_sequences",
        ).fetchone()
        assert tuple(row) == (105, "second", 105)


def test_empty_sequence_reservation_advances_floor_without_reuse(
    tmp_path: Path,
) -> None:
    paths = ChatStoragePaths(tmp_path)

    assert (
        reserve_recall_sequences(
            paths.catalog,
            workspace_id="workspace",
            count=0,
            high_water_floor=7,
            lease_owner="migration",
            now_us=1,
        )
        is None
    )
    lease = reserve_recall_sequences(
        paths.catalog,
        workspace_id="workspace",
        count=1,
        lease_owner="runtime",
        now_us=2,
    )
    assert lease is not None
    assert list(lease.values()) == [8]


@pytest.mark.parametrize("count,floor", [(-1, 0), (0, -1)])
def test_sequence_reservation_rejects_negative_inputs(
    tmp_path: Path,
    count: int,
    floor: int,
) -> None:
    paths = ChatStoragePaths(tmp_path)

    with pytest.raises(ValueError):
        reserve_recall_sequences(
            paths.catalog,
            workspace_id="workspace",
            count=count,
            high_water_floor=floor,
            lease_owner="invalid",
            now_us=1,
        )
