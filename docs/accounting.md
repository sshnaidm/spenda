# Accounting method

This document describes how every displayed dollar total can be reproduced from normalized `usage` rows and effective-dated `prices` rows.

## 1. Counted events

The preferred source is `token_usage_record.payload.usage`, which is an atomic response-level counter and has a stable `response_id`.

When no matching atomic record exists, `event_msg/token_count` is the fallback. The first snapshot uses `last_token_usage` (or the cumulative value if last usage is absent). Later records count the categorized monotonic delta in `total_token_usage`. This is necessary because older Codex UI events sometimes repeat the same `last_token_usage` at a new timestamp while the cumulative snapshot remains unchanged. An unchanged snapshot is ignored. A counter decrease is treated as a reset: the per-response value is counted when available and a `counter_reset` warning is stored.

Every normalized row recomputes `total_tokens = input_tokens + output_tokens`. Legacy UI records with zero input/output/cache/reasoning categories and only a stale nonzero `total_tokens` are ignored.

## 2. Ignored events

- cumulative `turn_token_usage`, `thread_token_usage`, and `total_token_usage` are never summed; atomic thread totals are also never used as the baseline for the separate full-history UI cumulative counter;
- a `token_count` identical to the immediately preceding atomic usage record is a duplicate UI representation and is ignored;
- rate-limit-only token events with `info: null` are ignored;
- prompts, response bodies, reasoning bodies, tool arguments/output, patches, world state, and unknown event kinds are ignored;
- `threads.tokens_used` is diagnostic only and is not another usage record.

The adapters may retain one fixed, non-content action label alongside a usage row when persisted metadata identifies the response as a test run, file change, image inspection, command, file search/read, web action, subagent action, assistant response, or similar fixed category. OpenCode and Claude Code labels use only content-block/part types and tool names. The dashboard does not retain the command, filenames, message text, arguments, or output. When the association is ambiguous, the label remains null and the UI shows only its sequential call marker.

## 3. Incremental versus cumulative semantics

`payload.usage` and `last_token_usage` are treated as incremental per-response records. The dashboard stores one normalized row per response/fallback event. Cumulative snapshots are retained only in ingestion state so a legacy delta can be computed; they are not copied into the usage ledger.

Incremental import stores the last complete byte offset and parser context. Re-reading, rescanning with `--all`, archive moves, and process restarts are safe because `usage.source_record_identity` is unique. Atomic identities use `response_id`; fallbacks hash stable source fields. A partial final line does not advance the offset.

If a parent edge appears after a child was imported, the child subtree and its existing usage ledger rows are reassigned to the resolved root in the same ingestion transaction. A missing referenced rollout marks the task accounting as partial or unavailable; its state-level token counter is reported as an evidence gap and is not converted into model or cost records.

## 4. Input and cached tokens

Observed Codex records show that `input_tokens` includes both cached-input and cache-write subsets. The normalized row computes:

```text
uncached_input_tokens = max(
    0,
    input_tokens - cached_input_tokens - cache_write_input_tokens
)
```

For older records without a cache-write field, cache write is zero and `input - cached` is billed at the ordinary input rate.

## 5. Reasoning tokens

Observed `reasoning_output_tokens` is a subset of `output_tokens`, while `total_tokens = input_tokens + output_tokens`. Reasoning is displayed separately for analysis, but output is charged once:

```text
output_cost = output_tokens × output_price
```

Reasoning tokens are never added to output or total tokens a second time.

## 6. Model attribution

Each usage record is attributed to the most recent `turn_context.payload.model` in that rollout. This supports thread-level model differences and model changes between turns. The state database's thread model is metadata/fallback, not proof that every response used that model. If no active model context exists, the record is stored as `unknown-model`; it is never assigned to the root model.

Provider is preserved. Built-in OpenAI API prices apply only to provider `openai`. Azure or other providers need their own explicit price records.

## 7. Root and subagent attribution

`thread_spawn_edges` is primary. Top-level rollout `session_meta.parent_thread_id`, structured `session_meta.source.subagent.thread_spawn.parent_thread_id`, and root `session_meta.session_id` fill absent state edges/grouping. Following parent IDs recursively assigns nested descendants to the stable root thread ID, which is also the dashboard task ID. `agent_path`, nickname, role, and depth are retained where present.

Rows marked as subagents but lacking a parent ID are marked orphaned. They are not linked to a root based only on timestamps.

## 8. Price selection and formulas

The event timestamp selects the row whose:

```text
model and provider match
effective_from <= timestamp
effective_until is null or timestamp < effective_until
```

Aliases are explicit in `model_aliases`. Unknown names are not silently mapped.

For an ordinary-context request:

```text
uncached_input_usd = uncached_input_tokens × input_per_million / 1,000,000
cached_input_usd = cached_input_tokens × cached_input_per_million / 1,000,000
cache_write_usd = cache_write_input_tokens × cache_write_per_million / 1,000,000
output_usd = output_tokens × output_per_million / 1,000,000
cost_usd = sum(the four components)
```

Official OpenAI model pages inspected on 2026-09-08 state that GPT-5.6 and GPT-6 Astra cache writes cost 1.25 times uncached input. Their prompts above 272,000 input tokens use 2× input/cache rates and 1.5× output for the full request. Because accounting is response-atomic, these multipliers are applied using that response's `input_tokens`.

Built-in sources:

- <https://developers.openai.com/api/docs/models/gpt-6-astra>
- <https://developers.openai.com/api/docs/models/gpt-5.6-sol>
- <https://developers.openai.com/api/docs/models/gpt-5.6-terra>
- <https://developers.openai.com/api/docs/models/gpt-5.6-luna>
- <https://developers.openai.com/api/docs/models/gpt-5.5>
- <https://developers.openai.com/api/docs/models/gpt-5.4>
- <https://developers.openai.com/api/docs/models/gpt-5.4-mini>
- <https://developers.openai.com/api/docs/models/gpt-5.4-pro>

The first built-in capture is versioned, but the 2026-07-01 effective start for the GPT-5.6 family is a **local-history coverage floor**, not a claim that the public price was first effective that day. It covers locally observed GPT-5.6 records beginning in July using the price verified on 2026-09-08. If authoritative older pricing differs, add an earlier row/boundary and rebuild. Astra begins at the actual capture date because no earlier local Astra usage was observed.

## 9. Unknown and omitted billing dimensions

If no price row matches, all component costs and `cost_usd` are null. The accounting metadata and exports retain that unknown state, and incomplete values are not used in averages or percentage comparisons. The UI deliberately displays only the numeric known subtotal, without uncertainty prefixes or text markers; an entirely unpriced value therefore displays as `$0.0000` while remaining unknown in the underlying accounting fields.

Normal persistence did not expose all possible server billing dimensions. The MVP does not estimate tool-call fees, Batch/Flex/Fast service tiers, regional-processing uplifts, credits, taxes, or server-side adjustments. It also cannot distinguish every historical cache-write billing policy when an older model page does not publish a write rate; built-in older-model rows conservatively use the normal input rate for writes.

Local totals should be reconciled with OpenAI organization Usage/Costs APIs or invoices in a future optional feature. Those APIs are not required by this dashboard.

## 10. OpenCode accounting

OpenCode assistant messages already contain token categories and a client-calculated cost. The dashboard imports that cost directly and does not apply its Codex price table. Cache reads and writes are added to OpenCode's uncached input, and reasoning is added to visible output, so the normalized token identities match Codex reports. See [data-sources.md](data-sources.md) for the exact mapping and read-only boundary.

## 11. Claude Code accounting

Claude Code transcript usage records contain model and token categories. The importer deduplicates streaming updates by `(sessionId, message.id)` and keeps the latest record, even when the same API message was copied into several subagent histories. Claude's `output_tokens` already includes thinking tokens; `thinking_tokens` is retained as a reasoning subset and is not added again. Claude `cost-state` records are cumulative, so consecutive snapshots are converted into dated cost and token changes whose sum equals the latest source total. This preserves historical period reporting without allocating cost to individual messages.

A root `cost-state` is Claude Code's own total for the whole session. Observed transcripts show models that occur only in subagent transcripts listed in the root's `modelUsage`, and cost-state token counts at or above the deduplicated root and subagent records (the difference is helper calls that never reach a transcript). When those counters are present, reports count the cost-state token rows and retain assistant rows only as per-call audit evidence. Every call of a session with a complete cost-state, root or subagent, that was recorded at or before the latest snapshot carries `cost_usd = 0` with the dollars on the cost-state rows, and the session is `complete`. Calls recorded after the latest snapshot (a resumed session, a subagent still running, or a session whose only snapshot is the zero-cost marker written at startup) are not in any snapshot. They count their own tokens and are priced like calls of a session without a cost-state, and the session is `estimated` when all of them are priced and `partial` otherwise, until a later snapshot covers them.

Direct Anthropic API and subscription sessions without any cost-state (Claude Code versions before about 2.1.246, and sessions that are still running) are priced per call from the built-in first-party Anthropic rows and marked `estimated`. An unclassified direct session is left unpriced rather than guessing whether it was API or subscription usage. A call never receives both: calls are estimated only when no cost-state snapshot covers them, and once a snapshot covering them appears the unit is reread and the estimates are replaced. A session whose cost-state reports an unknown model cost keeps that partial total and is not estimated. Fast-mode calls (`usage.speed == "fast"`) are left unpriced because they bill at a premium rate.

Each session records whether its last imported cost-state was complete (`sessions.cost_state_status`: `complete`, `partial`, or empty when the transcript has none). When a root transcript cannot be read completely on a later pass (an unreadable file, or a corrupt line) while other transcripts of the unit can, the stored snapshot rows stay authoritative for the calls up to their timestamp: a `complete` snapshot keeps the readable subagent calls at `cost_usd = 0`, a `partial` snapshot leaves them unpriced, and in neither case are list-price estimates added on top of the retained total. The unit's fingerprints are dropped so it is reread on the next pass, and the cost-state is replaced once the root is readable again. A final line that Claude Code is still writing (no trailing newline yet) is not an error; it is read once it is finished. When a complete directory scan shows the root transcript itself is gone while its subagent transcripts remain, the stored snapshots describe nothing left on disk: they are removed and the remaining calls are priced like an orphan unit without a cost-state.

Bedrock and Vertex sessions without a source cost-state are deliberately left unpriced. Partner-operated pricing can vary by provider, region, endpoint, and service tier, and the transcript does not prove all of those dimensions. A first-party Anthropic price is therefore not substituted for a cloud-provider price.

## 12. API backend and subscription billing

Each Claude row records the API backend detected from its identifiers (`vertex`, `bedrock`, `anthropic-oauth`, `anthropic-api`, or `anthropic`; see [data-sources.md](data-sources.md)). A Claude cost-state is authoritative for its whole session. Bedrock and Vertex calls are metered from that source total only; direct API calls can use either the source total or a first-party list-price estimate.

Subscription calls (`anthropic-oauth`) have no metered charge, so `billing_mode = 'subscription'`, `cost_usd = 0`, and the value the same calls would have cost on the API is stored in `equivalent_cost_usd`, again from the cost-state when one exists and from list prices otherwise. Real-spend totals exclude it; with the "Include subscription value" toggle (`include_subscription=1`, `--include-subscription`) every cost expression becomes `cost_usd + equivalent_cost_usd`, and a subscription row without an equivalent value counts as unknown.

A cost-state covering metered calls from more than one backend (for example Vertex and Bedrock) is stored as `mixed` and its dollars count as real spend. Its dollars and authoritative token counters remain a single whole-session total: they are never prorated across backends, models, messages, or agents. Consequently the snapshot and the calls it covers appear in **All** and **Mixed**, but not in a component backend view. Calls recorded after the latest mixed snapshot are outside it and carry their own backend, cost, and tokens, so they appear in their own backend view; the backend views therefore add up to **All**. When the snapshot spans subscription calls and metered calls, the total cannot be split into real spend and equivalent value, so its cost-state rows are stored with `billing_mode = 'unresolved'` and no `cost_usd` or `equivalent_cost_usd`; the calls themselves carry `cost_usd = 0` as covered calls, the session is `partial`, and both the real-spend and the subscription-equivalent totals report it as unknown rather than as $0. Newly imported cost-states also have unresolved billing whenever the unit scan is incomplete, even if every readable call appears metered: an unreadable child may contain subscription calls. The same applies when the unit has direct Anthropic calls whose billing is unverified (`anthropic`, no login profile); `--claude-billing` classifies them. Previously observed backends are retained, and the session is marked `partial` until the unit is readable and its billing can be classified. A previously imported snapshot retained because the root is unreadable keeps its stored classification. Agent rows show transcript-observed calls for audit only; when a cost-state exists, their token totals need not sum to the authoritative session counters and no per-agent cost is invented.

Built-in Anthropic prices captured 2026-09-15 from <https://platform.claude.com/docs/en/about-claude/pricing> (USD per million tokens; input / cache read / cache write / output):

| Model | Input | Cache read | Cache write (5m) | Output |
|---|---|---|---|---|
| claude-fable-5-1 | 10 | 0.25 | 12.5 | 50 |
| claude-fable-5 | 10 | 1 | 12.5 | 50 |
| claude-opus-5, -4-8, -4-7, -4-6, -4-5 | 5 | 0.5 | 6.25 | 25 |
| claude-sonnet-5 | 2 | 0.2 | 2.5 | 10 |
| claude-sonnet-4-6, -4-5 | 3 | 0.3 | 3.75 | 15 |
| claude-haiku-4-5 | 1 | 0.1 | 1.25 | 5 |

The cache-write column is the 5-minute rate (1.25× input). One-hour cache writes cost 2× input, so `cache_write_1h_input_tokens × 0.75 × input_per_million / 1,000,000` is added per record. Dated snapshot ids, Vertex `@date` spellings, and Bedrock inference-profile/version wrappers are canonicalized to their Anthropic model (`claude-sonnet-4-5-20250929`, `claude-haiku-4-5@20251001`, `us.anthropic.claude-opus-4-6-v1`, ...). A `[1m]` suffix is also stripped for lookup because 1M-context requests bill at the standard rate. This keeps assistant audit rows and authoritative cost-state rows under one model name. The `2025-09-01` effective start is a local-history coverage floor, not a launch date. `spenda price-add --provider anthropic` adds a row and reprices estimated or still-unpriced direct Anthropic calls that no cost-state snapshot covers; cost-state-covered rows keep their zero cost, calls included in a partial or unreadable cost-state stay unpriced, and Bedrock and Vertex calls stay unpriced. After repricing, a fully read session's accounting status is refreshed: `estimated` when every uncovered call is now priced, `partial` otherwise; a session with a partial or unresolved cost-state stays `partial`. A unit without complete import fingerprints keeps its previous accounting status because priced rows cannot account for unread transcript data.

## 13. Cursor accounting

Cursor bills by subscription and request, and its local history stores no
cost. Transcripts record no token counts either; editor bubbles carry a
`tokenCount` that Cursor populates only in some releases. The importer stores
one usage row per assistant message with the bubble-derived input and output
tokens when they can be aligned, and zero tokens otherwise. Rows with tokens
are priced from the price table for provider `cursor` (add rows with
`spenda price-add --provider cursor`); rows without tokens have no cost, and a
session with any such row is marked `partial` with the note "Cursor keeps no
token or cost accounting in its local history". Per-call timestamps come from editor
bubbles; without them every call of a session is dated at the session start.
See [data-sources.md](data-sources.md) for the exact read-only boundary.
