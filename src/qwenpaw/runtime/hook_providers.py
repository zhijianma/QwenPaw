# -*- coding: utf-8 -*-
"""Generation-pinned lifecycle Hook Provider assembly and dispatch."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..kernel.models import (
    HookDefinition,
    HookDisposition,
    HookMessage,
    HookOutcome,
    LifecyclePhase,
)
from .hooks import HookContext, HookRegistry, HookResult
from .phases import Phase

_HOOK_ID_PART_RE = re.compile(r"[^a-z0-9_.-]+")


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content or "Request handled by lifecycle hook."
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                value = block.get("text")
            else:
                value = getattr(block, "text", None)
            if isinstance(value, str) and value:
                parts.append(value)
    return "\n\n".join(parts) or "Request handled by lifecycle hook."


def legacy_result_to_outcome(result: HookResult) -> HookOutcome:
    """Translate the AgentScope-specific compatibility result."""
    disposition = HookDisposition(result.action.value)
    if disposition != HookDisposition.SHORT_CIRCUIT:
        return HookOutcome(disposition=disposition)
    if result.payload is None:
        raise TypeError("short-circuit legacy hook returned no payload")
    return HookOutcome(
        disposition=disposition,
        message=HookMessage(text=_message_text(result.payload)),
    )


@dataclass(frozen=True)
class WorkspaceHookHost:
    """Fixed compatibility view of one Workspace Hook Registry."""

    registry: HookRegistry
    context: HookContext
    provider_id: str
    _definitions: tuple[HookDefinition, ...]
    _hooks: dict[str, Any]

    @classmethod
    def capture(
        cls,
        context: HookContext,
        provider_id: str,
    ) -> "WorkspaceHookHost":
        """Capture and identify the current topologically sorted hooks."""
        registry = context.workspace.plugins.hook_registry.snapshot()
        entries = registry.hook_entries()
        name_to_id: dict[str, str] = {}
        hooks: dict[str, Any] = {}
        provisional: list[tuple[Any, str, int]] = []
        for order, hook in enumerate(entries):
            slug = (
                _HOOK_ID_PART_RE.sub(
                    "-",
                    hook.name.casefold(),
                ).strip(".-")
                or f"hook-{order}"
            )
            hook_id = f"{provider_id}.{slug}"
            if hook_id in hooks:
                raise ValueError(f"duplicate legacy hook name '{hook.name}'")
            name_to_id[hook.name] = hook_id
            hooks[hook_id] = hook
            provisional.append((hook, hook_id, order))
        definitions = tuple(
            HookDefinition(
                hook_id=hook_id,
                provider_id=provider_id,
                phase=LifecyclePhase(hook.phase.value),
                priority=hook.priority,
                order=order,
                before=tuple(
                    name_to_id[name]
                    for name in hook.before
                    if name in name_to_id
                ),
                after=tuple(
                    name_to_id[name]
                    for name in hook.after
                    if name in name_to_id
                ),
            )
            for hook, hook_id, order in provisional
        )
        return cls(registry, context, provider_id, definitions, hooks)

    def list_hooks(self) -> Sequence[HookDefinition]:
        """Return the fixed compatibility catalog."""
        return self._definitions

    async def run_hook(self, hook_id: str) -> HookOutcome:
        """Execute one captured legacy hook."""
        hook = self._hooks.get(hook_id)
        if hook is None:
            raise LookupError(hook_id)
        return legacy_result_to_outcome(await hook.run(self.context))

    def inject_context(
        self,
        content: str,
        *,
        priority: int = 100,
        source: str = "",
    ) -> None:
        """Expose the stable context-injection operation to providers."""
        self.context.inject_context(
            content,
            priority=priority,
            source=source,
        )


class HookRouterSession:
    """Execute merged Hook Provider catalogs in deterministic order."""

    def __init__(self, sessions: Sequence[Any]) -> None:
        self._sessions = tuple(
            sorted(sessions, key=lambda item: item.provider_id),
        )
        self._definitions: dict[str, tuple[Any, HookDefinition]] = {}
        self._ordered: dict[LifecyclePhase, tuple[str, ...]] = {}
        self._build_catalog()

    def _build_catalog(self) -> None:
        by_phase: dict[LifecyclePhase, list[HookDefinition]] = defaultdict(
            list,
        )
        for session in self._sessions:
            provider_id = session.provider_id
            for definition in session.list_hooks():
                if definition.provider_id != provider_id:
                    raise ValueError(
                        f"hook '{definition.hook_id}' has mismatched "
                        "provider ownership",
                    )
                if definition.hook_id in self._definitions:
                    raise ValueError(
                        f"duplicate hook id '{definition.hook_id}'",
                    )
                self._definitions[definition.hook_id] = (session, definition)
                by_phase[definition.phase].append(definition)
        self._ordered = {
            phase: self._topological_order(definitions)
            for phase, definitions in by_phase.items()
        }

    @staticmethod
    def _topological_order(
        definitions: Sequence[HookDefinition],
    ) -> tuple[str, ...]:
        by_id = {definition.hook_id: definition for definition in definitions}
        incoming = {hook_id: set() for hook_id in by_id}
        outgoing = {hook_id: set() for hook_id in by_id}

        def add_edge(before: str, after: str) -> None:
            if before not in by_id or after not in by_id:
                return
            outgoing[before].add(after)
            incoming[after].add(before)

        for definition in definitions:
            for other in definition.before:
                add_edge(definition.hook_id, other)
            for other in definition.after:
                add_edge(other, definition.hook_id)

        def sort_key(hook_id: str) -> tuple[int, int, str]:
            definition = by_id[hook_id]
            return definition.priority, definition.order, hook_id

        ready = sorted(
            (hook_id for hook_id, deps in incoming.items() if not deps),
            key=sort_key,
        )
        ordered: list[str] = []
        while ready:
            hook_id = ready.pop(0)
            ordered.append(hook_id)
            for other in sorted(outgoing[hook_id]):
                incoming[other].discard(hook_id)
                if not incoming[other]:
                    ready.append(other)
                    ready.sort(key=sort_key)
        if len(ordered) != len(by_id):
            unresolved = sorted(set(by_id).difference(ordered))
            raise ValueError(f"hook ordering cycle detected: {unresolved}")
        return tuple(ordered)

    def list_hooks(self) -> tuple[HookDefinition, ...]:
        """Return every merged definition in stable identity order."""
        return tuple(
            self._definitions[hook_id][1]
            for hook_id in sorted(self._definitions)
        )

    async def run(self, phase: LifecyclePhase) -> HookOutcome:
        """Execute one phase with sticky skip and immediate short circuit."""
        disposition = HookDisposition.CONTINUE
        for hook_id in self._ordered.get(phase, ()):
            session, _ = self._definitions[hook_id]
            outcome = await session.run_hook(hook_id)
            if outcome.disposition == HookDisposition.SHORT_CIRCUIT:
                return outcome
            if outcome.disposition == HookDisposition.SKIP_AGENT:
                disposition = HookDisposition.SKIP_AGENT
        return HookOutcome(disposition=disposition)

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


async def run_legacy_phase(
    registry: HookRegistry,
    phase: Phase,
    context: HookContext,
) -> HookOutcome:
    """Bootstrap fallback used only before a Hook Session can open."""
    result = await registry.run(phase, context)
    return legacy_result_to_outcome(result)


async def close_hook_session(session: Any) -> None:
    """Close an optional Hook Router session."""
    if session is not None:
        await session.close()


__all__ = [
    "HookRouterSession",
    "WorkspaceHookHost",
    "close_hook_session",
    "legacy_result_to_outcome",
    "run_legacy_phase",
]
