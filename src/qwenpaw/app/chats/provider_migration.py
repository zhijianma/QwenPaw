"""Recover exact legacy boundaries from provider-owned history."""

import json
from importlib import import_module

from ...utils.io_utils import run_sync_io


def _signature(blocks):
    """Compare inputs, final output and ordered tool identities/content."""
    inputs, outputs, tools = [], [], []
    for role, block in blocks:
        kind = block.get("type")
        if kind == "text":
            (inputs if role == "user" else outputs).append(block["text"])
        elif kind == "data":
            data = block.get("data") or {}
            if "arguments" in data:
                arguments = data["arguments"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                tools.append((data["call_id"], arguments))
            elif "output" in data:
                tools.append((data["call_id"], data["output"]))
            else:
                return None
        else:
            return None
    if not inputs or not outputs:
        return None
    return inputs, outputs[-1], tools


def recover_qoder_boundaries(catalog, identity):
    """Append only uniquely matched facts; never replay provider context."""
    state = catalog.read_provider_state(**identity)
    binding = state["binding"]
    if not binding or binding.get("provider") != "qoder":
        return
    turns = catalog.read_legacy_turn_blocks(**identity)
    if not turns or all(t in state["boundaries"] for t in turns):
        return
    # Qoder is optional and must not be imported for other backends.
    sdk = import_module("qoder_agent_sdk")
    native = sdk.get_session_messages(binding["external_session_id"])
    candidates, blocks = [], []
    for message in native:
        if message.parent_tool_use_id:
            continue
        content = message.message.get("content", [])
        if message.type == "user" and isinstance(content, str):
            blocks = [("user", {"type": "text", "text": content})]
            continue
        if not blocks or not isinstance(content, list):
            continue
        for block in content:
            kind = block.get("type")
            if kind == "thinking":
                continue
            if kind == "tool_use":
                block = {"type": "data", "data": {
                    "call_id": block["id"],
                    "arguments": block["input"],
                }}
            elif kind == "tool_result":
                block = {"type": "data", "data": {
                    "call_id": block["tool_use_id"],
                    "output": block.get("content"),
                }}
            blocks.append((message.type, block))
        if message.message.get("stop_reason") == "end_turn":
            candidates.append((_signature(blocks), message.uuid))
            blocks = []
    _record_matches(catalog, identity, state, turns, candidates, "message")


def _record_matches(catalog, identity, state, turns, candidates, kind):
    """Persist only unique matches in increasing native history order."""
    binding = state["binding"]
    previous = -1
    matched = []
    for turn_id, local in turns.items():
        signature = _signature(local)
        matches = [
            i for i, (value, _) in enumerate(candidates)
            if signature is not None and value == signature
        ]
        if len(matches) != 1 or matches[0] <= previous:
            return
        previous = matches[0]
        matched.append((turn_id, candidates[previous][1]))
    for turn_id, boundary_id in matched:
        if turn_id not in state["boundaries"]:
            catalog.record_provider_fact(
                **identity,
                kind="turn_boundary",
                turn_id=turn_id,
                data={**binding, "boundary_kind": kind,
                      "boundary_id": boundary_id},
            )


async def recover_codex_boundaries(catalog, identity, binary=None):
    """Match full native turns without resuming or replaying a thread."""
    state = await run_sync_io(catalog.read_provider_state, **identity)
    binding = state["binding"]
    if not binding or binding.get("provider") != "codex":
        return
    turns = await run_sync_io(catalog.read_legacy_turn_blocks, **identity)
    if not turns or all(t in state["boundaries"] for t in turns):
        return
    codex = import_module("...harnesses.codex.adapter", __package__)
    client = codex.CodexAppServerClient(binary=binary)
    candidates = []
    try:
        await client.start()
        result = await client.request("thread/read", {
            "threadId": binding["external_session_id"],
            "includeTurns": True,
        })
        for turn in result["thread"]["turns"]:
            if turn.get("status") != "completed":
                continue
            blocks = []
            for item in turn.get("items", []):
                for entry in codex.CodexAdapter._history_item(item):
                    kind = entry.kind.value
                    if kind == "reasoning":
                        continue
                    role = "user" if kind == "user" else "assistant"
                    if kind in {"tool_call", "tool_output"}:
                        continue
                    blocks.append((role, {
                        "type": "text", "text": entry.text,
                    }))
            candidates.append((_signature(blocks), turn["id"]))
    finally:
        await client.stop()
    # Native thread/read omits stored tools; match unique text boundaries.
    text_turns = {
        turn_id: [(role, block) for role, block in blocks
                  if block.get("type") == "text"]
        for turn_id, blocks in turns.items()
    }
    await run_sync_io(
        _record_matches, catalog, identity, state,
        text_turns, candidates, "turn",
    )
