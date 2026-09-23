from __future__ import annotations

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

BUILTIN_ALIASES = {"gpt-5.6": "gpt-5.6-sol"}


def seed_prices(conn: sqlite3.Connection) -> None:
    for row in BUILTIN_PRICES:
        conn.execute(
            """INSERT OR IGNORE INTO prices(
                model,provider,effective_from,input_per_million,
                cached_input_per_million,cache_write_per_million,output_per_million,
                long_context_threshold,long_input_multiplier,long_output_multiplier,source,notes
            ) VALUES(?, 'openai', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["model"], row["effective_from"], row["input"], row["cached"],
                row["write"], row["output"], row["threshold"], row["long_in"],
                row["long_out"], row["source"], row["notes"],
            ),
        )
    for alias, canonical in BUILTIN_ALIASES.items():
        conn.execute(
            "INSERT OR IGNORE INTO model_aliases(alias,canonical_model,provider) VALUES(?,?,'openai')",
            (alias, canonical),
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
    # OpenCode and Claude Code already provide source-owned accounting.  Never
    # overwrite their direct costs (or Claude's zero-cost call rows that are
    # represented by a separate cumulative cost-state record) with API prices.
    # Cursor rows without any token count are placeholders for responses the
    # source never measured; pricing them would present unknown usage as free.
    clauses, params = [
        "source_event_type NOT GLOB 'opencode_*'", "source_event_type NOT GLOB 'claude_*'",
        "NOT (source_event_type GLOB 'cursor_*' AND total_tokens=0)",
    ], []
    if model is not None:
        clauses.append("model=?")
        params.append(model)
    if provider is not None:
        clauses.append("provider=?")
        params.append(provider)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(f"SELECT * FROM usage{where}", params).fetchall()
    updated = 0
    for row in rows:
        usage = TokenUsage(
            input_tokens=row["input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            cache_write_input_tokens=row["cache_write_input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_output_tokens=row["reasoning_output_tokens"],
            total_tokens=row["total_tokens"],
        )
        cost = calculate_cost(conn, usage, row["model"], row["provider"], row["timestamp"])
        conn.execute(
            """UPDATE usage SET price_id=?,uncached_input_usd=?,cached_input_usd=?,
               cache_write_usd=?,output_usd=?,cost_usd=?,pricing_note=? WHERE id=?""",
            (
                cost.price_id,
                str(cost.uncached_input_usd) if cost.uncached_input_usd is not None else None,
                str(cost.cached_input_usd) if cost.cached_input_usd is not None else None,
                str(cost.cache_write_usd) if cost.cache_write_usd is not None else None,
                str(cost.output_usd) if cost.output_usd is not None else None,
                str(cost.total_usd) if cost.total_usd is not None else None,
                cost.note,
                row["id"],
            ),
        )
        updated += 1
    return updated
