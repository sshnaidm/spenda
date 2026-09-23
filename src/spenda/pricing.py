from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

from .models import CostResult, TokenUsage

MILLION = Decimal(1_000_000)

# The coverage-floor dates are explicit so a future price update adds a new row
# instead of changing old estimates. See docs/accounting.md for the historical
# limitation of the first captured price set.
BUILTIN_PRICES = (
    {
        "model": "gpt-6-astra",
        "effective_from": "2026-09-08T00:00:00Z",
        "input": "10", "cached": "1", "write": "12.5", "output": "50",
        "threshold": 272000, "long_in": "2", "long_out": "1.5",
        "source": "https://developers.openai.com/api/docs/models/gpt-6-astra",
        "notes": "Official price captured 2026-09-08.",
    },
    {
        "model": "gpt-5.6-sol",
        "effective_from": "2026-07-01T00:00:00Z",
        "input": "4", "cached": "0.4", "write": "5", "output": "20",
        "threshold": 272000, "long_in": "2", "long_out": "1.5",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        "notes": "Official price captured 2026-09-08; start is a local-history coverage floor.",
    },
    {
        "model": "gpt-5.6-terra",
        "effective_from": "2026-07-01T00:00:00Z",
        "input": "2", "cached": "0.2", "write": "2.5", "output": "12",
        "threshold": 272000, "long_in": "2", "long_out": "1.5",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        "notes": "Official price captured 2026-09-08; start is a local-history coverage floor.",
    },
    {
        "model": "gpt-5.6-luna",
        "effective_from": "2026-07-01T00:00:00Z",
        "input": "0.2", "cached": "0.02", "write": "0.25", "output": "1.2",
        "threshold": 272000, "long_in": "2", "long_out": "1.5",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        "notes": "Official price captured 2026-09-08; start is a local-history coverage floor.",
    },
    {
        "model": "gpt-5.5",
        "effective_from": "2026-04-23T00:00:00Z",
        "input": "5", "cached": "0.5", "write": "5", "output": "30",
        "threshold": 272000, "long_in": "2", "long_out": "1.5",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.5",
        "notes": "Official price captured 2026-09-08; cache writes use ordinary input rate.",
    },
    {
        "model": "gpt-5.4",
        "effective_from": "2026-03-05T00:00:00Z",
        "input": "2.5", "cached": "0.25", "write": "2.5", "output": "15",
        "threshold": 272000, "long_in": "2", "long_out": "1.5",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.4",
        "notes": "Official price captured 2026-09-08; cache writes use ordinary input rate.",
    },
    {
        "model": "gpt-5.4-mini",
        "effective_from": "2026-03-17T00:00:00Z",
        "input": "0.75", "cached": "0.075", "write": "0.75", "output": "4.5",
        "threshold": None, "long_in": "1", "long_out": "1",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
        "notes": "Official price captured 2026-09-08; cache writes use ordinary input rate.",
    },
    {
        "model": "gpt-5.4-pro",
        "effective_from": "2026-03-05T00:00:00Z",
        "input": "30", "cached": "30", "write": "30", "output": "180",
        "threshold": 272000, "long_in": "2", "long_out": "1.5",
        "source": "https://developers.openai.com/api/docs/models/gpt-5.4-pro",
        "notes": "Official price captured 2026-09-08; no cached-input discount.",
    },
)

ANTHROPIC_PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
# Anthropic list prices apply to Vertex AI and Bedrock as well, so Claude Code
# rows on every backend are estimated from provider ``anthropic``.  The
# effective start is a local-history coverage floor, not a launch date.  Cache
# writes are the 5-minute rate (1.25x input); 1-hour writes (2x input) are
# uplifted per record from the transcript's cache_creation split.  1M-context
# models bill the full window at standard rates, so no long-context threshold.
_ANTHROPIC_NOTE = (
    "First-party Anthropic list price captured 2026-09-15. Cache write is the 5m rate; "
    "1h writes are uplifted at ingest. Partner-cloud, fast-mode, and server-tool prices are not modeled."
)
_ANTHROPIC_FROM = "2025-09-01T00:00:00Z"
BUILTIN_PRICES += tuple(
    {
        "model": model, "provider": "anthropic", "effective_from": _ANTHROPIC_FROM,
        "input": rates[0], "cached": rates[1], "write": rates[2], "output": rates[3],
        "threshold": None, "long_in": "1", "long_out": "1",
        "source": ANTHROPIC_PRICE_SOURCE, "notes": _ANTHROPIC_NOTE,
    }
    for model, rates in (
        ("claude-fable-5-1", ("10", "0.25", "12.5", "50")),
        ("claude-fable-5", ("10", "1", "12.5", "50")),
        ("claude-opus-5", ("5", "0.5", "6.25", "25")),
        ("claude-opus-4-8", ("5", "0.5", "6.25", "25")),
        ("claude-opus-4-7", ("5", "0.5", "6.25", "25")),
        ("claude-opus-4-6", ("5", "0.5", "6.25", "25")),
        ("claude-opus-4-5", ("5", "0.5", "6.25", "25")),
        ("claude-sonnet-5", ("2", "0.2", "2.5", "10")),
        ("claude-sonnet-4-6", ("3", "0.3", "3.75", "15")),
        ("claude-sonnet-4-5", ("3", "0.3", "3.75", "15")),
        ("claude-haiku-4-5", ("1", "0.1", "1.25", "5")),
    )
)

# (alias, canonical model, provider). Aliases stay explicit: dated snapshot ids
# and Vertex "@date" spellings are the same price as the bare model id.
BUILTIN_ALIASES = (
    ("gpt-5.6", "gpt-5.6-sol", "openai"),
    ("claude-opus-4-5-20251101", "claude-opus-4-5", "anthropic"),
    ("claude-opus-4-5@20251101", "claude-opus-4-5", "anthropic"),
    ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5", "anthropic"),
    ("claude-sonnet-4-5@20250929", "claude-sonnet-4-5", "anthropic"),
    ("claude-haiku-4-5-20251001", "claude-haiku-4-5", "anthropic"),
    ("claude-haiku-4-5@20251001", "claude-haiku-4-5", "anthropic"),
)

# Claude Code labels 1M-context requests with a "[1m]" suffix; the price is the
# model's standard rate across the full window.
_CONTEXT_SUFFIX = "[1m]"
# Fast-mode requests bill at a premium the price table does not carry; the
# note marks rows that must stay unpriced when standard prices are applied.
FAST_MODE_NOTE = "fast-mode request; standard list price not applicable"


_BEDROCK_MODEL_PREFIX = re.compile(r"^(?:(?:global|us|eu|apac)\.)?anthropic\.")
_BEDROCK_MODEL_VERSION = re.compile(r"-v\d+(?::\d+)?$")


def price_model(model: str) -> str:
    """Return a stable Anthropic/OpenAI model id for storage and lookup.

    Claude cost-state uses backend-specific names such as
    ``us.anthropic.claude-opus-4-6-v1`` while message records use
    ``claude-opus-4-6``.  Normalize those spellings before aggregation so one
    model does not appear as separate zero-call and zero-cost rows.
    """

    normalized = model[: -len(_CONTEXT_SUFFIX)] if model.endswith(_CONTEXT_SUFFIX) else model
    bedrock_model = _BEDROCK_MODEL_PREFIX.match(normalized) is not None
    normalized = _BEDROCK_MODEL_PREFIX.sub("", normalized)
    if bedrock_model:
        normalized = _BEDROCK_MODEL_VERSION.sub("", normalized)
    aliases = {
        alias: canonical for alias, canonical, provider in BUILTIN_ALIASES
        if provider == "anthropic"
    }
    return aliases.get(normalized, normalized)


def seed_prices(conn: sqlite3.Connection) -> None:
    for row in BUILTIN_PRICES:
        conn.execute(
            """INSERT OR IGNORE INTO prices(
                model,provider,effective_from,input_per_million,
                cached_input_per_million,cache_write_per_million,output_per_million,
                long_context_threshold,long_input_multiplier,long_output_multiplier,source,notes
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["model"], row.get("provider", "openai"), row["effective_from"], row["input"],
                row["cached"], row["write"], row["output"], row["threshold"], row["long_in"],
                row["long_out"], row["source"], row["notes"],
            ),
        )
    for alias, canonical, provider in BUILTIN_ALIASES:
        conn.execute(
            "INSERT OR IGNORE INTO model_aliases(alias,canonical_model,provider) VALUES(?,?,?)",
            (alias, canonical, provider),
        )


def _iso(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return value


def find_price(conn: sqlite3.Connection, model: str, provider: str, timestamp: str) -> sqlite3.Row | None:
    alias = conn.execute(
        "SELECT canonical_model FROM model_aliases WHERE alias=? AND provider=?", (model, provider)
    ).fetchone()
    canonical = alias[0] if alias else model
    stamp = _iso(timestamp)
    return conn.execute(
        """SELECT * FROM prices
           WHERE model=? AND provider=? AND effective_from<=?
             AND (effective_until IS NULL OR effective_until>?)
           ORDER BY effective_from DESC LIMIT 1""",
        (canonical, provider, stamp, stamp),
    ).fetchone()


def calculate_cost(
    conn: sqlite3.Connection,
    usage: TokenUsage,
    model: str,
    provider: str,
    timestamp: str,
) -> CostResult:
    price = find_price(conn, model, provider, timestamp)
    if price is None:
        return CostResult(None, None, None, None, None, None, "unknown price")
    input_multiplier = Decimal("1")
    output_multiplier = Decimal("1")
    threshold = price["long_context_threshold"]
    note = None
    if threshold is not None and usage.input_tokens > threshold:
        input_multiplier = Decimal(price["long_input_multiplier"])
        output_multiplier = Decimal(price["long_output_multiplier"])
        note = f"long-context multipliers applied above {threshold} input tokens"
    uncached = Decimal(usage.uncached_input_tokens) * Decimal(price["input_per_million"]) * input_multiplier / MILLION
    cached = (
        Decimal(usage.cached_input_tokens) * Decimal(price["cached_input_per_million"]) * input_multiplier / MILLION
    )
    write = (
        Decimal(usage.cache_write_input_tokens) * Decimal(price["cache_write_per_million"]) * input_multiplier / MILLION
    )
    output = Decimal(usage.output_tokens) * Decimal(price["output_per_million"]) * output_multiplier / MILLION
    total = uncached + cached + write + output
    return CostResult(price["id"], uncached, cached, write, output, total, note)


def estimate_cost(
    conn: sqlite3.Connection,
    usage: TokenUsage,
    model: str,
    provider: str,
    timestamp: str,
    *,
    cache_write_1h_tokens: int = 0,
) -> CostResult:
    """Price a record from list prices, uplifting 1-hour cache writes to 2x input.

    The price table stores the 5-minute cache-write rate (1.25x input); the
    1-hour subset costs 2x input, so the difference (0.75x input) is added for
    those tokens.
    """
    # Model-name canonicalization covers Anthropic spellings only; other
    # providers (e.g. Cursor) are priced by the exact name they were ingested with.
    lookup = price_model(model) if provider == "anthropic" else model
    cost = calculate_cost(conn, usage, lookup, provider, timestamp)
    if cost.price_id is None or cache_write_1h_tokens <= 0:
        return cost
    price = conn.execute("SELECT input_per_million FROM prices WHERE id=?", (cost.price_id,)).fetchone()
    uplift = Decimal(cache_write_1h_tokens) * Decimal(price[0]) * Decimal("0.75") / MILLION
    note = f"1h cache-write uplift applied to {cache_write_1h_tokens} tokens"
    return CostResult(
        cost.price_id, cost.uncached_input_usd, cost.cached_input_usd,
        cost.cache_write_usd + uplift, cost.output_usd, cost.total_usd + uplift,
        f"{cost.note}; {note}" if cost.note else note,
    )


# Notes on Claude call rows, shared by ingestion and repricing so both describe
# a row the same way.  Rows whose note starts with CLAUDE_NO_PRICE_NOTE are the
# only unpriced Claude calls that a new Anthropic price row may price.
CLAUDE_NO_PRICE_NOTE = "no built-in Anthropic price for "
CLAUDE_COVERED_NOTE = "cost represented by cumulative Claude Code cost-state"
CLAUDE_NO_COST_STATE_NOTE = "Claude Code transcript has no cumulative cost-state"
CLAUDE_LATE_CALLS_NOTE = "Claude Code cost-state predates later calls"


def claude_estimate_note(subscription: bool, cost_note: str | None) -> str:
    note = (
        "subscription usage: real cost $0; equivalent API value estimated from built-in Anthropic list price"
        if subscription
        else "estimated from built-in Anthropic list price; no complete cost-state covers this message"
    )
    return f"{note}; {cost_note}" if cost_note else note


def claude_no_price_note(model: str) -> str:
    return f"{CLAUDE_NO_PRICE_NOTE}{model}; no complete Claude Code cost-state covers this message"


def claude_estimate_status(base_note: str, priced: int, total: int) -> tuple[str, str]:
    """Describe a unit whose uncovered calls are priced from list prices."""

    if total and priced == total:
        return "estimated", f"{base_note}; priced from built-in Anthropic list prices"
    if priced:
        return "partial", f"{base_note}; some records have no built-in Anthropic price"
    return "partial", base_note


def add_price(
    conn: sqlite3.Connection,
    *,
    model: str,
    effective_from: str,
    input_per_million: str,
    cached_input_per_million: str,
    cache_write_per_million: str,
    output_per_million: str,
    source: str,
    provider: str = "openai",
    effective_until: str | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO prices(model,provider,effective_from,effective_until,
           input_per_million,cached_input_per_million,cache_write_per_million,
           output_per_million,source,notes) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (model, provider, effective_from, effective_until, input_per_million,
         cached_input_per_million, cache_write_per_million, output_per_million, source, notes),
    )


def reprice_usage(
    conn: sqlite3.Connection, *, model: str | None = None, provider: str | None = None
) -> int:
    """Recalculate stored audit rows after an effective-dated price change."""
    # OpenCode and Claude Code cost-state rows carry source-owned accounting and
    # are never repriced.  Claude call rows are repriced only when they were
    # estimated from list prices (or still unpriced); rows whose dollars are
    # represented by a cumulative cost-state record keep their zero cost, and
    # fast-mode rows stay unpriced because standard prices do not apply.
    # Cursor rows without any token count are placeholders for responses the
    # source never measured; pricing them would present unknown usage as free.
    clauses, params = [
        "source_event_type NOT GLOB 'opencode_*'",
        "source_event_type!='claude_cost_state'",
        # Ingestion records why a Claude call is unpriced; only calls it would
        # have estimated (no covering cost-state, no fast mode, a direct
        # Anthropic backend, no price found) may be priced by a new price row.
        "(source_event_type!='claude_assistant_message' OR price_id IS NOT NULL"
        " OR substr(COALESCE(pricing_note,''),1,?)=?)",
        "NOT (source_event_type GLOB 'cursor_*' AND total_tokens=0)",
        "(source_event_type!='claude_assistant_message' OR backend IN ('anthropic-api','anthropic-oauth'))",
        "COALESCE(pricing_note,'')!=?",
    ], [len(CLAUDE_NO_PRICE_NOTE), CLAUDE_NO_PRICE_NOTE, FAST_MODE_NOTE]
    if model is not None:
        clauses.append("model=?")
        params.append(model)
    if provider is not None:
        clauses.append("provider=?")
        params.append(provider)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(f"SELECT * FROM usage{where}", params).fetchall()
    updated = 0
    claude_sessions: set[str] = set()
    for row in rows:
        usage = TokenUsage(
            input_tokens=row["input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            cache_write_input_tokens=row["cache_write_input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_output_tokens=row["reasoning_output_tokens"],
            total_tokens=row["total_tokens"],
        )
        cost = estimate_cost(
            conn, usage, row["model"], row["provider"], row["timestamp"],
            cache_write_1h_tokens=int(row["cache_write_1h_input_tokens"] or 0),
        )
        conn.execute(
            """UPDATE usage SET price_id=?,uncached_input_usd=?,cached_input_usd=?,
               cache_write_usd=?,output_usd=?,cost_usd=?,equivalent_cost_usd=?,pricing_note=? WHERE id=?""",
            (
                cost.price_id,
                *(
                    str(value) if value is not None else None
                    for value in (cost.uncached_input_usd, cost.cached_input_usd, cost.cache_write_usd, cost.output_usd)
                ),
                *subscription_split(
                    str(cost.total_usd) if cost.total_usd is not None else None,
                    row["billing_mode"] == "subscription",
                ),
                (
                    claude_estimate_note(row["billing_mode"] == "subscription", cost.note)
                    if cost.price_id is not None
                    else claude_no_price_note(row["model"])
                ) if row["source_event_type"] == "claude_assistant_message" else cost.note,
                row["id"],
            ),
        )
        if row["source_event_type"] == "claude_assistant_message":
            claude_sessions.add(row["session_id"])
        updated += 1
    for session_id in claude_sessions:
        # Incomplete Claude units have their fingerprints removed so the next
        # ingestion retries them. Priced rows cannot prove that an unreadable
        # part of the transcript contains no further calls.
        incomplete = conn.execute(
            "SELECT 1 FROM agents a WHERE a.session_id=? AND a.source_kind='claude' "
            "AND (a.source_rollout_path IS NULL OR NOT EXISTS ("
            "SELECT 1 FROM ingestion_state state WHERE state.source_key='claude:'||a.source_rollout_path "
            "AND state.source_path=a.source_rollout_path)) LIMIT 1",
            (session_id,),
        ).fetchone()
        if incomplete:
            continue
        # A partial or unresolved cost-state keeps the session partial whatever
        # its later calls cost; only calls outside any cost-state are counted.
        cost_state = conn.execute(
            "SELECT COUNT(*),SUM(cost_usd IS NULL AND equivalent_cost_usd IS NULL) FROM usage "
            "WHERE session_id=? AND source_event_type='claude_cost_state'",
            (session_id,),
        ).fetchone()
        if cost_state[0] and (cost_state[1] or conn.execute(
            "SELECT 1 FROM sessions WHERE id=? AND COALESCE(cost_state_status,'')!='complete'", (session_id,)
        ).fetchone()):
            continue
        total, priced = conn.execute(
            "SELECT COUNT(*),SUM(price_id IS NOT NULL) FROM usage "
            "WHERE session_id=? AND source_event_type='claude_assistant_message' "
            "AND COALESCE(pricing_note,'')!=?",
            (session_id, CLAUDE_COVERED_NOTE),
        ).fetchone()
        status, note = claude_estimate_status(
            CLAUDE_LATE_CALLS_NOTE if cost_state[0] else CLAUDE_NO_COST_STATE_NOTE, int(priced or 0), total
        )
        conn.execute(
            "UPDATE sessions SET accounting_status=?,accounting_note=? "
            "WHERE id=? AND source_app='claude'",
            (status, note, session_id),
        )
    return updated


# ``billing_mode`` for snapshots with mixed subscription/metered calls or
# incomplete backend coverage: real spend and equivalent value cannot be split.
BILLING_UNRESOLVED = "unresolved"


def subscription_split(total: str | None, subscription: bool) -> tuple[str | None, str | None]:
    """Return ``(cost_usd, equivalent_cost_usd)`` for a priced total.

    Subscription usage has no metered charge, so its real cost is zero and the
    list-price value is kept separately as an equivalent API value.
    """
    return ("0", total) if subscription else (total, None)
