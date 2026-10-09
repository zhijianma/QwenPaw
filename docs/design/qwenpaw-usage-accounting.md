# QwenPaw Usage Accounting Contract

> Status: incremental migration; Chat-first Lite scope. Task UI is excluded.

## 1. Decision

Model Call is the authoritative fact for model usage. Token Usage is a
read-optimized, rebuildable projection. A frontend aggregate, message-history
scan, or fire-and-forget JSON buffer must never become a competing fact source.

The ownership path is:

```text
Agent
  └── ChatSpec.id
      └── Invocation (turn)
          └── ModelCallAttempt (actual provider/model)
              └── ModelCallResult (provider usage/cache/cost)
```

`session_id` is transport compatibility only and is not an accounting key.

## 2. Fact and projection boundaries

| Layer | Responsibility | Durability |
|---|---|---|
| `ModelCallAttempt` | Agent, Chat, Invocation, route, adapter and formatter identity | Immutable fact |
| `ModelCallResult` | terminal status, provider-reported tokens, cache counters and cost | Immutable fact |
| Task usage ledger | enforce one Task execution budget | Append-only execution fact |
| Token Usage Summary | global/agent/chat/turn/date/model query | Rebuildable projection |
| Chat message usage | render the completed turn after refresh | Snapshot projection |
| legacy token JSON | preserve pre-cutover history and API continuity | Compatibility projection |

Provider usage uses `provider_reported`. A local context estimate uses
`local_estimate`; it may help explain context pressure but must not be added to
provider billing totals.

## 3. Write ordering

For every provider attempt:

1. persist `ModelCallAttempt` before network dispatch;
2. normalize provider usage once at the adapter boundary;
3. charge the active execution budget when applicable;
4. persist `ModelCallResult`, including cache and cost evidence;
5. update compatibility and UI projections only after result persistence.

This ordering prevents a failed durable write from creating a phantom call in
the legacy statistics file. A projection failure must not mutate the immutable
fact. Reconciliation can replay facts later.

## 4. Query contract

All scopes are filters over the same facts:

- global: no ownership filter;
- agent: `agent_id`;
- chat: `agent_id + ChatSpec.id`;
- turn: `agent_id + ChatSpec.id + invocation_id`;
- date/model: UTC `completed_at` plus actual Provider/Model from the Attempt.

Retry and fallback are separate attempts and separate calls. Multiple calls in
one tool loop share the Invocation turn. Composite public keys remain
collision-safe JSON tuples until the API moves to structured rows.

## 5. Legacy cutover

The existing JSON file contains already-aggregated rows and has no attempt ID.
It cannot be safely merged with Model Call facts by date because overlap would
double count. The read switch therefore requires all of the following:

1. write a one-time UTC cutover watermark and immutable source version;
2. keep legacy rows strictly before that watermark;
3. index Model Call results at or after the watermark by unique `attempt_id`;
4. make the index disposable and rebuildable from registered Agent workspaces;
5. expose reconciliation state (`watermark`, indexed count, last rebuild,
   errors) without message or prompt content;
6. compare old and new totals during a bounded shadow-read period before the
   API changes source.

No direct date-level addition is allowed. Records that predate Agent, Chat, or
Turn ownership remain explicitly unattributed.

## 6. Lite acceptance

- A successful normal and streaming response persists complete usage evidence.
- Failed, cancelled, retry and fallback attempts retain their own result.
- Cache counters are zero when the adapter semantics are not verified.
- A failed Model Call result write emits no legacy projection row.
- Historical Model Call files without Agent/cache fields remain readable.
- Summary values remain stable across restart and projection rebuild.
- Global, Agent, Chat and Turn totals reconcile to the same attempt set.
- Task pages and Task-specific frontend work remain out of scope.

