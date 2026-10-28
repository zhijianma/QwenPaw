# -*- coding: utf-8 -*-
"""Cross-platform physical paths for normalized Chat storage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class ChatStoragePaths:
    """Resolve reserved Chat files below one Agent workspace."""

    workspace: Path

    @property
    def root(self) -> Path:
        return self.workspace / ".qwenpaw" / "chat"

    @property
    def catalog(self) -> Path:
        return self.root / "chat_catalog.db"

    @property
    def recall(self) -> Path:
        return self.root / "chat_recall.db"

    @property
    def conversations(self) -> Path:
        return self.root / "conversations"

    def conversation(self, conversation_id: str) -> Path:
        """Return a canonical UUID filename without accepting path syntax."""
        try:
            canonical = str(UUID(conversation_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(
                f"Invalid conversation UUID: {conversation_id!r}",
            ) from exc
        if conversation_id != canonical:
            raise ValueError(
                f"Conversation UUID must be canonical lowercase: "
                f"{conversation_id!r}",
            )
        return self.conversations / f"{canonical}.db"

    def ensure_layout(self) -> None:
        """Create only the reserved directories, never database files."""
        self.conversations.mkdir(parents=True, exist_ok=True)
