# QwenPaw Model Transport Contract

Status: Lite Kernel contract frozen; resumable Provider Adapter pending.

## Ownership

Model transport is a Provider Adapter capability. The Kernel understands only
content-safe capability and validation facts; it does not know WebSocket frame
formats, HTTP headers, provider cursors, route tokens, response bodies, or
credentials.

`Provider.get_model_transport_contract(model_id)` is resolved while building
the concrete model. `TokenRecordingModelWrapper` fixes that contract in each
`ModelCallAttempt` before network I/O. Providers that do not override the
method receive the safe HTTP/non-resumable contract and use durable context
rebuild after partial output.

## Stable models

- `ModelTransportContract`: protocol, cursor-resume mode, route affinity,
  fallback and required validation capabilities.
- `ModelTransportResumeEvidence`: Host-keyed HMAC digests of expected/actual
  response identity, emitted prefix and optional sticky route, plus exact
  prefix byte counts.
- `ModelTransportRecoveryDecision`: selected recovery mode and content-safe
  validation reason.
- `evaluate_model_transport_recovery()`: the common fail-closed validator.

Raw response IDs, cursor values, route tokens and output prefixes are
Provider-private. Only per-runtime `hmac-sha256:` digests and lengths may enter
the ephemeral Kernel validator; Resume Evidence is not persisted or exposed by
the API. Host code creates those digests through
`ModelTransportEvidenceHasher`, which uses a random process-local key by
default and separates response, prefix and route domains. The durable Result
contains only the recovery mode and enum reason.

## Admission rules

1. Cursor resume must explicitly validate response identity and prefix.
2. Sticky routing is valid only for a resumable transport.
3. WebSocket-to-HTTP continuation must be declared by the Provider contract.
4. Inline resume and HTTP continuation are both candidate recoveries, not
   trusted outcomes. Each needs complete matching evidence.
5. Missing evidence, response mismatch, prefix hash/length mismatch or route
   mismatch selects `durable_context_rebuild`.
6. Once an interruption reaches the generic Model Call wrapper, trusted
   Provider evidence is unavailable; the wrapper therefore records durable
   rebuild rather than guessing that a continuation occurred.

## Compatibility and migration

Historical `ModelCallAttempt` records omit `transport_contract` and read as the
safe default. Historical `ModelCallResult` records may omit transport recovery
facts. New partial-stream failures record both the recovery mode and validation
reason. Pre-output retry/fallback, user Interrupt and successful calls do not
invent a transport continuation decision.

The first Provider that enables cursor resume must implement a Provider-local
Adapter that validates its candidate before yielding any resumed bytes. It
must then pass simulated identity, prefix and sticky-route failures plus a real
disconnect/reconnect exercise. Until that exists, Lite deliberately continues
through the already durable model-step context reconstruction path.
