# -*- coding: utf-8 -*-
"""Generation-pinned command catalogs and deterministic dispatch."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..kernel.models import (
    CommandDefinition,
    CommandDisposition,
    CommandMessage,
    CommandRequest,
    CommandResult,
)

_COMMAND_ID_PART_RE = re.compile(r"[^a-z0-9_.-]+")


def parse_command_request(raw_text: str) -> CommandRequest | None:
    """Parse a leading slash command without interpreting its arguments."""
    text = raw_text.lstrip()
    if not text.startswith("/") or len(text) == 1:
        return None
    body = text[1:]
    name, _, arguments = body.partition(" ")
    if not name:
        return None
    return CommandRequest(
        raw_text=raw_text,
        command_name=name.casefold(),
        arguments=arguments.lstrip(),
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content or "Command completed."
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                value = block.get("text")
            else:
                value = getattr(block, "text", None)
            if isinstance(value, str) and value:
                parts.append(value)
    return "\n\n".join(parts) or "Command completed."


def _to_result(handled: bool, message: Any) -> CommandResult:
    if not handled:
        return CommandResult(disposition=CommandDisposition.NOT_HANDLED)
    if message is None:
        return CommandResult(disposition=CommandDisposition.CONTINUE)
    return CommandResult(
        disposition=CommandDisposition.RESPOND,
        message=CommandMessage(text=_message_text(message)),
    )


@dataclass(frozen=True)
class WorkspaceCommandHost:
    """Fixed compatibility view of one Workspace slash-command registry."""

    registry: Any
    context: Any
    provider_id: str

    @classmethod
    def capture(
        cls,
        context: Any,
        provider_id: str,
    ) -> "WorkspaceCommandHost":
        """Capture the current registry before any command is dispatched."""
        source = context.workspace.plugins.slash_command_registry
        snapshot = getattr(source, "snapshot", None)
        registry = snapshot() if callable(snapshot) else source
        return cls(registry, context, provider_id)

    def list_commands(self) -> Sequence[CommandDefinition]:
        """Translate legacy specs into a provider-owned static catalog."""
        definitions: list[CommandDefinition] = []
        used_ids: set[str] = set()
        command_specs = getattr(self.registry, "command_specs", None)
        specs = command_specs() if callable(command_specs) else ()
        for index, spec in enumerate(specs):
            slug = (
                _COMMAND_ID_PART_RE.sub(
                    "-",
                    spec.name.casefold(),
                ).strip(".-")
                or f"command-{index}"
            )
            command_id = f"{self.provider_id}.{slug}"
            if command_id in used_ids:
                command_id = f"{command_id}-{index}"
            used_ids.add(command_id)
            definitions.append(
                CommandDefinition(
                    command_id=command_id,
                    provider_id=self.provider_id,
                    name=spec.name,
                    aliases=spec.aliases,
                    category=spec.category,
                    help_text=spec.help_text,
                    protected=spec.protected,
                ),
            )
        return tuple(definitions)

    async def dispatch(self, request: CommandRequest) -> CommandResult:
        """Dispatch a catalog command using the captured registry."""
        handled, response = await self._dispatch(request.raw_text)
        return _to_result(handled, response)

    async def fallback(self, request: CommandRequest) -> CommandResult:
        """Try the captured dynamic Skill fallback."""
        handled, response = await self._dispatch(request.raw_text)
        return _to_result(handled, response)

    async def _dispatch(self, raw_text: str) -> tuple[bool, Any]:
        """Use detailed dispatch when available, preserving legacy callers."""
        detailed = getattr(self.registry, "dispatch_detailed", None)
        if callable(detailed):
            return await detailed(raw_text, self.context)
        dispatch = getattr(self.registry, "dispatch", None)
        if not callable(dispatch):
            return False, None
        response = await dispatch(raw_text, self.context)
        return response is not None, response


class CommandRouterSession:
    """Merge provider catalogs without install-order shadowing."""

    def __init__(self, sessions: Sequence[Any]) -> None:
        self._sessions = tuple(
            sorted(sessions, key=lambda item: item.provider_id),
        )
        self._commands: dict[str, tuple[Any, CommandDefinition]] = {}
        self._ambiguous: set[str] = set()
        self._fallback = None
        self._build_catalog()

    def _build_catalog(self) -> None:
        command_ids: set[str] = set()
        for session in self._sessions:
            provider_id = session.provider_id
            if session.allows_dynamic_fallback:
                if not provider_id.startswith("qwenpaw.system."):
                    raise ValueError(
                        "dynamic command fallback is reserved for system "
                        "providers",
                    )
                if self._fallback is not None:
                    raise ValueError(
                        "only one dynamic command fallback is allowed",
                    )
                self._fallback = session
            for definition in session.list_commands():
                if definition.provider_id != provider_id:
                    raise ValueError(
                        f"command '{definition.command_id}' has mismatched "
                        "provider ownership",
                    )
                if definition.command_id in command_ids:
                    raise ValueError(
                        f"duplicate command id '{definition.command_id}'",
                    )
                command_ids.add(definition.command_id)
                if definition.protected and not provider_id.startswith(
                    "qwenpaw.system.",
                ):
                    raise ValueError(
                        "only system command providers may protect names",
                    )
                for name in (definition.name, *definition.aliases):
                    self._add_name(name.casefold(), session, definition)

    def _add_name(
        self,
        name: str,
        session: Any,
        definition: CommandDefinition,
    ) -> None:
        if name in self._ambiguous:
            if definition.protected:
                self._ambiguous.remove(name)
                self._commands[name] = (session, definition)
            return
        existing = self._commands.get(name)
        if existing is None:
            self._commands[name] = (session, definition)
            return
        _, existing_definition = existing
        if existing_definition.protected and not definition.protected:
            return
        if definition.protected and not existing_definition.protected:
            self._commands[name] = (session, definition)
            return
        if definition.command_id == existing_definition.command_id:
            return
        if definition.protected:
            raise ValueError(f"duplicate protected command name '/{name}'")
        self._commands.pop(name)
        self._ambiguous.add(name)

    def list_commands(self) -> tuple[CommandDefinition, ...]:
        """Return unique, dispatchable definitions in stable order."""
        definitions = {
            definition.command_id: definition
            for _, definition in self._commands.values()
        }
        return tuple(definitions[key] for key in sorted(definitions))

    async def dispatch(self, raw_text: str) -> CommandResult:
        """Dispatch one command or return an explicit not-handled result."""
        request = parse_command_request(raw_text)
        if request is None:
            return CommandResult(disposition=CommandDisposition.NOT_HANDLED)
        if request.command_name in self._ambiguous:
            return CommandResult(
                disposition=CommandDisposition.RESPOND,
                message=CommandMessage(
                    text=(
                        f"Command '/{request.command_name}' is ambiguous. "
                        "Disable one of the conflicting providers."
                    ),
                ),
            )
        target = self._commands.get(request.command_name)
        if target is not None:
            session, _ = target
            return await session.dispatch(request)
        if self._fallback is not None:
            return await self._fallback.fallback(request)
        return CommandResult(disposition=CommandDisposition.NOT_HANDLED)

    async def close(self) -> None:
        """Close every provider session even if one close operation fails."""
        first_error: BaseException | None = None
        for session in reversed(self._sessions):
            try:
                await session.close()
            except BaseException as error:  # noqa: BLE001
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


async def close_command_session(session: Any) -> None:
    """Close an optional command session."""
    if session is not None:
        await session.close()


__all__ = [
    "CommandRouterSession",
    "WorkspaceCommandHost",
    "close_command_session",
    "parse_command_request",
]
