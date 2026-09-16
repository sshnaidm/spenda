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

If no price row matches, all component costs and `cost_usd` are null. Reports omit those records from dollar totals; a group containing only unpriced records is displayed as `$0.0000`.

Normal persistence did not expose all possible server billing dimensions. The MVP does not estimate tool-call fees, Batch/Flex/Fast service tiers, regional-processing uplifts, credits, taxes, or server-side adjustments. It also cannot distinguish every historical cache-write billing policy when an older model page does not publish a write rate; built-in older-model rows conservatively use the normal input rate for writes.

Local totals should be reconciled with OpenAI organization Usage/Costs APIs or invoices in a future optional feature. Those APIs are not required by this dashboard.

## 10. OpenCode accounting

OpenCode assistant messages already contain token categories and a client-calculated cost. The dashboard imports that cost directly and does not apply its Codex price table. Cache reads and writes are added to OpenCode's uncached input, and reasoning is added to visible output, so the normalized token identities match Codex reports. See [data-sources.md](data-sources.md) for the exact mapping and read-only boundary.

## 11. Claude Code accounting

Claude Code transcript usage records contain model and token categories. The importer deduplicates streaming updates by `(sessionId, message.id)` and keeps the latest record. Claude's `output_tokens` already includes thinking tokens; `thinking_tokens` is retained as a reasoning subset and is not added again. Claude `cost-state` records are cumulative, so consecutive snapshots are converted into dated cost changes whose sum equals the latest source total. This preserves historical period reporting without allocating cost to individual messages.

A root `cost-state` is Claude Code's own total for the whole session. Observed transcripts show models that occur only in subagent transcripts listed in the root's `modelUsage`, and cost-state token counts at or above the sum of root and subagent records (the difference is helper calls that never reach a transcript). Every call of a session with a complete cost-state, root or subagent, therefore carries `cost_usd = 0` with the dollars on the cost-state rows, and the session is `complete`.

Sessions without any cost-state (Claude Code versions before about 2.1.246, and sessions that are still running) are priced per call from the built-in Anthropic price rows and marked `estimated`. A session never receives both: calls are estimated only when no cost-state exists for the unit, and once a cost-state appears the unit is reread and the estimates are replaced. A session whose cost-state reports an unknown model cost keeps that partial total and is not estimated. Fast-mode calls (`usage.speed == "fast"`) are left unpriced because they bill at a premium rate.

The built-in Anthropic rows (see section 12) apply to every backend because Vertex AI and Bedrock list the same per-token prices; the cost-state totals observed on Vertex AI reproduce those rates exactly for Opus 4.8 and Haiku 4.5. Regional-endpoint premiums, `inference_geo` multipliers, web-search fees, and helper calls that are absent from transcripts are not modeled, so estimates are lower bounds.

## 12. API backend and subscription billing

Each Claude row records the API backend detected from its identifiers (`vertex`, `bedrock`, `anthropic-oauth`, `anthropic-api`, or `anthropic`; see [data-sources.md](data-sources.md)). Vertex AI, Bedrock, and API-key calls are metered: their `cost_usd` is the cost-state change or the list-price estimate.

Subscription calls (`anthropic-oauth`) have no metered charge, so `billing_mode = 'subscription'`, `cost_usd = 0`, and the value the same calls would have cost on the API is stored in `equivalent_cost_usd`, again from the cost-state when one exists and from list prices otherwise. Real-spend totals exclude it; with the "Include subscription value" toggle (`include_subscription=1`, `--include-subscription`) every cost expression becomes `cost_usd + equivalent_cost_usd`, and a subscription row without an equivalent value counts as unknown. A session that mixed backends keeps its cost-state rows metered because the cumulative total cannot be split.

Built-in Anthropic prices captured 2026-09-15 from <https://platform.claude.com/docs/en/about-claude/pricing> (USD per million tokens; input / cache read / cache write / output):

| Model | Input | Cache read | Cache write (5m) | Output |
|---|---|---|---|---|
| claude-fable-5-1 | 10 | 0.25 | 12.5 | 50 |
| claude-fable-5 | 10 | 1 | 12.5 | 50 |
| claude-opus-5, -4-8, -4-7, -4-6, -4-5 | 5 | 0.5 | 6.25 | 25 |
| claude-sonnet-5 | 2 | 0.2 | 2.5 | 10 |
| claude-sonnet-4-6, -4-5 | 3 | 0.3 | 3.75 | 15 |
| claude-haiku-4-5 | 1 | 0.1 | 1.25 | 5 |

The cache-write column is the 5-minute rate (1.25× input). One-hour cache writes cost 2× input, so `cache_write_1h_input_tokens × 0.75 × input_per_million / 1,000,000` is added per record. Dated snapshot ids and Vertex `@date` spellings (`claude-sonnet-4-5-20250929`, `claude-haiku-4-5@20251001`, ...) are explicit aliases; a `[1m]` suffix is stripped for lookup because 1M-context requests bill at the standard rate. The `2025-09-01` effective start is a local-history coverage floor, not a launch date. `spenda price-add --provider anthropic` adds a row and reprices estimated Claude calls only; cost-state-covered rows keep their zero cost.
