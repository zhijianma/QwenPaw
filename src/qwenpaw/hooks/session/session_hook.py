# -*- coding: utf-8 -*-
"""Session load/save lifecycle hooks.

Loads persisted session state into ``ctx.session_state`` (PRE_AGENT_BUILD)
so the builder can inject it into the newly-constructed agent. Saves
agent state back to session storage after the response completes.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from uuid import UUID

from ..base import LifecycleHook
from ..cron.cron_hook import restore_cron_context
from ...agents.acp.meta import ACP_EPHEMERAL_META_KEY
from ...runtime._state_utils import StateProxy
from ...runtime.console_turn_state import (
    prepare_console_regeneration,
    repair_invalid_history_images,
    stamp_console_turn,
)
from ...runtime.hooks import HookContext, HookResult
from ...runtime.phases import Phase
from .signals import SESSION_SAVE_SUCCEEDED_KEY

logger = logging.getLogger(__name__)


def _is_ephemeral_request(ctx: HookContext) -> bool:
    request = ctx.request
    request_context = getattr(request, "request_context", None)
    if isinstance(request_context, dict):
        value = request_context.get(ACP_EPHEMERAL_META_KEY)
        if value is True:
            return True
        if isinstance(value, str) and value.lower() in {"1", "true", "yes"}:
            return True
    return False


class SessionLoadHook(LifecycleHook):
    """Load persisted session state before agent construction."""

    phase = Phase.PRE_AGENT_BUILD
    name = "session_load"
    priority = 10

    async def run(self, ctx: HookContext) -> HookResult:
        if _is_ephemeral_request(ctx):
            return HookResult()
        if ctx.workspace is None:
            return HookResult()
        session = getattr(ctx.workspace, "session", None)
        if session is None:
            return HookResult()
        checkpoint_state = await self._load_background_action_checkpoint(ctx)
        if checkpoint_state is None:
            checkpoint_state = await self._load_model_step_checkpoint(ctx)
        if checkpoint_state is not None:
            ctx.session_state = checkpoint_state
        else:
            try:
                request = ctx.request
                user_id = getattr(request, "user_id", "") or ctx.session_id
                channel = getattr(request, "channel", "") or ""

                proxy = StateProxy()
                await session.load_session_state(
                    session_id=ctx.session_id,
                    user_id=user_id,
                    channel=channel,
                    agent=proxy,
                )
                if proxy.data:
                    ctx.session_state = proxy.data
            except KeyError as e:
                logger.debug(
                    "session_load: skipped (schema mismatch): %s",
                    e,
                )
            except Exception:
                logger.debug("session_load: failed", exc_info=True)
        if ctx.session_state:
            mode_state = ctx.session_state.get("mode_state")
            if isinstance(mode_state, dict):
                loaded_mode_state = dict(mode_state)
                loaded_mode_state.update(ctx.mode_state)
                ctx.mode_state = loaded_mode_state
            prepare_console_regeneration(ctx.session_state, ctx.request)
            if getattr(ctx.request, "channel", None) == "console":
                await asyncio.to_thread(
                    repair_invalid_history_images,
                    ctx.session_state,
                )
        return HookResult()

    @staticmethod
    async def _load_background_action_checkpoint(
        ctx: HookContext,
    ) -> dict | None:
        """Load a private snapshot bound to one background continuation."""
        request_context = (
            getattr(ctx.request, "request_context", None) or {}
        )
        raw_checkpoint_id = request_context.get(
            "background_action_checkpoint_id",
        )
        if raw_checkpoint_id is None:
            return None
        try:
            checkpoint_id = UUID(str(raw_checkpoint_id))
            continuation_id = UUID(
                str(request_context["background_action_continuation_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "invalid background Action checkpoint binding",
            ) from exc
        from ...runtime.background_actions import (
            lite_background_action_context_store,
        )

        checkpoint, state = await lite_background_action_context_store(
            ctx.workspace_dir,
        ).load(checkpoint_id)
        if (
            checkpoint.continuation_id != continuation_id
            or checkpoint.agent_id != ctx.agent_id
            or checkpoint.committed_item.conversation_id
            != request_context.get("os_conversation_id")
        ):
            raise ValueError(
                "background Action checkpoint ownership mismatch",
            )
        from ...runtime.model_step_contexts import (
            context_has_committed_action_items,
        )

        request = ctx.request
        proxy = StateProxy()
        await ctx.workspace.session.load_session_state(
            session_id=ctx.session_id,
            user_id=getattr(request, "user_id", "") or ctx.session_id,
            channel=getattr(request, "channel", "") or "",
            agent=proxy,
        )
        merged = copy.deepcopy(proxy.data or state)
        if context_has_committed_action_items(
            merged,
            (checkpoint.committed_item,),
        ):
            return merged
        snapshot_state = state.get("state")
        snapshot_context = (
            snapshot_state.get("context")
            if isinstance(snapshot_state, dict)
            else None
        )
        merged_state = merged.get("state")
        merged_context = (
            merged_state.get("context")
            if isinstance(merged_state, dict)
            else None
        )
        if (
            not isinstance(snapshot_context, list)
            or not snapshot_context
            or not isinstance(merged_context, list)
        ):
            raise ValueError(
                "background Action context cannot merge into Session",
            )
        result_message = next(
            (
                message
                for message in reversed(snapshot_context)
                if context_has_committed_action_items(
                    {"state": {"context": [message]}},
                    (checkpoint.committed_item,),
                )
            ),
            None,
        )
        if result_message is None:
            raise ValueError(
                "background Action snapshot has no bound result message",
            )
        merged_context.append(copy.deepcopy(result_message))
        if not context_has_committed_action_items(
            merged,
            (checkpoint.committed_item,),
        ):
            raise ValueError(
                "background Action Session merge lost its binding",
            )
        return merged

    @staticmethod
    async def _load_model_step_checkpoint(
        ctx: HookContext,
    ) -> dict | None:
        """Load an explicitly bound immutable recovery snapshot."""
        request_context = (
            getattr(ctx.request, "request_context", None) or {}
        )
        raw_checkpoint_id = request_context.get(
            "model_step_context_checkpoint_id",
        )
        if raw_checkpoint_id is None:
            return None
        try:
            checkpoint_id = UUID(str(raw_checkpoint_id))
            continuation_id = UUID(
                str(request_context["model_step_continuation_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "invalid model-step context checkpoint binding",
            ) from exc
        from ...runtime.model_step_contexts import (
            lite_model_step_context_store,
        )

        checkpoint, state = await lite_model_step_context_store(
            ctx.workspace_dir,
        ).load(checkpoint_id)
        if (
            checkpoint.continuation_id != continuation_id
            or checkpoint.conversation_id
            != request_context.get("os_conversation_id")
        ):
            raise ValueError(
                "model-step context checkpoint ownership mismatch",
            )
        return state


class SessionSaveHook(LifecycleHook):
    """Persist agent state after response completion."""

    phase = Phase.POST_RESPONSE
    name = "session_save"
    priority = 90

    async def run(self, ctx: HookContext) -> HookResult:
        ctx.extras[SESSION_SAVE_SUCCEEDED_KEY] = False
        if _is_ephemeral_request(ctx):
            return HookResult()
        if ctx.workspace is None or ctx.agent is None:
            return HookResult()
        session = getattr(ctx.workspace, "session", None)
        if session is None:
            return HookResult()
        try:
            request = ctx.request
            user_id = getattr(request, "user_id", "") or ctx.session_id
            channel = getattr(request, "channel", "") or ""

            restore_cron_context(ctx)
            proxy = StateProxy()
            proxy.data = ctx.agent.state_dict()
            stamp_console_turn(proxy.data, request, "completed")
            proxy.data["mode_state"] = ctx.mode_state
            await session.save_session_state(
                session_id=ctx.session_id,
                user_id=user_id,
                channel=channel,
                agent=proxy,
            )
            ctx.extras[SESSION_SAVE_SUCCEEDED_KEY] = True
        except Exception:
            logger.debug("session_save: failed", exc_info=True)
        return HookResult()


__all__ = ["SessionLoadHook", "SessionSaveHook"]
