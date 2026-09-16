from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def cost_sql(include_subscription: bool, alias: str = "u") -> tuple[str, str]:
    """Return the per-row cost expression and its unknown-cost predicate.

    Real spend is ``cost_usd``.  Subscription rows carry a zero real cost and
    keep their list-price value in ``equivalent_cost_usd``; with
    ``include_subscription`` that value is added to the total and a missing
    value counts as unknown.
    """
    prefix = f"{alias}." if alias else ""
    if not include_subscription:
        return f"CAST({prefix}cost_usd AS REAL)", f"{prefix}cost_usd IS NULL"
    return (
        f"CAST({prefix}cost_usd AS REAL)+COALESCE(CAST({prefix}equivalent_cost_usd AS REAL),0)",
        f"({prefix}cost_usd IS NULL OR ({prefix}billing_mode='subscription'"
        f" AND {prefix}equivalent_cost_usd IS NULL))",
    )


def session_aggregate_sql(include_subscription: bool = False) -> str:
    cost, unknown = cost_sql(include_subscription, alias="")
    # ``estimated`` sessions are priced from list prices rather than a
    # source-reported total; they count as known cost, unlike ``partial``.
    return f"""
WITH agent_agg AS (
  SELECT session_id,COUNT(*) agent_count FROM agents GROUP BY session_id
), usage_agg AS (
  SELECT session_id,GROUP_CONCAT(DISTINCT model) models_used,
         GROUP_CONCAT(DISTINCT backend) backends_used,
         SUM(input_tokens) input_tokens,SUM(cached_input_tokens) cached_input_tokens,
         SUM(cache_write_input_tokens) cache_write_input_tokens,
         SUM(uncached_input_tokens) uncached_input_tokens,SUM(output_tokens) output_tokens,
         SUM(reasoning_output_tokens) reasoning_tokens,SUM(total_tokens) total_tokens,
         SUM({cost}) known_cost_usd,
         SUM(CAST(equivalent_cost_usd AS REAL)) equivalent_cost_usd,
         SUM(CASE WHEN {unknown} THEN 1 ELSE 0 END) unknown_cost_records,
         SUM(billing_mode='subscription' AND equivalent_cost_usd IS NULL) unknown_value_records,
         SUM(source_event_type!='claude_cost_state') usage_events
  FROM usage GROUP BY session_id
), tag_agg AS (
  SELECT st.session_id,GROUP_CONCAT(t.name) tags
  FROM session_tags st JOIN tags t ON t.id=st.tag_id GROUP BY st.session_id
)
SELECT s.*,
       COALESCE(a.agent_count,0) AS agent_count,
       u.models_used,
       u.backends_used,
       COALESCE(u.input_tokens,0) AS input_tokens,
       COALESCE(u.cached_input_tokens,0) AS cached_input_tokens,
       COALESCE(u.cache_write_input_tokens,0) AS cache_write_input_tokens,
       COALESCE(u.uncached_input_tokens,0) AS uncached_input_tokens,
       COALESCE(u.output_tokens,0) AS output_tokens,
       COALESCE(u.reasoning_tokens,0) AS reasoning_tokens,
       COALESCE(u.total_tokens,0) AS total_tokens,
       u.known_cost_usd,
       u.equivalent_cost_usd,
       COALESCE(u.unknown_cost_records,0)
         + CASE WHEN s.accounting_status NOT IN ('complete','estimated') THEN 1 ELSE 0 END AS unknown_cost_records,
       COALESCE(u.unknown_value_records,0) AS unknown_value_records,
       COALESCE(u.usage_events,0) AS usage_events,
       CAST(strftime('%s',s.updated_at)-strftime('%s',s.created_at) AS INTEGER) AS duration_seconds,
       t.tags
FROM sessions s
LEFT JOIN agent_agg a ON a.session_id=s.id
LEFT JOIN usage_agg u ON u.session_id=s.id
LEFT JOIN tag_agg t ON t.session_id=s.id
"""


SESSION_AGGREGATE = session_aggregate_sql()


def session_rows(
    conn: sqlite3.Connection,
    *,
    where: str = "1=1",
    params: tuple[Any, ...] = (),
    order: str = "started",
    direction: str = "desc",
    limit: int | None = None,
    include_subscription: bool = False,
) -> list[sqlite3.Row]:
    allowed_orders = {
        "time": "julianday(s.created_at)",
        "started": "julianday(s.created_at)",
        "source": "LOWER(s.source_app)",
        "title": "LOWER(COALESCE(s.title,''))",
        "project": "LOWER(COALESCE(s.repo_name,s.cwd,''))",
        "root_model": "LOWER(COALESCE(s.root_model,''))",
        "models": "LOWER(COALESCE(models_used,''))",
        "agents": "agent_count",
        "input": "input_tokens",
        "cached": "CASE WHEN input_tokens>0 THEN 1.0*cached_input_tokens/input_tokens ELSE 0 END",
        "output": "output_tokens",
        "tokens": "total_tokens",
        "total": "total_tokens",
        "cost": "COALESCE(known_cost_usd,0.0)",
        "duration": "duration_seconds",
    }
    expression = allowed_orders.get(order, allowed_orders["started"])
    order_direction = "ASC" if direction.lower() == "asc" else "DESC"
    sql = (
        session_aggregate_sql(include_subscription)
        + f" WHERE {where} ORDER BY {expression} {order_direction}, s.created_at DESC, s.id"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params = (*params, limit)
    return conn.execute(sql, params).fetchall()


def session_detail(
    conn: sqlite3.Connection, session_id: str, *, include_subscription: bool = False
) -> sqlite3.Row | None:
    rows = session_rows(
        conn, where="s.id=?", params=(session_id,), include_subscription=include_subscription
    )
    return rows[0] if rows else None


def format_tokens(value: int | None) -> str:
    number = int(value or 0)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(number) >= divisor:
            number_text = f"{number / divisor:.2f}".rstrip("0").rstrip(".")
            return f"{number_text}{suffix}"
    return f"{number:,}"


def format_cost(value: float | str | None, unknown: int = 0) -> str:
    amount = float(value or 0)
    digits = 4 if abs(amount) < 10 else 2
    return f"${amount:,.{digits}f}"


def format_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def iso_date(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def as_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["cost_usd"] = None if result.get("unknown_cost_records") else result.get("known_cost_usd")
    return result
