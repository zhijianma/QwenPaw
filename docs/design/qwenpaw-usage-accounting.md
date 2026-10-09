# QwenPaw Usage Accounting Contract

> Status: Lite fact projection implemented; Chat-first scope. Task UI is
> excluded.

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
fact or create an aggregated fallback row that cannot later be deduplicated.
Reconciliation replays the fact later.

## 4. Query contract

All scopes are filters over the same facts:

- global: no ownership filter;
- agent: `agent_id`;
- chat: `agent_id + ChatSpec.id`;
- turn: `agent_id + ChatSpec.id + invocation_id`;
- date/model: UTC `completed_at` plus actual Provider/Model from the Attempt.

Every scope exposes cost evidence alongside tokens. `cost_micros` sums only
Provider-reported monetary micro-units; `cost_unknown_calls` counts calls whose
price is unavailable. The two fields remain separate, so a partially known
aggregate is never presented as a complete bill or as zero-cost usage. The
contract does not infer a currency or convert Provider prices.

Retry and fallback are separate attempts and separate calls. Multiple calls in
one tool loop share the Invocation turn. Composite public keys remain
collision-safe JSON tuples until the API moves to structured rows.

The persisted Chat turn snapshot keeps aggregate input/output totals for
compact rendering and a `model_routes` breakdown keyed by actual
Provider/Model. Repeated calls to one route increase its `call_count`; a
fallback or mid-turn route switch creates another row. The UI must not label
the aggregate total as belonging only to the final route.

### Context-window statistics

Context utilization is a Model Call fact, not a scan of message content. Each
Attempt records the actual model's context window and active compaction
threshold. Its effective context input is:

- cache-eligible input when the Adapter's cache semantics are verified;
- otherwise the Provider-reported input tokens.

Every global/Agent/Chat/Turn/date/model aggregate exposes:

- weighted utilization: sum of effective context input divided by sum of
  observed context windows;
- peak utilization: the largest single-call ratio in the range;
- observed calls: calls with a known positive context window;
- near-compaction calls: calls at or above their own configured threshold.

Unknown historical windows remain unobserved and render as unavailable, never
as `0%`. The post-turn character estimate remains useful for the live Chat
indicator, but is labelled `local_estimate` and does not enter cross-scope
Provider statistics.

## 5. Legacy cutover

The existing JSON file contains already-aggregated rows and has no attempt ID.
It cannot be safely merged with Model Call facts by date because overlap would
double count. The read switch therefore requires all of the following:

1. write the next UTC date as a one-time immutable cutover watermark;
2. keep attributed legacy rows strictly before that watermark;
3. index Model Call results by unique `attempt_id` and query them at or after
   the watermark;
4. rebuild the disposable index from all configured Agent workspaces at app
   startup;
5. expose cutover, indexed count and last rebuild state without message or
   prompt content;
6. dual-write before the cutover as a bounded structural shadow period; stop
   attributed JSON writes after it. Unscoped compatibility writes remain in
   JSON and cannot overlap a Model Call attempt.

During the pre-cutover shadow period, queries may overlay fact-derived context
fields onto an exact legacy aggregation identity. Token and call totals still
come from JSON, so the overlay cannot double count them.

No direct date-level addition is allowed. The query removes attributed JSON
rows on and after cutover before adding projected rows. Records that predate
Agent, Chat, or Turn ownership remain explicitly unattributed.

## 6. Lite acceptance

- A successful normal and streaming response persists complete usage evidence.
- Failed, cancelled, retry and fallback attempts retain their own result.
- Cache counters are zero when the adapter semantics are not verified.
- A failed Model Call result write emits no legacy projection row.
- Historical Model Call files without Agent/cache fields remain readable.
- Summary values remain stable across restart and projection rebuild.
- Global, Agent, Chat and Turn totals reconcile to the same attempt set.
- Known cost and unknown-cost call counts reconcile across every scope.
- A multi-call Turn preserves per-route token totals after message refresh.
- Task pages and Task-specific frontend work remain out of scope.
