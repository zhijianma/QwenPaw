# Codex SDK adoption gate

Status: transport replacement blocked for `openai-codex==0.144.4`.

## Scope

QwenPaw intends to replace its quarantined private app-server transport with
the official Python SDK. Replacement is allowed only when the pinned artifact
preserves QwenPaw's durable, fail-closed approval and cancellation semantics.
Method names alone are not sufficient evidence.

Audited artifact:

- distribution: `openai-codex==0.144.4`;
- wheel SHA-256:
  `de1513a6e94b9a8d7728a3b74298bc1469428ade10ba0ef2d5db47dd1cb606f5`;
- audit schema: `qwenpaw.codex-sdk-compatibility.v1`.

## Result

The pinned SDK exposes all required functional operations:

| Capability | 0.144.4 |
| --- | --- |
| Thread start / resume / fork | Present |
| Thread history | Present |
| Turn stream | Present |
| Turn steer / interrupt | Present |
| Login | Present |
| Model discovery | Present |

It does not pass the safety gate:

| Gate | Evidence | Result |
| --- | --- | --- |
| Default approval is fail-closed | Default handler returns `accept` for command and file-change approval | Blocked |
| Async public handler injection | `AsyncCodex` and `AsyncCodexClient` constructors expose no approval handler | Blocked |
| Deferred approval | `ApprovalHandler` is synchronous and cannot return `Awaitable` | Blocked |
| Stream cancellation cleanup | async wait is offloaded to a blocking queue; unregister removes the route without waking the waiter | Blocked |

The last item does not require stream cancellation to interrupt the Codex turn.
Stopping observation and interrupting execution remain distinct operations. The
required property is that cancellation releases the blocked SDK worker.

## Reproduce

Download the exact wheel without installing it into the QwenPaw environment,
then run:

```bash
python scripts/audit_codex_sdk_wheel.py \
  --wheel /absolute/path/openai_codex-0.144.4-py3-none-any.whl
```

Exit code `0` means every functional and safety gate passed. Exit code `1`
means the JSON report contains one or more blockers. The 0.144.4 report is:

```text
APPROVAL_DEFAULT_NOT_FAIL_CLOSED
ASYNC_APPROVAL_HANDLER_UNAVAILABLE
DEFERRED_APPROVAL_UNSUPPORTED
ASYNC_STREAM_CANCELLATION_LEAK_RISK
```

## Adoption rule

Do not access private SDK fields to install a handler, and do not maintain both
transports in production. On a future SDK upgrade:

1. run the wheel audit against the exact pinned artifact;
2. exercise delayed approval, stream cancellation and process shutdown with the
   bundled matching CLI;
3. map typed notifications and approval decisions behind the existing
   `HarnessAdapter` contract;
4. switch the transport in one change and delete the private subprocess,
   JSON-RPC router and their transport-only tests.

Until all gates pass, `CodexAppServerClient` remains a frozen compatibility
adapter and must not gain new product or domain behavior.
