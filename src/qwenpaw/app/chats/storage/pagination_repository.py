# -*- coding: utf-8 -*-
"""Stable, byte-bounded reads from one normalized Chat transcript."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..pagination_cursor import ChatCursorCodec, ChatCursorScope
from .common import connect_database


PAGE_MAX_BYTES = 2 * 1024 * 1024
TURN_PAGE_LIMIT = 30
MESSAGE_PAGE_LIMIT = 100
BLOCK_PAGE_LIMIT = 200
FRAGMENT_MAX_BYTES = 256 * 1024
FRAGMENT_PAYLOAD_BYTES = 180 * 1024
_PAGE_HEADROOM_BYTES = 4096
_PREVIEW_BYTES = 512
_TOOL_BLOCK_INLINE_MAX_BYTES = 32 * 1024
_METADATA_MAX_BYTES = 32 * 1024
_ERROR_DIGEST_MAX_CHARS = 1024
_TERMINAL_TURN_STATES = (
    "completed",
    "failed",
    "canceled",
    "interrupted",
)


class ChatPageNotFoundError(LookupError):
    """The requested Chat or transcript entity does not exist."""


class ChatViewInvalidatedError(RuntimeError):
    """The fixed history head is no longer reachable from the Chat."""

    def __init__(self, current_head_turn_id: str | None) -> None:
        super().__init__("Chat history view was invalidated")
        self.current_head_turn_id = current_head_turn_id


class ChatEntityVersionError(RuntimeError):
    """A mutable block changed after a fragment cursor was issued."""


@dataclass(frozen=True)
class ChatPageView:
    """Stable Chat lineage identity shared by every page level."""

    conversation_id: str
    head_turn_id: str | None
    view_version: str

    def as_dict(self) -> dict[str, str | None]:
        """Return the wire representation of the fixed view."""
        return {
            "conversation_id": self.conversation_id,
            "head_turn_id": self.head_turn_id,
            "view_version": self.view_version,
        }


@dataclass(frozen=True)
class ChatPage:
    """Uniform response envelope for a bounded transcript page."""

    items: tuple[dict[str, Any], ...]
    older_cursor: str | None
    newer_cursor: str | None
    view: ChatPageView
    bytes: int
    truncated_entity: bool

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible response payload."""
        return {
            "items": list(self.items),
            "older_cursor": self.older_cursor,
            "newer_cursor": self.newer_cursor,
            "view": self.view.as_dict(),
            "bytes": self.bytes,
            "truncated_entity": self.truncated_entity,
        }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _scope(
    view: ChatPageView,
    *,
    level: str,
    subject: str,
) -> ChatCursorScope:
    return ChatCursorScope(
        conversation_id=view.conversation_id,
        head_turn_id=view.head_turn_id,
        view_version=view.view_version,
        level=level,
        subject=subject,
    )


def _chat_head(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT head_turn_id FROM chat_meta WHERE singleton_id = 1",
    ).fetchone()
    if row is None:
        raise ChatPageNotFoundError("Chat not found")
    value = row["head_turn_id"]
    return str(value) if value is not None else None


def _turn_reachable(
    connection: sqlite3.Connection,
    *,
    head_turn_id: str | None,
    turn_id: str,
) -> bool:
    if head_turn_id is None:
        return False
    row = connection.execute(
        "WITH RECURSIVE lineage(turn_id, parent_turn_id) AS ("
        "SELECT turn_id, parent_turn_id FROM chat_turns WHERE turn_id = ? "
        "UNION ALL SELECT t.turn_id, t.parent_turn_id FROM chat_turns AS t "
        "JOIN lineage AS l ON t.turn_id = l.parent_turn_id) "
        "SELECT 1 FROM lineage WHERE turn_id = ? LIMIT 1",
        (head_turn_id, turn_id),
    ).fetchone()
    return row is not None


def _stable_history_head(
    connection: sqlite3.Connection,
    *,
    current_head_turn_id: str | None,
) -> str | None:
    if current_head_turn_id is None:
        return None
    placeholders = ", ".join("?" for _ in _TERMINAL_TURN_STATES)
    row = connection.execute(
        "WITH RECURSIVE lineage(turn_id, parent_turn_id, state, depth) AS ("
        "SELECT turn_id, parent_turn_id, state, lineage_depth "
        "FROM chat_turns WHERE turn_id = ? UNION ALL "
        "SELECT t.turn_id, t.parent_turn_id, t.state, t.lineage_depth "
        "FROM chat_turns AS t JOIN lineage AS l "
        "ON t.turn_id = l.parent_turn_id) "
        "SELECT turn_id FROM lineage WHERE state IN ("
        f"{placeholders}) ORDER BY depth DESC LIMIT 1",
        (current_head_turn_id, *_TERMINAL_TURN_STATES),
    ).fetchone()
    return str(row["turn_id"]) if row is not None else None


def _history_view_version(
    connection: sqlite3.Connection,
    *,
    head_turn_id: str | None,
) -> str:
    if head_turn_id is None:
        return hashlib.sha256(b"empty-history").hexdigest()
    row = connection.execute(
        "SELECT write_seq, state, updated_at_us, terminal_at_us "
        "FROM chat_turns WHERE turn_id = ?",
        (head_turn_id,),
    ).fetchone()
    if row is None:
        raise ChatViewInvalidatedError(None)
    payload = _json_bytes(
        {
            "head_turn_id": head_turn_id,
            "state": str(row["state"]),
            "terminal_at_us": row["terminal_at_us"],
            "updated_at_us": int(row["updated_at_us"]),
            "write_seq": int(row["write_seq"]),
        },
    )
    return hashlib.sha256(payload).hexdigest()


def _resolve_view(
    connection: sqlite3.Connection,
    *,
    conversation_id: str,
    head_turn_id: str | None,
    view_version: str | None = None,
) -> ChatPageView:
    meta = connection.execute(
        "SELECT conversation_id FROM chat_meta WHERE singleton_id = 1",
    ).fetchone()
    if meta is None or str(meta["conversation_id"]) != conversation_id:
        raise ChatPageNotFoundError("Chat conversation not found")
    current_head = _chat_head(connection)
    stable_head = _stable_history_head(
        connection,
        current_head_turn_id=current_head,
    )
    fixed_head = stable_head if head_turn_id is None else head_turn_id
    if fixed_head is not None and not _turn_reachable(
        connection,
        head_turn_id=current_head,
        turn_id=fixed_head,
    ):
        raise ChatViewInvalidatedError(stable_head)
    if fixed_head is not None:
        state_row = connection.execute(
            "SELECT state FROM chat_turns WHERE turn_id = ?",
            (fixed_head,),
        ).fetchone()
        if (
            state_row is None
            or str(state_row["state"]) not in _TERMINAL_TURN_STATES
        ):
            raise ChatViewInvalidatedError(stable_head)
    current_version = _history_view_version(
        connection,
        head_turn_id=fixed_head,
    )
    if view_version is not None and view_version != current_version:
        raise ChatViewInvalidatedError(stable_head)
    return ChatPageView(
        conversation_id=conversation_id,
        head_turn_id=fixed_head,
        view_version=current_version,
    )


def read_history_page_view(
    path: Path,
    *,
    conversation_id: str,
) -> ChatPageView:
    """Return the current immutable history identity for one Chat."""
    with connect_database(path, readonly=True) as connection:
        return _resolve_view(
            connection,
            conversation_id=conversation_id,
            head_turn_id=None,
        )


def _finalize_page(
    *,
    items: Sequence[dict[str, Any]],
    older_cursor: str | None,
    newer_cursor: str | None,
    view: ChatPageView,
    truncated_entity: bool,
    max_bytes: int,
) -> ChatPage:
    mutable = list(items)
    while True:
        payload = {
            "items": mutable,
            "older_cursor": older_cursor,
            "newer_cursor": newer_cursor,
            "view": view.as_dict(),
            "bytes": 0,
            "truncated_entity": truncated_entity,
        }
        size = len(_json_bytes(payload))
        payload["bytes"] = size
        adjusted_size = len(_json_bytes(payload))
        if adjusted_size <= max_bytes:
            return ChatPage(
                items=tuple(mutable),
                older_cursor=older_cursor,
                newer_cursor=newer_cursor,
                view=view,
                bytes=adjusted_size,
                truncated_entity=truncated_entity,
            )
        if not mutable:
            raise ValueError("Chat page envelope exceeds its byte budget")
        mutable.pop()
        truncated_entity = True


def _page_cursors(
    *,
    codec: ChatCursorCodec,
    scope: ChatCursorScope,
    minimum: int | None,
    maximum: int | None,
    floor: int,
    ceiling: int,
    position_name: str,
) -> tuple[str | None, str | None]:
    if minimum is None or maximum is None:
        return None, None
    older = None
    newer = None
    if minimum > floor:
        older = codec.encode(
            scope,
            direction="older",
            position={position_name: minimum},
        )
    if maximum < ceiling:
        newer = codec.encode(
            scope,
            direction="newer",
            position={position_name: maximum},
        )
    return older, newer


def _cursor_boundary(
    cursor: str | None,
    *,
    codec: ChatCursorCodec,
    scope: ChatCursorScope,
    position_name: str,
    default_direction: str = "older",
) -> tuple[str, int | None]:
    if cursor is None:
        return default_direction, None
    decoded = codec.decode(cursor, expected_scope=scope)
    value = decoded.position.get(position_name)
    if isinstance(value, bool) or not isinstance(value, int):
        from ..pagination_cursor import InvalidChatCursorError

        raise InvalidChatCursorError("Invalid Chat cursor")
    return decoded.direction, value


def read_turn_page(
    path: Path,
    *,
    conversation_id: str,
    subject: str,
    codec: ChatCursorCodec,
    head_turn_id: str | None = None,
    view_version: str | None = None,
    cursor: str | None = None,
    target_turn_id: str | None = None,
    limit: int = TURN_PAGE_LIMIT,
    max_bytes: int = PAGE_MAX_BYTES,
) -> ChatPage:
    """Read immutable turn shells from one fixed Chat lineage."""
    page_limit = max(1, min(limit, TURN_PAGE_LIMIT))
    with connect_database(path, readonly=True) as connection:
        view = _resolve_view(
            connection,
            conversation_id=conversation_id,
            head_turn_id=head_turn_id,
            view_version=view_version,
        )
        if view.head_turn_id is None:
            return _finalize_page(
                items=(),
                older_cursor=None,
                newer_cursor=None,
                view=view,
                truncated_entity=False,
                max_bytes=max_bytes,
            )
        scope = _scope(view, level="turn", subject=subject)
        if cursor is not None and target_turn_id is not None:
            from ..pagination_cursor import InvalidChatCursorError

            raise InvalidChatCursorError(
                "cursor and target_turn_id are mutually exclusive",
            )
        direction, boundary = _cursor_boundary(
            cursor,
            codec=codec,
            scope=scope,
            position_name="lineage_depth",
        )
        head_row = connection.execute(
            "SELECT lineage_depth FROM chat_turns WHERE turn_id = ?",
            (view.head_turn_id,),
        ).fetchone()
        if head_row is None:
            raise ChatViewInvalidatedError(
                _chat_head(connection),
            )
        ceiling = int(head_row["lineage_depth"])
        if target_turn_id is not None:
            if not _turn_reachable(
                connection,
                head_turn_id=view.head_turn_id,
                turn_id=target_turn_id,
            ):
                raise ChatPageNotFoundError(
                    "Chat turn not found in Chat view",
                )
            target_row = connection.execute(
                "SELECT lineage_depth FROM chat_turns WHERE turn_id = ?",
                (target_turn_id,),
            ).fetchone()
            if target_row is None:
                raise ChatPageNotFoundError("Chat turn not found")
            direction = "older"
            boundary = int(target_row["lineage_depth"]) + 1
        comparison = "<" if direction == "older" else ">"
        order = "DESC" if direction == "older" else "ASC"
        boundary_value = ceiling + 1 if boundary is None else boundary
        rows = connection.execute(
            "WITH RECURSIVE lineage AS ("
            "SELECT turn_id, parent_turn_id, state, lineage_depth, "
            "usage_json, error_digest, created_at_us, updated_at_us, "
            "terminal_at_us FROM chat_turns WHERE turn_id = ? UNION ALL "
            "SELECT t.turn_id, t.parent_turn_id, t.state, "
            "t.lineage_depth, t.usage_json, t.error_digest, "
            "t.created_at_us, t.updated_at_us, t.terminal_at_us "
            "FROM chat_turns AS t JOIN lineage AS l "
            "ON t.turn_id = l.parent_turn_id) "
            "SELECT * FROM lineage WHERE lineage_depth "
            f"{comparison} ? ORDER BY lineage_depth {order} LIMIT ?",
            (view.head_turn_id, boundary_value, page_limit + 1),
        ).fetchall()
        rows = rows[:page_limit]
        items: list[dict[str, Any]] = []
        truncated = False
        remaining = max_bytes - _PAGE_HEADROOM_BYTES
        for row in rows:
            item, item_complete = _turn_item(
                connection,
                row,
                codec=codec,
                view=view,
                subject=subject,
                budget=max(0, remaining),
            )
            item_size = len(_json_bytes(item)) + (1 if items else 0)
            if item_size > remaining:
                truncated = True
                break
            items.append(item)
            remaining -= item_size
            if not item_complete:
                truncated = True
                if item["child_cursor"] is not None:
                    break
        if direction == "older":
            items.reverse()
        depths = [int(item["lineage_depth"]) for item in items]
        older, newer = _page_cursors(
            codec=codec,
            scope=scope,
            minimum=min(depths) if depths else None,
            maximum=max(depths) if depths else None,
            floor=0,
            ceiling=ceiling,
            position_name="lineage_depth",
        )
        return _finalize_page(
            items=items,
            older_cursor=older,
            newer_cursor=newer,
            view=view,
            truncated_entity=truncated,
            max_bytes=max_bytes,
        )


def _verify_turn(
    connection: sqlite3.Connection,
    *,
    view: ChatPageView,
    turn_id: str,
) -> None:
    if not _turn_reachable(
        connection,
        head_turn_id=view.head_turn_id,
        turn_id=turn_id,
    ):
        raise ChatPageNotFoundError("Chat turn not found in Chat view")


def _ordinal_page(
    connection: sqlite3.Connection,
    *,
    table: str,
    parent_column: str,
    parent_id: str,
    selected_columns: str,
    boundary: int | None,
    direction: str,
    limit: int,
) -> list[sqlite3.Row]:
    comparison = "<" if direction == "older" else ">"
    order = "DESC" if direction == "older" else "ASC"
    if boundary is None:
        if direction == "older":
            row = connection.execute(
                f"SELECT MAX(ordinal) AS maximum FROM {table} "
                f"WHERE {parent_column} = ?",
                (parent_id,),
            ).fetchone()
            maximum = row["maximum"]
            boundary = int(maximum) + 1 if maximum is not None else 0
        else:
            boundary = -1
    rows = connection.execute(
        f"SELECT {selected_columns} FROM {table} "
        f"WHERE {parent_column} = ? AND ordinal {comparison} ? "
        f"ORDER BY ordinal {order} LIMIT ?",
        (parent_id, boundary, limit + 1),
    ).fetchall()
    rows = rows[:limit]
    return list(reversed(rows)) if direction == "older" else list(rows)


def _bounded_json_value(raw: str | None) -> tuple[Any, bool]:
    if raw is None:
        return None, True
    encoded = raw.encode("utf-8")
    if len(encoded) > _METADATA_MAX_BYTES:
        return {
            "omitted": True,
            "total_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }, False
    return json.loads(raw), True


def _continuation_cursor(
    *,
    codec: ChatCursorCodec,
    scope: ChatCursorScope,
    position_name: str,
    position: int,
) -> str:
    return codec.encode(
        scope,
        direction="newer",
        position={position_name: position},
    )


def _message_rows(
    connection: sqlite3.Connection,
    *,
    turn_id: str,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT message_id, role, ordinal, state, metadata_json, "
            "created_at_us, updated_at_us FROM chat_messages "
            "WHERE turn_id = ? ORDER BY ordinal LIMIT ?",
            (turn_id, MESSAGE_PAGE_LIMIT + 1),
        ).fetchall(),
    )


def _block_rows(
    connection: sqlite3.Connection,
    *,
    message_id: str,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT block_id, message_id, kind, ordinal, state, "
            "total_bytes, content_sha256, entity_version, metadata_json, "
            "created_at_us, updated_at_us FROM chat_blocks "
            "WHERE message_id = ? ORDER BY ordinal LIMIT ?",
            (message_id, BLOCK_PAGE_LIMIT + 1),
        ).fetchall(),
    )


def _embed_blocks(
    connection: sqlite3.Connection,
    *,
    message_id: str,
    codec: ChatCursorCodec,
    view: ChatPageView,
    subject: str,
    budget: int,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    rows = _block_rows(connection, message_id=message_id)
    has_more = len(rows) > BLOCK_PAGE_LIMIT
    rows = rows[:BLOCK_PAGE_LIMIT]
    items: list[dict[str, Any]] = []
    recursive_complete = True
    remaining = max(0, budget)
    for row in rows:
        item, item_truncated = _block_item(
            connection,
            row,
            content_budget=remaining,
            codec=codec,
            view=view,
            subject=subject,
        )
        item_size = len(_json_bytes(item)) + (1 if items else 0)
        if item_size > remaining:
            has_more = True
            break
        items.append(item)
        remaining -= item_size
        recursive_complete = recursive_complete and not item_truncated
    _mark_loaded_tool_results(items)
    complete = not has_more and len(items) == len(rows)
    complete = complete and recursive_complete
    cursor = None
    if len(items) < len(rows) or has_more:
        boundary = int(items[-1]["ordinal"]) if items else -1
        cursor = _continuation_cursor(
            codec=codec,
            scope=_scope(
                view,
                level=f"block:{message_id}",
                subject=subject,
            ),
            position_name="ordinal",
            position=boundary,
        )
    return items, complete, cursor


def _message_item(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    turn_id: str,
    codec: ChatCursorCodec,
    view: ChatPageView,
    subject: str,
    budget: int,
) -> tuple[dict[str, Any], bool]:
    metadata, metadata_complete = _bounded_json_value(
        str(row["metadata_json"]),
    )
    item: dict[str, Any] = {
        "message_id": str(row["message_id"]),
        "turn_id": turn_id,
        "role": str(row["role"]),
        "ordinal": int(row["ordinal"]),
        "state": str(row["state"]),
        "metadata": metadata,
        "metadata_complete": metadata_complete,
        "created_at_us": int(row["created_at_us"]),
        "updated_at_us": int(row["updated_at_us"]),
        "blocks": [],
        "child_cursor": None,
        "children_complete": False,
    }
    shell_size = len(_json_bytes(item))
    blocks, blocks_complete, child_cursor = _embed_blocks(
        connection,
        message_id=str(row["message_id"]),
        codec=codec,
        view=view,
        subject=subject,
        budget=max(0, budget - shell_size),
    )
    item["blocks"] = blocks
    item["child_cursor"] = child_cursor
    item["children_complete"] = blocks_complete
    return item, metadata_complete and blocks_complete


def _embed_messages(
    connection: sqlite3.Connection,
    *,
    turn_id: str,
    codec: ChatCursorCodec,
    view: ChatPageView,
    subject: str,
    budget: int,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    rows = _message_rows(connection, turn_id=turn_id)
    has_more = len(rows) > MESSAGE_PAGE_LIMIT
    rows = rows[:MESSAGE_PAGE_LIMIT]
    items: list[dict[str, Any]] = []
    recursive_complete = True
    remaining = max(0, budget)
    for row in rows:
        item, item_complete = _message_item(
            connection,
            row,
            turn_id=turn_id,
            codec=codec,
            view=view,
            subject=subject,
            budget=remaining,
        )
        item_size = len(_json_bytes(item)) + (1 if items else 0)
        if item_size > remaining:
            has_more = True
            break
        items.append(item)
        remaining -= item_size
        recursive_complete = recursive_complete and item_complete
        if not item_complete and item["child_cursor"] is not None:
            break
    complete = not has_more and len(items) == len(rows)
    complete = complete and recursive_complete
    cursor = None
    if len(items) < len(rows) or has_more:
        boundary = int(items[-1]["ordinal"]) if items else -1
        cursor = _continuation_cursor(
            codec=codec,
            scope=_scope(
                view,
                level=f"message:{turn_id}",
                subject=subject,
            ),
            position_name="ordinal",
            position=boundary,
        )
    turn_blocks = [
        block
        for message in items
        for block in message.get("blocks", [])
        if isinstance(block, dict)
    ]
    _mark_loaded_tool_results(turn_blocks)
    return items, complete, cursor


def _turn_item(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    codec: ChatCursorCodec,
    view: ChatPageView,
    subject: str,
    budget: int,
) -> tuple[dict[str, Any], bool]:
    usage, usage_complete = _bounded_json_value(
        str(row["usage_json"]) if row["usage_json"] is not None else None,
    )
    error_digest = row["error_digest"]
    if isinstance(error_digest, str):
        error_digest = error_digest[:_ERROR_DIGEST_MAX_CHARS]
    turn_id = str(row["turn_id"])
    item: dict[str, Any] = {
        "turn_id": turn_id,
        "parent_turn_id": (
            str(row["parent_turn_id"])
            if row["parent_turn_id"] is not None
            else None
        ),
        "state": str(row["state"]),
        "lineage_depth": int(row["lineage_depth"]),
        "usage": usage,
        "usage_complete": usage_complete,
        "error_digest": error_digest,
        "created_at_us": int(row["created_at_us"]),
        "updated_at_us": int(row["updated_at_us"]),
        "terminal_at_us": row["terminal_at_us"],
        "messages": [],
        "child_cursor": None,
        "children_complete": False,
    }
    shell_size = len(_json_bytes(item))
    messages, messages_complete, child_cursor = _embed_messages(
        connection,
        turn_id=turn_id,
        codec=codec,
        view=view,
        subject=subject,
        budget=max(0, budget - shell_size),
    )
    item["messages"] = messages
    item["child_cursor"] = child_cursor
    item["children_complete"] = messages_complete
    return item, usage_complete and messages_complete


def read_message_page(
    path: Path,
    *,
    conversation_id: str,
    turn_id: str,
    subject: str,
    codec: ChatCursorCodec,
    head_turn_id: str | None,
    view_version: str | None = None,
    cursor: str | None = None,
    limit: int = MESSAGE_PAGE_LIMIT,
    max_bytes: int = PAGE_MAX_BYTES,
) -> ChatPage:
    """Read message shells after proving turn reachability."""
    page_limit = max(1, min(limit, MESSAGE_PAGE_LIMIT))
    with connect_database(path, readonly=True) as connection:
        view = _resolve_view(
            connection,
            conversation_id=conversation_id,
            head_turn_id=head_turn_id,
            view_version=view_version,
        )
        _verify_turn(connection, view=view, turn_id=turn_id)
        scope = _scope(
            view,
            level=f"message:{turn_id}",
            subject=subject,
        )
        direction, boundary = _cursor_boundary(
            cursor,
            codec=codec,
            scope=scope,
            position_name="ordinal",
            default_direction="newer",
        )
        rows = _ordinal_page(
            connection,
            table="chat_messages",
            parent_column="turn_id",
            parent_id=turn_id,
            selected_columns=(
                "message_id, role, ordinal, state, metadata_json, "
                "created_at_us, updated_at_us"
            ),
            boundary=boundary,
            direction=direction,
            limit=page_limit,
        )
        items: list[dict[str, Any]] = []
        truncated = False
        remaining = max_bytes - _PAGE_HEADROOM_BYTES
        for row in rows:
            item, item_complete = _message_item(
                connection,
                row,
                turn_id=turn_id,
                codec=codec,
                view=view,
                subject=subject,
                budget=max(0, remaining),
            )
            item_size = len(_json_bytes(item)) + (1 if items else 0)
            if item_size > remaining:
                truncated = True
                break
            items.append(item)
            remaining -= item_size
            if not item_complete:
                truncated = True
                break
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()[0],
        )
        ordinals = [int(item["ordinal"]) for item in items]
        older, newer = _page_cursors(
            codec=codec,
            scope=scope,
            minimum=min(ordinals) if ordinals else None,
            maximum=max(ordinals) if ordinals else None,
            floor=0,
            ceiling=max(0, count - 1),
            position_name="ordinal",
        )
        return _finalize_page(
            items=items,
            older_cursor=older,
            newer_cursor=newer,
            view=view,
            truncated_entity=truncated,
            max_bytes=max_bytes,
        )


def _message_turn(
    connection: sqlite3.Connection,
    *,
    message_id: str,
) -> str:
    row = connection.execute(
        "SELECT turn_id FROM chat_messages WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        raise ChatPageNotFoundError("Chat message not found")
    return str(row["turn_id"])


def _block_payload(
    connection: sqlite3.Connection,
    *,
    block_id: str,
) -> bytes:
    rows = connection.execute(
        "SELECT payload FROM chat_block_segments WHERE block_id = ? "
        "ORDER BY segment_ordinal",
        (block_id,),
    ).fetchall()
    return b"".join(bytes(row["payload"]) for row in rows)


def _block_item(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    content_budget: int,
    codec: ChatCursorCodec,
    view: ChatPageView,
    subject: str,
) -> tuple[dict[str, Any], bool]:
    block_id = str(row["block_id"])
    total_bytes = int(row["total_bytes"])
    metadata, metadata_complete = _bounded_json_value(
        str(row["metadata_json"]),
    )
    item: dict[str, Any] = {
        "block_id": block_id,
        "message_id": str(row["message_id"]),
        "kind": str(row["kind"]),
        "ordinal": int(row["ordinal"]),
        "state": str(row["state"]),
        "total_bytes": total_bytes,
        "content_sha256": row["content_sha256"],
        "entity_version": int(row["entity_version"]),
        "metadata": metadata,
        "metadata_complete": metadata_complete,
        "created_at_us": int(row["created_at_us"]),
        "updated_at_us": int(row["updated_at_us"]),
        "content": None,
        "content_loaded": False,
        "content_cursor": None,
    }
    tool_row = connection.execute(
        "SELECT provider_call_id, call_block_id, result_block_id, state, "
        "tool_name FROM chat_tool_links WHERE call_block_id = ? "
        "OR result_block_id = ? LIMIT 1",
        (block_id, block_id),
    ).fetchone()
    if tool_row is not None:
        is_call = str(tool_row["call_block_id"]) == block_id
        result_block_id = tool_row["result_block_id"]
        item["tool_link"] = {
            "provider_call_id": str(tool_row["provider_call_id"]),
            "tool_name": tool_row["tool_name"],
            "state": str(tool_row["state"]),
            "call_ref": str(tool_row["call_block_id"]),
            "result_ref": (
                str(result_block_id) if result_block_id is not None else None
            ),
            "result_load_state": (
                "not_loaded"
                if is_call and result_block_id is not None
                else "pending"
                if is_call and str(tool_row["state"]) == "running"
                else "unavailable"
                if is_call
                else None
            ),
        }
        # Tool calls and results are collapsed by default in the Console.
        # Keep their potentially large payloads off the first-paint path;
        # the existing fragment cursor still exposes the complete content.
        content_budget = min(content_budget, _TOOL_BLOCK_INLINE_MAX_BYTES)
    content_scope = _scope(
        view,
        level=f"fragment:{block_id}",
        subject=subject,
    )
    item["content_cursor"] = codec.encode(
        content_scope,
        direction="newer",
        position={
            "entity_version": int(row["entity_version"]),
            "offset": 0,
        },
    )
    preview_row = connection.execute(
        "SELECT substr(payload, 1, ?) AS preview "
        "FROM chat_block_segments WHERE block_id = ? "
        "ORDER BY segment_ordinal LIMIT 1",
        (_PREVIEW_BYTES, block_id),
    ).fetchone()
    if preview_row is not None and preview_row["preview"] is not None:
        item["preview"] = bytes(preview_row["preview"]).decode(
            "utf-8",
            errors="replace",
        )
    if total_bytes > content_budget:
        return item, True
    payload = _block_payload(connection, block_id=block_id)
    try:
        item["content"] = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        item["content"] = payload.decode("utf-8", errors="replace")
    item["content_loaded"] = True
    item["content_cursor"] = None
    if len(_json_bytes(item)) > content_budget:
        item["content"] = None
        item["content_loaded"] = False
        item["content_cursor"] = codec.encode(
            content_scope,
            direction="newer",
            position={
                "entity_version": int(row["entity_version"]),
                "offset": 0,
            },
        )
        return item, True
    return item, not metadata_complete


def _mark_loaded_tool_results(items: Sequence[dict[str, Any]]) -> None:
    loaded = {str(item["block_id"]) for item in items}
    for item in items:
        tool_link = item.get("tool_link")
        if not isinstance(tool_link, dict):
            continue
        result_ref = tool_link.get("result_ref")
        if result_ref is not None and str(result_ref) in loaded:
            tool_link["result_load_state"] = "loaded"


def read_block_page(
    path: Path,
    *,
    conversation_id: str,
    message_id: str,
    subject: str,
    codec: ChatCursorCodec,
    head_turn_id: str | None,
    view_version: str | None = None,
    cursor: str | None = None,
    limit: int = BLOCK_PAGE_LIMIT,
    max_bytes: int = PAGE_MAX_BYTES,
) -> ChatPage:
    """Read blocks with inline content only while the byte budget allows."""
    page_limit = max(1, min(limit, BLOCK_PAGE_LIMIT))
    with connect_database(path, readonly=True) as connection:
        view = _resolve_view(
            connection,
            conversation_id=conversation_id,
            head_turn_id=head_turn_id,
            view_version=view_version,
        )
        turn_id = _message_turn(connection, message_id=message_id)
        _verify_turn(connection, view=view, turn_id=turn_id)
        scope = _scope(
            view,
            level=f"block:{message_id}",
            subject=subject,
        )
        direction, boundary = _cursor_boundary(
            cursor,
            codec=codec,
            scope=scope,
            position_name="ordinal",
            default_direction="newer",
        )
        rows = _ordinal_page(
            connection,
            table="chat_blocks",
            parent_column="message_id",
            parent_id=message_id,
            selected_columns=(
                "block_id, message_id, kind, ordinal, state, total_bytes, "
                "content_sha256, entity_version, metadata_json, "
                "created_at_us, updated_at_us"
            ),
            boundary=boundary,
            direction=direction,
            limit=page_limit,
        )
        values: list[dict[str, Any]] = []
        truncated = False
        remaining = max_bytes - _PAGE_HEADROOM_BYTES
        for row in rows:
            item, item_truncated = _block_item(
                connection,
                row,
                content_budget=max(0, remaining),
                codec=codec,
                view=view,
                subject=subject,
            )
            item_size = len(_json_bytes(item)) + (1 if values else 0)
            if item_size > remaining:
                truncated = True
                break
            values.append(item)
            remaining -= item_size
            truncated = truncated or item_truncated
            if item_truncated and item["content_cursor"] is None:
                break
        _mark_loaded_tool_results(values)
        count = int(
            connection.execute(
                "SELECT COUNT(*) FROM chat_blocks WHERE message_id = ?",
                (message_id,),
            ).fetchone()[0],
        )
        ordinals = [int(item["ordinal"]) for item in values]
        older, newer = _page_cursors(
            codec=codec,
            scope=scope,
            minimum=min(ordinals) if ordinals else None,
            maximum=max(ordinals) if ordinals else None,
            floor=0,
            ceiling=max(0, count - 1),
            position_name="ordinal",
        )
        return _finalize_page(
            items=values,
            older_cursor=older,
            newer_cursor=newer,
            view=view,
            truncated_entity=truncated,
            max_bytes=max_bytes,
        )


def _block_owner(
    connection: sqlite3.Connection,
    *,
    block_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT b.message_id, b.entity_version, b.total_bytes, "
        "b.content_sha256, b.state, m.turn_id FROM chat_blocks AS b "
        "JOIN chat_messages AS m ON m.message_id = b.message_id "
        "WHERE b.block_id = ?",
        (block_id,),
    ).fetchone()
    if row is None:
        raise ChatPageNotFoundError("Chat block not found")
    return row


def _read_fragment_bytes(
    connection: sqlite3.Connection,
    *,
    block_id: str,
    start: int,
    end: int,
) -> bytes:
    rows = connection.execute(
        "SELECT start_byte, end_byte, payload "
        "FROM chat_block_segments WHERE block_id = ? "
        "AND end_byte > ? AND start_byte < ? ORDER BY segment_ordinal",
        (block_id, start, end),
    ).fetchall()
    parts: list[bytes] = []
    for row in rows:
        segment_start = int(row["start_byte"])
        payload = bytes(row["payload"])
        left = max(0, start - segment_start)
        right = min(len(payload), end - segment_start)
        parts.append(payload[left:right])
    return b"".join(parts)


def read_block_fragment(
    path: Path,
    *,
    conversation_id: str,
    block_id: str,
    subject: str,
    codec: ChatCursorCodec,
    head_turn_id: str | None,
    view_version: str | None = None,
    cursor: str | None = None,
    fragment_bytes: int = FRAGMENT_PAYLOAD_BYTES,
) -> ChatPage:
    """Read one byte-addressed block fragment with version fencing."""
    size_limit = max(1, min(fragment_bytes, FRAGMENT_PAYLOAD_BYTES))
    with connect_database(path, readonly=True) as connection:
        view = _resolve_view(
            connection,
            conversation_id=conversation_id,
            head_turn_id=head_turn_id,
            view_version=view_version,
        )
        owner = _block_owner(connection, block_id=block_id)
        _verify_turn(
            connection,
            view=view,
            turn_id=str(owner["turn_id"]),
        )
        version = int(owner["entity_version"])
        scope = _scope(
            view,
            level=f"fragment:{block_id}",
            subject=subject,
        )
        start = 0
        if cursor is not None:
            decoded = codec.decode(cursor, expected_scope=scope)
            if decoded.direction != "newer":
                from ..pagination_cursor import InvalidChatCursorError

                raise InvalidChatCursorError("Invalid Chat cursor")
            offset = decoded.position.get("offset")
            cursor_version = decoded.position.get("entity_version")
            if isinstance(offset, bool) or not isinstance(offset, int):
                from ..pagination_cursor import InvalidChatCursorError

                raise InvalidChatCursorError("Invalid Chat cursor")
            if cursor_version != version:
                raise ChatEntityVersionError("Chat block version changed")
            start = offset
        total_bytes = int(owner["total_bytes"])
        if start < 0 or start > total_bytes:
            from ..pagination_cursor import InvalidChatCursorError

            raise InvalidChatCursorError("Invalid Chat cursor")
        end = min(total_bytes, start + size_limit)
        payload = _read_fragment_bytes(
            connection,
            block_id=block_id,
            start=start,
            end=end,
        )
        if len(payload) != end - start:
            raise RuntimeError("Chat block fragment is incomplete")
        newer = None
        if end < total_bytes:
            newer = codec.encode(
                scope,
                direction="newer",
                position={
                    "entity_version": version,
                    "offset": end,
                },
            )
        item = {
            "block_id": block_id,
            "entity_version": version,
            "start_byte": start,
            "end_byte": end,
            "total_bytes": total_bytes,
            "payload_base64": base64.b64encode(payload).decode("ascii"),
            "payload_encoding": "base64",
            "fragment_sha256": hashlib.sha256(payload).hexdigest(),
            "content_sha256": (
                owner["content_sha256"]
                if str(owner["state"]) != "partial"
                else None
            ),
        }
        return _finalize_page(
            items=(item,),
            older_cursor=None,
            newer_cursor=newer,
            view=view,
            truncated_entity=end < total_bytes,
            max_bytes=FRAGMENT_MAX_BYTES,
        )
