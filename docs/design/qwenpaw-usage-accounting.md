# QwenPaw Usage Accounting Contract

> Status: Lite fact projection implemented; Chat-first scope. Task UI is
> excluded.

## 1. Decision

Model Call is the authoritative fact for model usage. Token Usage is a
read-optimized, rebuildable projection. A frontend aggregate, message-history
scan, or fire-and-forget JSON buffer must never become a competing fact source.

The public query models live in `token_usage/models.py`. Projection and API
layers depend on this stable contract; the lifecycle manager depends on both,
so the disposable SQLite projection never imports orchestration or singleton
state. `manager.py` continues to re-export these names for source compatibility
with existing extensions. Cross-scope reduction lives in the pure
`token_usage/aggregation.py` function, keeping weighting, coverage and
collision-safe compatibility keys independent from storage lifecycle. Legacy
JSON decoding and cutover overlay live in `token_usage/compatibility.py`; the
Manager only coordinates the buffer, fact projection and those pure adapters.

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
| Live turn accumulator | isolate active Chat/Invocation deltas for gates and SSE | Process-local projection |
| Chat message usage | render the completed turn after refresh | Snapshot projection |
| legacy token JSON | preserve pre-cutover history and API continuity | Compatibility projection |

Provider usage uses `provider_reported`. A local context estimate uses
`local_estimate`; it may help explain context pressure but must not be added to
provider billing totals. In particular, an estimated assistant-message size
must never replace a Provider-reported completion count. The Chat snapshot
keeps `latest_assistant_tokens` under `context_usage` as separate local
evidence when it is available.

The live accumulator is not a fact store. Its domain API accepts `chat_id`
(`ChatSpec.id`) and `invocation_id`, so simultaneous turns cannot merge. Model
wrappers only emit normalized deltas; Loop Gates and Chat persistence consume
snapshots through that API. A transport `session_id` may enter only through a
named protocol compatibility adapter when no Chat/Invocation identity exists;
it must not leak back into the accounting model.

The accumulator stores frozen `TurnUsageEvidence` and
`TurnModelUsageRoute` objects internally. Provider-reported, local-estimate,
partial and unavailable measurements have explicit invariants; token totals,
route totals and missing-usage call counts fail closed when contradictory.
The accumulator also rejects a payload whose `chat_id` or `turn_id` conflicts
with its ownership key, and uses a payload `turn_id` when a compatibility
caller omits the explicit Invocation argument.
Public callers continue to receive sparse dictionaries so persisted Chat
metadata and rolling-upgrade plugins do not acquire newly defaulted fields.

The package-level `persist_chat_turn_usage` function and the model wrapper's
`pop_usage_for_chat` method are the domain-facing extension points. The older
`persist_turn_usage` and `pop_usage_for_session` names remain compatibility
adapters for existing session-oriented plugins and protocols.

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

The Summary API exposes ownership aggregates as structured
`scopes.agents/chats/turns` rows. Identity is carried by explicit fields instead
of JSON-encoded dictionary keys. The older `by_agent/by_chat/by_turn` maps remain
read-only compatibility projections during migration and must reconcile exactly
with the structured rows; clients should not parse their keys.

`chat_id` is the canonical public identity field and always means
`ChatSpec.id`. New Summary/Details JSON and OpenAPI only expose `chat_id`.
Historical payloads and extensions may still provide `conversation_id`, and
Python retains a read-only compatibility property. The legacy SQLite column and
`ModelCallAttempt.conversation_id` are translated at the projection boundary;
they are not a second usage ownership concept.

The active-turn accumulator never infers Chat ownership from a transport
session when a Model Call Attempt exists. Attempt ownership wins even when it
is intentionally unscoped. Only callers without the durable Model Call plane
may enter through the explicitly named legacy protocol-session adapter; this
keeps old transports operational without polluting `ChatSpec.id` aggregates.

Every scope exposes cost evidence alongside tokens. `cost_micros` sums only
Provider-reported monetary micro-units; `cost_unknown_calls` counts calls whose
price is unavailable. The two fields remain separate, so a partially known
aggregate is never presented as a complete bill or as zero-cost usage. The
contract does not infer a currency or convert Provider prices.

Every terminal Model Call remains visible even when its Provider omits token
usage. `call_count` therefore means actual terminal attempts, while
`usage_observed_calls` and `usage_unobserved_calls` expose measurement
coverage. Unobserved calls contribute zero only to the arithmetic token sum;
they are never described as measured zero-token calls. Their context window is
also excluded from utilization because a known denominator without observed
input would manufacture a false `0%` ratio.

Historical Result facts written before `usage_measurement` existed are upgraded
at the Kernel model boundary: a complete input/output pair means
`provider_reported`, while a partial pair is rejected as ambiguous. This keeps
the immutable files untouched and makes a projection rebuild repair coverage
instead of simultaneously counting their tokens and calling them unobserved.

The completed Chat turn snapshot follows the same rule. A successful call
without Provider usage is persisted as `measurement=unavailable` with its
actual Provider/Model and call count. Mixed turns use `measurement=partial`:
known tokens remain visible while `usage_unobserved_calls` reports missing
measurements. A Provider-reported zero remains distinct from both cases. The
Console renders these states after streaming and after message-history reload;
it never formats an unavailable call as `0 tok`.

Retry and fallback are separate attempts and separate calls. Multiple calls in
one tool loop share the Invocation turn. Ownership maps use collision-safe JSON
tuple keys and structured rows are canonical. Model maps retain the readable
`provider:model` key when it is unique; if two distinct Provider/Model pairs
would produce the same string, every colliding pair uses an exact JSON tuple
key. Clients must render the explicit `provider_id` and `model` fields instead
of parsing or displaying either compatibility key.

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
- Date and actual Provider/Model totals reconcile to that same attempt set.
- Summary/Details responses contain `chat_id` only; legacy input still restores.
- An unscoped Model Call cannot inherit a transport session as Chat ownership.
- Known cost and unknown-cost call counts reconcile across every scope.
- Calls without Provider usage remain countable and report unknown coverage.
- A multi-call Turn preserves per-route token totals after message refresh.
- A Provider-reported Turn is never rewritten with a local output estimate.
- Turn route totals, coverage, Chat identity and Invocation identity reconcile
  before the snapshot can enter SSE or message metadata.
- Malformed live Turn totals or measurement states are rejected before they
  can reach SSE or persisted message metadata.
- Task pages and Task-specific frontend work remain out of scope.
