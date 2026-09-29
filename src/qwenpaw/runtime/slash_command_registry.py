# -*- coding: utf-8 -*-
"""Per-workspace slash-command registry.

Named ``SlashCommandRegistry`` (not ``CommandRegistry``) to avoid clashing
with ``app/channels/command_registry.py:CommandRegistry`` — that one is the
IM-channel priority router and is unrelated to runtime slash dispatch.

Unifies the four previously-parallel command mechanisms
(conversation, control, daemon, skill) into a single dispatch point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from agentscope.message import Msg

    from .hooks import HookContext


CommandHandler = Callable[["HookContext", str], Awaitable["Msg | None"]]

SYSTEM_COMMAND_OWNER_ID = "qwenpaw.system.commands.workspace-commands"
SYSTEM_RESERVED_COMMANDS = frozenset(
    {
        "approval",
        "approve",
        "auto_memory_status",
        "checkpoint",
        "clear",
        "compact",
        "compact_str",
        "daemon",
        "deny",
        "dump_history",
        "history",
        "load_history",
        "logs",
        "message",
        "model",
        "new",
        "plan",
        "proactive",
        "reload-config",
        "reload_config",
        "reme",
        "restart",
        "skills",
        "status",
        "stop",
        "system_prompt",
        "version",
    },
)


@dataclass(frozen=True)
class FallbackDispatch:
    """Explicit result for a dynamic fallback handler."""

    handled: bool
    response: "Msg | None" = None


FallbackHandler = Callable[
    [str, "HookContext"],
    Awaitable["Msg | FallbackDispatch | None"],
]


@dataclass(frozen=True)
class CommandSpec:
    """Declarative description of one slash command.

    ``aliases`` are extra names that resolve to the same handler.
    ``category`` records the origin (``"daemon"`` / ``"control"`` /
    ``"conversation"`` / ``"skill"`` / ``"auto"`` / ``"user"``) so future
    introspection can group commands without re-parsing source.
    """

    name: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()
    category: str = "user"
    help_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    owner_id: str = "qwenpaw.legacy.commands"
    protected: bool = False


class SlashCommandRegistry:
    """Resolve raw text starting with ``/`` to a registered ``CommandSpec``.

    Lookup is case-insensitive on the command name; arguments are returned
    verbatim (with leading whitespace stripped). When no name matches and a
    single fallback handler is registered, ``dispatch`` delegates to it —
    typically used by ``/<skill_name>`` resolution.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, CommandSpec] = {}
        self._fallback: FallbackHandler | None = None

    # ---------------------------------------------------------------- register
    def register(self, spec: CommandSpec) -> None:
        names = (spec.name, *spec.aliases)
        normalized = tuple(name.casefold() for name in names)
        if spec.protected and spec.owner_id != SYSTEM_COMMAND_OWNER_ID:
            raise ValueError(
                "only the system command provider may protect names",
            )
        if not spec.protected:
            reserved = SYSTEM_RESERVED_COMMANDS.intersection(normalized)
            if reserved:
                names_text = ", ".join(f"/{name}" for name in sorted(reserved))
                raise ValueError(
                    f"reserved system command names: {names_text}",
                )
        for nm in names:
            key = nm.casefold()
            if not key:
                raise ValueError(
                    f"command spec has empty name in {names!r}",
                )
            if key in self._by_name:
                existing = self._by_name[key]
                raise ValueError(
                    f"command /{key} already registered "
                    f"by {existing.category} ({existing.name})",
                )
        for nm in names:
            self._by_name[nm.casefold()] = spec

    def register_fallback(self, handler: FallbackHandler) -> None:
        if self._fallback is not None:
            raise ValueError(
                "fallback handler already registered; only one allowed",
            )
        self._fallback = handler

    # ------------------------------------------------------------------ query
    def resolve(self, raw_text: str) -> tuple[CommandSpec, str] | None:
        """Parse ``raw_text`` and return ``(spec, args)`` or ``None``.

        Leading whitespace on ``args`` is stripped; everything else is kept
        intact (including embedded quotes and unicode).
        """
        if not raw_text:
            return None
        text = raw_text.lstrip()
        if not text.startswith("/"):
            return None
        body = text[1:]
        if not body:
            return None
        name, _, rest = body.partition(" ")
        spec = self._by_name.get(name.casefold())
        if spec is None:
            return None
        return spec, rest.lstrip()

    def names(self) -> list[str]:
        return sorted(self._by_name.keys())

    def snapshot(self) -> "SlashCommandRegistry":
        """Return an immutable-by-convention copy for one invocation."""
        snapshot = SlashCommandRegistry()
        snapshot._by_name = dict(  # pylint: disable=protected-access
            self._by_name,
        )
        snapshot._fallback = self._fallback  # pylint: disable=protected-access
        return snapshot

    def command_specs(self) -> tuple[CommandSpec, ...]:
        """Return de-duplicated specs in deterministic name order."""
        seen: set[int] = set()
        specs: list[CommandSpec] = []
        for name in sorted(self._by_name):
            spec = self._by_name[name]
            if id(spec) in seen:
                continue
            seen.add(id(spec))
            specs.append(spec)
        return tuple(specs)

    def advertisable_commands(
        self,
        *,
        exclude_categories: frozenset[str] | None = None,
        exclude_names: frozenset[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Return ``(name, help_text)`` pairs suitable for ACP broadcast.

        Only commands with non-empty ``help_text`` are included.
        Commands in *exclude_categories* or *exclude_names* are skipped.
        Duplicates (aliases) are de-duplicated by spec identity.
        """
        if exclude_categories is None:
            exclude_categories = frozenset()
        if exclude_names is None:
            exclude_names = frozenset()

        seen_specs: set[int] = set()
        result: list[tuple[str, str]] = []
        for name, spec in self._by_name.items():
            if id(spec) in seen_specs:
                continue
            if spec.category in exclude_categories:
                continue
            if name in exclude_names:
                continue
            if not spec.help_text:
                continue
            seen_specs.add(id(spec))
            result.append((name, spec.help_text))
        return result

    # ---------------------------------------------------------------- dispatch
    async def dispatch(
        self,
        raw_text: str,
        ctx: "HookContext",
    ) -> "Msg | None":
        """Resolve and execute. Returns ``None`` if nothing matched."""
        _, response = await self.dispatch_detailed(raw_text, ctx)
        return response

    async def dispatch_detailed(
        self,
        raw_text: str,
        ctx: "HookContext",
    ) -> tuple[bool, "Msg | None"]:
        """Execute and distinguish no-match from handled-without-response."""
        match = self.resolve(raw_text)
        if match is not None:
            spec, args = match
            return True, await spec.handler(ctx, args)
        if (
            self._fallback is not None
            and raw_text
            and raw_text.lstrip().startswith("/")
        ):
            result = await self._fallback(raw_text, ctx)
            if isinstance(result, FallbackDispatch):
                return result.handled, result.response
            if result is not None:
                return True, result
        return False, None


__all__ = [
    "CommandHandler",
    "CommandSpec",
    "FallbackDispatch",
    "FallbackHandler",
    "SYSTEM_COMMAND_OWNER_ID",
    "SYSTEM_RESERVED_COMMANDS",
    "SlashCommandRegistry",
]
