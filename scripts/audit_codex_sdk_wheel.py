# -*- coding: utf-8 -*-
"""Audit one pinned openai-codex wheel against QwenPaw safety gates."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from zipfile import ZipFile

SCHEMA = "qwenpaw.codex-sdk-compatibility.v1"

_OPERATIONS = {
    "thread_start": ("AsyncCodex", "thread_start"),
    "thread_resume": ("AsyncCodex", "thread_resume"),
    "thread_fork": ("AsyncCodex", "thread_fork"),
    "history": ("AsyncThread", "read"),
    "stream": ("AsyncTurnHandle", "stream"),
    "steer": ("AsyncTurnHandle", "steer"),
    "interrupt": ("AsyncTurnHandle", "interrupt"),
    "login": ("AsyncCodex", "login_chatgpt"),
    "models": ("AsyncCodex", "models"),
}


def _class_methods(tree: ast.Module) -> dict[str, dict[str, ast.AST]]:
    result: dict[str, dict[str, ast.AST]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        result[node.name] = {
            item.name: item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    return result


def _parameters(method: ast.AST | None) -> set[str]:
    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    arguments = method.args
    return {
        item.arg
        for item in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        )
    }


def _method_text(method: ast.AST | None) -> str:
    return ast.unparse(method) if method is not None else ""


def _assignment_text(tree: ast.Module, name: str) -> str:
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if any(
            isinstance(item, ast.Name) and item.id == name for item in targets
        ):
            return ast.unparse(node.value) if node.value is not None else ""
    return ""


def _read_module(wheel: ZipFile, name: str) -> tuple[str, ast.Module]:
    path = f"openai_codex/{name}.py"
    source = wheel.read(path).decode("utf-8")
    return source, ast.parse(source, filename=path)


def _metadata(wheel: ZipFile) -> dict[str, str]:
    names = [
        name
        for name in wheel.namelist()
        if name.endswith(".dist-info/METADATA")
    ]
    if len(names) != 1:
        raise ValueError(
            "wheel must contain exactly one dist-info METADATA file",
        )
    message = BytesParser().parsebytes(wheel.read(names[0]))
    return {
        "name": str(message.get("Name") or ""),
        "version": str(message.get("Version") or ""),
    }


def audit_wheel(path: Path) -> dict[str, Any]:
    """Return a deterministic compatibility report for an SDK wheel."""
    wheel_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    with ZipFile(path) as wheel:
        metadata = _metadata(wheel)
        _, api_tree = _read_module(wheel, "api")
        _, async_tree = _read_module(wheel, "async_client")
        _, client_tree = _read_module(wheel, "client")
        _, router_tree = _read_module(wheel, "_message_router")

    api = _class_methods(api_tree)
    async_client = _class_methods(async_tree)
    client = _class_methods(client_tree)
    router = _class_methods(router_tree)
    operations = {
        name: method in api.get(class_name, {})
        for name, (class_name, method) in _OPERATIONS.items()
    }

    sync_init = client.get("CodexClient", {}).get("__init__")
    async_init = async_client.get("AsyncCodexClient", {}).get("__init__")
    public_async_init = api.get("AsyncCodex", {}).get("__init__")
    handler_type = _assignment_text(client_tree, "ApprovalHandler")
    default_handler = client.get("CodexClient", {}).get(
        "_default_approval_handler",
    )
    default_handler_text = _method_text(default_handler)

    stream = api.get("AsyncTurnHandle", {}).get("stream")
    next_notification = async_client.get("AsyncCodexClient", {}).get(
        "next_turn_notification",
    )
    unregister_turn = router.get("MessageRouter", {}).get("unregister_turn")
    stream_text = _method_text(stream)
    next_notification_text = _method_text(next_notification)
    unregister_turn_text = _method_text(unregister_turn)

    approvals = {
        "sync_handler_injectable": "approval_handler"
        in _parameters(sync_init),
        "async_handler_injectable": (
            "approval_handler" in _parameters(async_init)
            or "approval_handler" in _parameters(public_async_init)
        ),
        "handler_supports_awaitable": "Awaitable" in handler_type,
        "default_accepts": "'accept'" in default_handler_text,
        "default_fail_closed": (
            "'accept'" not in default_handler_text
            and any(
                decision in default_handler_text
                for decision in ("'decline'", "'deny'", "'reject'")
            )
        ),
    }
    cancellation = {
        "stream_unregisters_route": "unregister_turn_notifications"
        in stream_text,
        "blocking_wait_offloaded": "_call_sync" in next_notification_text,
        "unregister_wakes_waiter": any(
            marker in unregister_turn_text
            for marker in (".put(", ".put_nowait(", "fail")
        ),
        "stream_cancellation_interrupts_turn": "interrupt" in stream_text,
    }

    blockers: list[str] = []
    if metadata["name"] != "openai-codex":
        blockers.append("DISTRIBUTION_MISMATCH")
    if not all(operations.values()):
        blockers.append("REQUIRED_OPERATION_MISSING")
    if approvals["default_accepts"] or not approvals["default_fail_closed"]:
        blockers.append("APPROVAL_DEFAULT_NOT_FAIL_CLOSED")
    if not approvals["async_handler_injectable"]:
        blockers.append("ASYNC_APPROVAL_HANDLER_UNAVAILABLE")
    if not approvals["handler_supports_awaitable"]:
        blockers.append("DEFERRED_APPROVAL_UNSUPPORTED")
    if (
        cancellation["blocking_wait_offloaded"]
        and not cancellation["unregister_wakes_waiter"]
    ):
        blockers.append("ASYNC_STREAM_CANCELLATION_LEAK_RISK")

    return {
        "schema": SCHEMA,
        "distribution": metadata,
        "wheel_sha256": wheel_sha256,
        "operations": operations,
        "approvals": approvals,
        "cancellation": cancellation,
        "blockers": blockers,
        "safe_for_qwenpaw": not blockers,
    }


def main() -> int:
    """Run the wheel audit and print one machine-readable report."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    args = parser.parse_args()
    report = audit_wheel(args.wheel)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["safe_for_qwenpaw"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
