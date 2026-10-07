"""Fill missing model prices from the public models.dev catalogue.

Codex rollouts record only token counts, so a model missing from the built-in
price table would count as free.  When stored usage has no price, the catalogue
is fetched (at once for a model not looked up before, otherwise at most once
per ``FETCH_INTERVAL``) and a price row is added for each model it lists.  A
fetched row is only inserted when no price covers the model's earliest unpriced
call, and it starts at that call, so it never takes over an existing interval.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .pricing import (
    find_price,
    no_fast_price_note,
    price_model,
    reprice_usage,
    repriceable_clauses,
    timestamp_instant,
    utc_timestamp,
)

log = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
FETCH_INTERVAL = timedelta(hours=6)
# The catalogue keys providers the same way usage rows do.
PROVIDERS = ("openai", "anthropic")
_META_KEY = "models_dev_checked_at"
_CHECKED_KEY = "models_dev_checked_models"

Catalogue = dict[str, Any]


def fetch_enabled() -> bool:
    """Return False when ``SPENDA_PRICE_FETCH`` disables network price lookups."""
    return os.environ.get("SPENDA_PRICE_FETCH", "1").strip().lower() not in {"0", "false", "no", "off"}


def fetch_models_dev(timeout: float = 30) -> Catalogue:
    request = urllib.request.Request(MODELS_DEV_URL, headers={"User-Agent": "spenda"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _rate(value: Any) -> Decimal | None:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return rate if rate.is_finite() and rate >= 0 else None


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def price_row(catalogue: Catalogue, provider: str, model: str, effective_from: str) -> dict[str, Any] | None:
    """Translate a models.dev cost entry into a ``BUILTIN_PRICES``-style row."""
    entry = ((catalogue.get(provider) or {}).get("models") or {}).get(model)
    cost = entry.get("cost") if isinstance(entry, dict) else None
    if not isinstance(cost, dict):
        return None
    rate_in, rate_out = _rate(cost.get("input")), _rate(cost.get("output"))
    if rate_in is None or rate_out is None:
        return None
    # A missing cached rate means no discount; a missing write rate follows the
    # built-in convention of billing cache writes at the ordinary input rate.
    cached = _rate(cost.get("cache_read"))
    write = _rate(cost.get("cache_write"))
    threshold, long_in, long_out = None, Decimal(1), Decimal(1)
    for tier in cost.get("tiers") or ():
        spec = tier.get("tier") if isinstance(tier, dict) else None
        if not isinstance(spec, dict) or spec.get("type") != "context" or not isinstance(spec.get("size"), int):
            continue
        tier_in, tier_out = _rate(tier.get("input")), _rate(tier.get("output"))
        if tier_in is None or tier_out is None or not rate_in or not rate_out:
            continue
        # The price table models one long-context tier as multipliers over the
        # whole request, which is how OpenAI and Anthropic bill it.
        threshold, long_in, long_out = spec["size"], tier_in / rate_in, tier_out / rate_out
        break
    # Fast mode (formerly priority processing) is one multiplier over every
    # standard rate; a model without a listed Fast price keeps such calls unpriced.
    priority = None
    fast = ((entry.get("experimental") or {}).get("modes") or {}).get("fast")
    fast_cost = fast.get("cost") if isinstance(fast, dict) else None
    if isinstance(fast_cost, dict) and rate_in:
        fast_in = _rate(fast_cost.get("input"))
        priority = None if fast_in is None else _plain(fast_in / rate_in)
    checked = datetime.now(UTC).date().isoformat()
    return {
        "model": model, "provider": provider, "effective_from": effective_from,
        "input": _plain(rate_in), "cached": _plain(rate_in if cached is None else cached),
        "write": _plain(rate_in if write is None else write), "output": _plain(rate_out),
        "threshold": threshold, "long_in": _plain(long_in), "long_out": _plain(long_out),
        "priority": priority, "source": f"{MODELS_DEV_URL}#{provider}/{model}",
        "notes": f"Fetched from models.dev {checked}; start is the first local call without a price.",
    }


def _unpriced(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[set[str], str]]:
    """Map ``(provider, price lookup name)`` to ``(stored model names, first use)``."""
    clauses, params = repriceable_clauses()
    placeholders = ",".join("?" for _ in PROVIDERS)
    # A Fast-mode call on a model without a Fast tier is unpriced although the
    # model has a price; repricing it on every pass would change nothing.
    no_fast = no_fast_price_note("")
    rows = conn.execute(
        f"SELECT DISTINCT provider,model,timestamp FROM usage WHERE {' AND '.join(clauses)} "
        f"AND price_id IS NULL AND provider IN ({placeholders}) AND model!='unknown-model' "
        "AND substr(COALESCE(pricing_note,''),1,?)!=?",
        (*params, *PROVIDERS, len(no_fast), no_fast),
    ).fetchall()
    found: dict[tuple[str, str], tuple[set[str], str]] = {}
    for provider, model, stamp in rows:
        instant = timestamp_instant(stamp)
        if instant is None:
            continue
        lookup = price_model(model) if provider == "anthropic" else model
        models, earliest = found.get((provider, lookup), (set(), stamp))
        models.add(model)
        # Ordered as instants: as strings "...02.500Z" sorts before "...02Z".
        found[(provider, lookup)] = (models, stamp if instant < timestamp_instant(earliest) else earliest)
    return found


def still_unpriced(conn: sqlite3.Connection, names: list[str]) -> set[str]:
    """Return the ``provider:model`` names that still have calls without any cost."""
    rows = conn.execute(
        "SELECT DISTINCT provider,model FROM usage WHERE price_id IS NULL "
        "AND cost_usd IS NULL AND equivalent_cost_usd IS NULL"
    ).fetchall()
    remaining = {f"{provider}:{price_model(model) if provider == 'anthropic' else model}" for provider, model in rows}
    return remaining & set(names)


def _reprice(conn: sqlite3.Connection, provider: str, models: set[str]) -> None:
    for model in sorted(models):
        reprice_usage(conn, model=model, provider=provider)


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM dashboard_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _due(conn: sqlite3.Connection, now: datetime, missing: set[str]) -> bool:
    """A lookup is due after the interval, or at once for a model not yet looked up."""
    try:
        checked = set(json.loads(_meta(conn, _CHECKED_KEY) or "[]"))
        return not missing <= checked or now - datetime.fromisoformat(_meta(conn, _META_KEY)) >= FETCH_INTERVAL
    except (TypeError, ValueError):
        return True


def fill_missing_prices(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    fetch_remote: bool = True,
    fetch: Callable[[], Catalogue] | None = None,
) -> list[str]:
    """Price stored usage whose model had no price; return ``provider:model`` rows added.

    Usage priced by a newly seeded built-in row is repriced too, so a price
    update in this package reaches calls that were ingested before it.
    """
    unpriced = _unpriced(conn)
    if not unpriced:
        return []
    missing = {}
    for (provider, lookup), (models, first) in unpriced.items():
        if find_price(conn, lookup, provider, first) is not None:
            _reprice(conn, provider, models)
        else:
            missing[(provider, lookup)] = (models, first)
    now = datetime.now(UTC)
    names = {f"{provider}:{lookup}" for provider, lookup in missing}
    if not missing or not fetch_remote or not (force or _due(conn, now, names)):
        return []
    # Record the attempt first so an outage or a model the catalogue never
    # lists does not trigger a download on every ingestion pass.
    conn.executemany(
        "INSERT INTO dashboard_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ((_META_KEY, now.isoformat()), (_CHECKED_KEY, json.dumps(sorted(names)))),
    )
    try:
        catalogue = (fetch or fetch_models_dev)()
    except (OSError, ValueError) as exc:
        # Keep the attempt and any repricing above; the next lookup waits for the interval.
        log.warning("models.dev price lookup failed: %s: %s", type(exc).__name__, exc)
        return []
    added = []
    for (provider, lookup), (models, first) in sorted(missing.items()):
        # Start exactly at the first uncovered call: no existing interval
        # contains it, so the new row cannot take over an earlier priced span.
        row = price_row(catalogue, provider, lookup, utc_timestamp(first))
        if row is None:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO prices(
                model,provider,effective_from,input_per_million,
                cached_input_per_million,cache_write_per_million,output_per_million,
                long_context_threshold,long_input_multiplier,long_output_multiplier,priority_multiplier,
                source,notes
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["model"], row["provider"], row["effective_from"], row["input"], row["cached"],
                row["write"], row["output"], row["threshold"], row["long_in"], row["long_out"],
                row["priority"], row["source"], row["notes"],
            ),
        )
        _reprice(conn, provider, models)
        added.append(f"{provider}:{lookup}")
    return added
