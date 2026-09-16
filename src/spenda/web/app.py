from __future__ import annotations

import hashlib
import logging
import statistics
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..db import database, initialize
from ..ingestion.claude_auth import BACKEND_LABELS, BACKEND_MIXED, BACKENDS
from ..ingestion.service import ingest_all as ingest
from ..pricing import seed_prices
from ..reports import (
    cost_sql,
    format_cost,
    format_duration,
    format_tokens,
    iso_date,
    session_detail,
    session_rows,
    token_sum_sql,
)

log = logging.getLogger(__name__)
TEMPLATE_DIR = Path(__file__).with_name("templates")
MODEL_STYLES = {
    "gpt-5.6-sol": "sol",
    "gpt-5.6-terra": "terra",
    "gpt-5.6-luna": "luna",
    "gpt-6-astra": "astra",
    "claude-opus-4-8": "blue",
    "claude-opus-5": "coral",
    "claude-haiku-4-5-20251001": "green",
    "claude-haiku-4-5@20251001": "green",
    "claude-haiku-4-5": "green",
    "claude-opus-4-6": "purple",
    "claude-opus-4-7": "indigo",
    "claude-sonnet-5": "orange",
    "claude-sonnet-4-5-20250929": "teal",
    "claude-sonnet-4-5": "teal",
    "claude-sonnet-4-6": "olive",
    "claude-fable-5-1": "pink",
    "claude-fable-5": "rust",
}
MODEL_PALETTE = (
    "blue", "coral", "green", "purple", "orange",
    "teal", "pink", "olive", "indigo", "rust",
)
SOURCES = ("all", "codex", "opencode", "claude")
SOURCE_LABELS = {"all": "All", "codex": "Codex", "opencode": "OpenCode", "claude": "Claude Code"}
# API backend filter for Claude Code rows; Codex and OpenCode rows have none.
BACKEND_FILTERS = ("all", *BACKENDS)
BACKEND_FILTER_LABELS = {"all": "All backends", **BACKEND_LABELS}
SESSION_SORT_KEYS = (
    "started", "source", "title", "project", "root_model", "models", "agents", "input",
    "cached", "output", "total", "cost", "duration",
)
SESSION_SORT_LABELS = {
    "started": "Started", "source": "Source", "title": "Task / title", "project": "Project",
    "root_model": "Root model", "models": "Models used", "agents": "Agents",
    "input": "Input", "cached": "Cached", "output": "Output", "total": "Total",
    "cost": "Cost", "duration": "Duration",
}
TEXT_SESSION_SORTS = {"source", "title", "project", "root_model", "models"}


def _model_style(model: str) -> str:
    normalized = model.removesuffix("@default")
    if style := MODEL_STYLES.get(normalized):
        return style

    # Keep unfamiliar model colors stable across page loads and server restarts.
    digest = hashlib.blake2s(normalized.encode("utf-8"), digest_size=2).digest()
    index = int.from_bytes(digest, byteorder="big") % len(MODEL_PALETTE)
    return MODEL_PALETTE[index]


def _source(value: str) -> str:
    return value if value in SOURCES else "all"


def _backend(value: str) -> str:
    return value if value in BACKEND_FILTERS else "all"


def _source_usage_clause(source: str, backend: str = "all", alias: str = "u") -> tuple[str, tuple[Any, ...]]:
    clauses, params = [], []
    if source != "all":
        clauses.append(
            f"EXISTS(SELECT 1 FROM sessions source_session WHERE source_session.id={alias}.session_id "
            "AND source_session.source_app=?)"
        )
        params.append(source)
    if backend != "all":
        clauses.append(f"{alias}.backend=?")
        params.append(backend)
        if backend != BACKEND_MIXED:
            clauses.append(
                f"NOT EXISTS(SELECT 1 FROM usage mixed_state "
                f"WHERE mixed_state.session_id={alias}.session_id "
                "AND mixed_state.source_event_type='claude_cost_state' "
                "AND mixed_state.backend='mixed')"
            )
    return " AND ".join(clauses) or "1=1", tuple(params)


def _backend_session_clause(backend: str, alias: str = "s") -> tuple[str, tuple[Any, ...]]:
    """Match sessions whose accounting can be attributed to the backend."""
    if backend == "all":
        return "1=1", ()
    match = f"EXISTS(SELECT 1 FROM usage ub WHERE ub.session_id={alias}.id AND ub.backend=?)"
    if backend == BACKEND_MIXED:
        return match, (backend,)
    # A cumulative mixed-backend cost-state cannot be split honestly. Keep it
    # in All/Mixed instead of making every component backend appear as $0.
    strict = (
        f" NOT EXISTS(SELECT 1 FROM usage um WHERE um.session_id={alias}.id "
        "AND um.source_event_type='claude_cost_state' AND um.backend='mixed')"
    )
    return f"{match} AND{strict}", (backend,)


def _query_suffix(source: str, backend: str, include_subscription: bool) -> str:
    """Query string that carries the current filters between pages."""
    items = [("source", source)]
    if backend != "all":
        items.append(("backend", backend))
    if include_subscription:
        items.append(("include_subscription", "1"))
    return urlencode(items)


def _strict_unknown_cost(unknown: str, usage_alias: str = "u") -> str:
    """Include source-level accounting gaps in an aggregate's unknown flag."""

    return (
        f"(({unknown}) OR EXISTS(SELECT 1 FROM sessions accounting_session "
        f"WHERE accounting_session.id={usage_alias}.session_id "
        "AND accounting_session.accounting_status NOT IN ('complete','estimated')))"
    )


def _model_composition(rows: list[Any]) -> dict[str, Any]:
    entries = []
    for row in rows:
        entries.append(
            {
                "model": row["model"],
                "style": _model_style(row["model"]),
                "cost": float(
                    (row["cost_usd"] if "cost_usd" in row.keys() else row["known_cost_usd"])
                    or 0
                ),
                "tokens": int(row["total_tokens"] or 0),
                "calls": int(row["usage_events"] or 0),
                "unknown_cost_records": int(row["unknown_cost_records"] or 0),
            }
        )
    totals = {
        "cost": sum(entry["cost"] for entry in entries),
        "tokens": sum(entry["tokens"] for entry in entries),
        "calls": sum(entry["calls"] for entry in entries),
    }
    return {"entries": entries, **totals}


def _sort_links(request: Request, current_sort: str, current_direction: str) -> dict[str, str]:
    # Every other query parameter (source, backend, filters) is preserved.
    base = [
        (key, value) for key, value in request.query_params.multi_items()
        if key not in {"sort", "direction"}
    ]
    links = {}
    for key in SESSION_SORT_KEYS:
        if key == current_sort:
            next_direction = "desc" if current_direction == "asc" else "asc"
        else:
            next_direction = "asc" if key in TEXT_SESSION_SORTS else "desc"
        links[key] = "?" + urlencode([*base, ("sort", key), ("direction", next_direction)])
    return links


def _period_boundary(period: str, now: datetime | None = None) -> str | None:
    local_now = (now or datetime.now().astimezone()).astimezone()
    if period == "all":
        return None
    if period == "today":
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "7d":
        start = local_now - timedelta(days=7)
    else:
        start = local_now - timedelta(days=30)
    return start.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _period_clause(period: str, source: str = "all", backend: str = "all") -> tuple[str, tuple[Any, ...]]:
    boundary = _period_boundary(period)
    clauses, params = [], []
    if boundary is not None:
        clauses.append("julianday(u.timestamp)>=julianday(?)")
        params.append(boundary)
    source_clause, source_params = _source_usage_clause(source, backend)
    clauses.append(source_clause)
    params.extend(source_params)
    return " AND ".join(clauses), tuple(params)


def _session_period(period: str, source: str = "all", backend: str = "all") -> tuple[str, tuple[Any, ...]]:
    boundary = _period_boundary(period)
    # Period and backend apply to the same usage row, so a session whose only
    # recent activity is on another backend does not match.
    usage = "EXISTS(SELECT 1 FROM usage up WHERE up.session_id=s.id"
    params: list[Any] = []
    if boundary is not None:
        usage += " AND julianday(up.timestamp)>=julianday(?)"
        params.append(boundary)
    if backend != "all":
        usage += " AND up.backend=?"
        params.append(backend)
        if backend != BACKEND_MIXED:
            usage += (
                " AND NOT EXISTS(SELECT 1 FROM usage um WHERE um.session_id=s.id "
                "AND um.source_event_type='claude_cost_state' AND um.backend='mixed')"
            )
    usage += ")"
    if source != "all":
        usage += " AND s.source_app=?"
        params.append(source)
    return usage, tuple(params)


def _overview(
    conn, period: str, sort: str = "started", direction: str = "desc", source: str = "all",
    backend: str = "all", include_subscription: bool = False,
) -> dict[str, Any]:
    usage_where, usage_params = _period_clause(period, source, backend)
    cost, unknown = cost_sql(include_subscription)
    strict_unknown = _strict_unknown_cost(unknown)
    total_tokens = token_sum_sql("total_tokens")
    input_tokens = token_sum_sql("input_tokens")
    cached_tokens = token_sum_sql("cached_input_tokens")
    usage = conn.execute(
        f"""SELECT COALESCE({total_tokens},0) tokens,COALESCE({input_tokens},0) input_tokens,
            COALESCE({cached_tokens},0) cached_tokens,SUM({cost}) known_cost,
            SUM({unknown}) unknown_cost_records,COUNT(DISTINCT session_id) sessions,
            COUNT(DISTINCT thread_id) agents,
            SUM(CAST(u.equivalent_cost_usd AS REAL)) subscription_value,
            SUM(u.billing_mode='subscription' AND u.equivalent_cost_usd IS NULL) unknown_value_records
            FROM usage u WHERE {usage_where}""",
        usage_params,
    ).fetchone()
    session_where, session_params = _session_period(period, source, backend)
    rows = session_rows(
        conn, where=session_where, params=session_params, order=sort, direction=direction,
        include_subscription=include_subscription, backend=backend,
        since=_period_boundary(period),
    )
    incomplete_sessions = sum(
        1 for row in rows if row["accounting_status"] not in ("complete", "estimated")
    )
    aggregate_unknown = int(usage["unknown_cost_records"] or 0) + incomplete_sessions
    known_session_costs = [
        float(r[0] or 0) for r in conn.execute(
            f"""SELECT SUM({cost}) FROM usage u
                JOIN sessions average_session ON average_session.id=u.session_id
                WHERE {usage_where} GROUP BY session_id
                HAVING SUM({unknown})=0
                AND MAX(average_session.accounting_status IN ('complete','estimated'))=1""",
            usage_params,
        ).fetchall()
    ]
    subagents = sum(max(0, int(r["agent_count"]) - 1) for r in rows)
    output_tokens = token_sum_sql("output_tokens")
    models = conn.execute(
        f"""SELECT model,GROUP_CONCAT(DISTINCT backend) backends,
            COUNT(DISTINCT session_id) sessions,COUNT(DISTINCT thread_id) agents,
            {input_tokens} input_tokens,{cached_tokens} cached_input_tokens,
            {output_tokens} output_tokens,{total_tokens} total_tokens,
            SUM(source_event_type!='claude_cost_state') usage_events,
            SUM({cost}) cost_usd,SUM({strict_unknown}) unknown_cost_records
            FROM usage u WHERE {usage_where} GROUP BY model ORDER BY cost_usd DESC""",
        usage_params,
    ).fetchall()
    total_known = float(usage["known_cost"] or 0)
    most_used = max(models, key=lambda r: r["total_tokens"] or 0)["model"] if models else "unknown"
    most_expensive = max(models, key=lambda r: r["cost_usd"] or 0)["model"] if models else "unknown"
    return {
        "tokens": usage["tokens"], "input_tokens": usage["input_tokens"],
        "cached_tokens": usage["cached_tokens"], "known_cost": total_known,
        "unknown_cost_records": aggregate_unknown,
        "subscription_value": float(usage["subscription_value"] or 0),
        "unknown_value_records": int(usage["unknown_value_records"] or 0),
        "sessions": len(rows), "agents": usage["agents"], "subagents": subagents,
        "average": statistics.fmean(known_session_costs) if known_session_costs and not aggregate_unknown else None,
        "median": statistics.median(known_session_costs) if known_session_costs and not aggregate_unknown else None,
        "cached_pct": 100 * usage["cached_tokens"] / usage["input_tokens"] if usage["input_tokens"] else 0,
        "most_used": most_used, "most_expensive": most_expensive,
        "models": models, "model_composition": _model_composition(models),
        "total_known": total_known, "session_rows": rows[:30],
    }


def _svg_trend(rows: list[Any], width: int = 900, height: int = 190) -> str:
    if not rows:
        return '<svg viewBox="0 0 900 190" role="img"><text x="20" y="95">No usage in this period</text></svg>'
    values = [float(r[1] or 0) for r in rows]
    maximum = max(values) or 1
    left, top, bottom = 48, 12, 32
    plot_w, plot_h = width - left - 12, height - top - bottom
    step = plot_w / max(1, len(rows) - 1)
    points = " ".join(
        f"{left + i * step:.1f},{top + plot_h - value / maximum * plot_h:.1f}"
        for i, value in enumerate(values)
    )
    labels = "".join(
        f'<text x="{left + i * step:.1f}" y="{height - 8}" text-anchor="middle">{str(row[0] or "")[5:]}</text>'
        for i, row in enumerate(rows) if i in {0, len(rows) - 1} or len(rows) <= 8
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Daily spend trend">'
        f'<line x1="{left}" y1="{top+plot_h}" x2="{width-12}" y2="{top+plot_h}" class="axis"/>'
        f'<polyline points="{points}" class="trend-line"/>'
        f'<text x="4" y="20">${maximum:.2f}</text>{labels}</svg>'
    )


def create_app(settings: Settings | None = None, *, ingest_interval: float = 10) -> FastAPI:
    settings = settings or Settings.load()
    settings.validate()
    initialize(settings.database)
    with database(settings.database) as conn:
        seed_prices(conn)

    def refresh() -> None:
        try:
            ingest(settings)
        except Exception:
            log.exception("Background ingestion failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()

        def loop() -> None:
            while not stop.is_set():
                refresh()
                stop.wait(max(1, ingest_interval))

        task = threading.Thread(target=loop, name="spenda-ingest", daemon=True)
        task.start()
        yield
        stop.set()
        task.join(timeout=5)
        if task.is_alive():
            log.warning("Background ingestion did not stop within five seconds")

    app = FastAPI(title="Spenda", lifespan=lifespan)
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters.update(
        tokens=format_tokens, cost=format_cost, duration=format_duration, isodate=iso_date,
        modelstyle=_model_style,
    )

    def render(request: Request, name: str, **context):
        source = _source(context.pop("source", "all"))
        backend = _backend(context.pop("backend", "all"))
        include_subscription = bool(context.pop("include_subscription", False))
        source_links = {
            item: str(request.url.include_query_params(source=item)) for item in SOURCES
        }
        backend_links = {
            item: str(request.url.include_query_params(backend=item)) for item in BACKEND_FILTERS
        }
        subscription_toggle_link = str(
            request.url.include_query_params(include_subscription="0" if include_subscription else "1")
        )
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "request": request, "source": source, "sources": SOURCES,
                "source_links": source_links, "source_labels": SOURCE_LABELS,
                "backend": backend, "backends": BACKEND_FILTERS, "backend_links": backend_links,
                "backend_labels": BACKEND_FILTER_LABELS,
                "include_subscription": include_subscription,
                "subscription_toggle_link": subscription_toggle_link,
                "nav_query": _query_suffix(source, backend, include_subscription),
                **context,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request, period: str = "30d", sort: str = "started", direction: str = "desc",
        source: str = "all", backend: str = "all", include_subscription: bool = False,
    ):
        source = _source(source)
        backend = _backend(backend)
        sort = sort if sort in SESSION_SORT_KEYS else "started"
        direction = "asc" if direction == "asc" else "desc"
        cost, _unknown = cost_sql(include_subscription)
        with database(settings.database, readonly=True) as conn:
            overview = _overview(conn, period, sort, direction, source, backend, include_subscription)
            usage_where, usage_params = _period_clause(period, source, backend)
            daily = conn.execute(
                f"SELECT date(timestamp,'localtime'),SUM({cost}) FROM usage u WHERE {usage_where} "
                "GROUP BY date(timestamp,'localtime') HAVING date(timestamp,'localtime') IS NOT NULL "
                "ORDER BY date(timestamp,'localtime')",
                usage_params,
            ).fetchall()
        return render(
            request, "home.html", active="home", source=source, backend=backend,
            include_subscription=include_subscription, period=period, data=overview,
            trend=_svg_trend(daily), refresh=10, sort_key=sort, sort_direction=direction,
            sort_links=_sort_links(request, sort, direction),
        )

    @app.get("/sessions", response_class=HTMLResponse)
    def sessions_page(
        request: Request, sort: str = "started", direction: str = "desc",
        start: str | None = None, end: str | None = None,
        project: str | None = None, model: str | None = None, root_model: str | None = None,
        contains_astra: bool = False, subagents: bool = False, min_cost: float | None = None,
        source: str = "all", backend: str = "all", include_subscription: bool = False,
    ):
        source = _source(source)
        backend = _backend(backend)
        sort = sort if sort in SESSION_SORT_KEYS else "started"
        direction = "asc" if direction == "asc" else "desc"
        cost, _unknown = cost_sql(include_subscription, alias="uf")
        clauses, params = ["1=1"], []
        if source != "all":
            clauses.append("s.source_app=?")
            params.append(source)
        backend_clause, backend_params = _backend_session_clause(backend)
        if backend_params:
            clauses.append(backend_clause)
            params.extend(backend_params)
        if start:
            clauses.append("date(s.created_at,'localtime')>=date(?)")
            params.append(start)
        if end:
            clauses.append("date(s.created_at,'localtime')<=date(?)")
            params.append(end)
        if project:
            clauses.append("COALESCE(s.repo_name,s.cwd)=?")
            params.append(project)
        if model:
            model_backend = "" if backend == "all" else " AND uf.backend=?"
            clauses.append(
                f"EXISTS(SELECT 1 FROM usage uf WHERE uf.session_id=s.id "
                f"AND uf.model=?{model_backend})"
            )
            params.append(model)
            if backend != "all":
                params.append(backend)
        if root_model:
            clauses.append("s.root_model=?")
            params.append(root_model)
        if contains_astra:
            astra_backend = "" if backend == "all" else " AND uf.backend=?"
            clauses.append(
                "EXISTS(SELECT 1 FROM usage uf WHERE uf.session_id=s.id "
                f"AND uf.model='gpt-6-astra'{astra_backend})"
            )
            if backend != "all":
                params.append(backend)
        if subagents:
            clauses.append(
                "EXISTS(SELECT 1 FROM agents af WHERE af.session_id=s.id AND af.parent_thread_id IS NOT NULL)"
            )
        if min_cost is not None:
            backend_cost = "" if backend == "all" else " AND uf.backend=?"
            clauses.append(
                f"COALESCE((SELECT SUM({cost}) FROM usage uf "
                f"WHERE uf.session_id=s.id{backend_cost}),0)>=?"
            )
            if backend != "all":
                params.append(backend)
            params.append(min_cost)
        with database(settings.database, readonly=True) as conn:
            rows = session_rows(
                conn, where=" AND ".join(clauses), params=tuple(params), order=sort,
                direction=direction, include_subscription=include_subscription, backend=backend,
            )
            source_sql, source_values = (("", ()) if source == "all" else (" AND source_app=?", (source,)))
            projects = [r[0] for r in conn.execute(
                "SELECT DISTINCT COALESCE(repo_name,cwd) FROM sessions "
                f"WHERE COALESCE(repo_name,cwd) IS NOT NULL{source_sql} ORDER BY 1", source_values
            )]
            usage_source, usage_values = _source_usage_clause(source, backend)
            models = [r[0] for r in conn.execute(
                f"SELECT DISTINCT model FROM usage u WHERE {usage_source} ORDER BY model", usage_values
            )]
            roots = [r[0] for r in conn.execute(
                "SELECT DISTINCT root_model FROM sessions WHERE root_model IS NOT NULL"
                f"{source_sql} ORDER BY root_model", source_values
            )]
        return render(
            request, "sessions.html", active="sessions", source=source, backend=backend,
            include_subscription=include_subscription, rows=rows, projects=projects,
            models=models, roots=roots, sort_key=sort, sort_direction=direction,
            sort_links=_sort_links(request, sort, direction), sort_labels=SESSION_SORT_LABELS,
        )

    @app.get("/sessions/{session_id}", response_class=HTMLResponse)
    def session_page(
        request: Request, session_id: str, backend: str = "all", include_subscription: bool = False,
    ):
        backend = _backend(backend)
        cost, unknown = cost_sql(include_subscription)
        input_tokens = token_sum_sql("input_tokens")
        cached_tokens = token_sum_sql("cached_input_tokens")
        output_tokens = token_sum_sql("output_tokens")
        reasoning_tokens = token_sum_sql("reasoning_output_tokens")
        total_tokens = token_sum_sql("total_tokens")
        with database(settings.database, readonly=True) as conn:
            session = session_detail(
                conn, session_id, include_subscription=include_subscription, backend=backend
            )
            # In the Mixed view the cost-state is the strict aggregate, but
            # the component call rows remain useful audit evidence. Show all
            # of them without attributing their cost to any backend or agent.
            filter_detail_backend = backend not in ("all", BACKEND_MIXED)
            backend_join = " AND u.backend=?" if filter_detail_backend else ""
            detail_params: tuple[Any, ...] = (
                (backend, session_id) if filter_detail_backend else (session_id,)
            )
            agents = conn.execute(
                f"""SELECT a.*,
                   COALESCE(SUM(u.id IS NOT NULL AND u.source_event_type!='claude_cost_state'),0) usage_events,
                   COALESCE(SUM(CASE WHEN u.source_event_type!='claude_cost_state'
                       THEN u.input_tokens ELSE 0 END),0) input_tokens,
                   COALESCE(SUM(CASE WHEN u.source_event_type!='claude_cost_state'
                       THEN u.cached_input_tokens ELSE 0 END),0) cached_input_tokens,
                   COALESCE(SUM(CASE WHEN u.source_event_type!='claude_cost_state'
                       THEN u.output_tokens ELSE 0 END),0) output_tokens,
                   COALESCE(SUM(CASE WHEN u.source_event_type!='claude_cost_state'
                       THEN u.reasoning_output_tokens ELSE 0 END),0) reasoning_tokens,
                   COALESCE(SUM(CASE WHEN u.source_event_type!='claude_cost_state'
                       THEN u.total_tokens ELSE 0 END),0) total_tokens,
                   SUM(CASE WHEN u.source_event_type!='claude_cost_state' THEN {cost} END) known_cost_usd,
                   SUM(u.id IS NOT NULL AND u.source_event_type!='claude_cost_state'
                       AND {unknown}) unknown_cost_records,
                   CAST(strftime('%s',COALESCE(MAX(u.timestamp),a.updated_at))
                        -strftime('%s',COALESCE(MIN(u.timestamp),a.created_at)) AS INTEGER) duration_seconds,
                   GROUP_CONCAT(DISTINCT u.model) models_used
                   FROM agents a LEFT JOIN usage u ON u.thread_id=a.thread_id{backend_join}
                   WHERE a.session_id=?
                   GROUP BY a.thread_id ORDER BY COALESCE(a.agent_path,'/root'),a.created_at""",
                detail_params,
            ).fetchall()
            usage_backend = " AND backend=?" if filter_detail_backend else ""
            usage_params: tuple[Any, ...] = (
                (session_id, backend) if filter_detail_backend else (session_id,)
            )
            model_rows = conn.execute(
                f"""SELECT model,GROUP_CONCAT(DISTINCT backend) backends,COUNT(DISTINCT thread_id) agents,
                   SUM(source_event_type!='claude_cost_state') usage_events,{input_tokens} input_tokens,
                   {cached_tokens} cached_input_tokens,{output_tokens} output_tokens,
                   {reasoning_tokens} reasoning_tokens,{total_tokens} total_tokens,
                   SUM({cost}) known_cost_usd,SUM({unknown}) unknown_cost_records
                   FROM usage u WHERE session_id=?{usage_backend}
                   GROUP BY model ORDER BY known_cost_usd DESC""",
                usage_params,
            ).fetchall()
            event_rows = conn.execute(
                f"SELECT * FROM usage WHERE session_id=?{usage_backend} ORDER BY timestamp,source_ordinal",
                usage_params,
            ).fetchall()
            session_has_cost_state = bool(conn.execute(
                "SELECT 1 FROM usage WHERE session_id=? AND source_event_type='claude_cost_state' LIMIT 1",
                (session_id,),
            ).fetchone())
            tags = [r[0] for r in conn.execute(
                "SELECT t.name FROM tags t JOIN session_tags st ON st.tag_id=t.id "
                "WHERE st.session_id=? ORDER BY t.name",
                (session_id,),
            )]
        if session is None:
            return HTMLResponse("Session not found", status_code=404)
        agent_rows = []
        for agent in agents:
            data = dict(agent)
            path = data.get("agent_path") or ("/root" if not data.get("parent_thread_id") else "/root/subagent")
            data["depth"] = max(0, path.strip("/").count("/"))
            agent_rows.append(data)
        events = []
        for number, event in enumerate(event_rows, 1):
            data = dict(event)
            marker = f"Call #{number:03d}" if data.get("response_id") else f"Usage event #{number:03d}"
            data["call_label"] = f"{marker} · {data['call_label']}" if data.get("call_label") else marker
            data["full_identity"] = data.get("response_id") or data["source_record_identity"]
            events.append(data)
        return render(
            request, "session.html", active="sessions", source=session["source_app"],
            backend=backend, include_subscription=include_subscription, session=session,
            agents=agent_rows,
            models=model_rows, model_composition=_model_composition(model_rows), events=events,
            tags=tags, session_has_unallocated_cost=session_has_cost_state,
            refresh=10 if session["status"] == "running" else None,
        )

    @app.post("/sessions/{session_id}/tags")
    def update_tags(
        session_id: str, tags: str = Form(""), source: str = "all", backend: str = "all",
        include_subscription: bool = False,
    ):
        names = sorted({part.strip() for part in tags.split(",") if part.strip()})
        with database(settings.database) as conn:
            if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                return HTMLResponse("Session not found", status_code=404)
            conn.execute("DELETE FROM session_tags WHERE session_id=?", (session_id,))
            for name in names:
                conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
                conn.execute("INSERT INTO session_tags SELECT ?,id FROM tags WHERE name=?", (session_id, name))
        query = _query_suffix(_source(source), _backend(backend), include_subscription)
        return RedirectResponse(f"/sessions/{session_id}?{query}", status_code=303)

    @app.get("/models", response_class=HTMLResponse)
    def models_page(
        request: Request, source: str = "all", backend: str = "all", include_subscription: bool = False,
    ):
        source = _source(source)
        backend = _backend(backend)
        source_where, source_params = _source_usage_clause(source, backend)
        cost, unknown = cost_sql(include_subscription)
        strict_unknown = _strict_unknown_cost(unknown)
        total_tokens = token_sum_sql("total_tokens")
        input_tokens = token_sum_sql("input_tokens")
        cached_tokens = token_sum_sql("cached_input_tokens")
        with database(settings.database, readonly=True) as conn:
            rows = conn.execute(
                f"""SELECT model,provider,backend,COUNT(DISTINCT session_id) sessions,
                   COUNT(DISTINCT thread_id) agents,
                   SUM(source_event_type!='claude_cost_state') usage_events,
                   {total_tokens} total_tokens,{input_tokens} input_tokens,
                   {cached_tokens} cached_input_tokens,
                   SUM({cost}) known_cost_usd,SUM({strict_unknown}) unknown_cost_records,
                   CASE WHEN SUM({strict_unknown})=0
                        THEN SUM({cost})/COUNT(DISTINCT session_id) END average_cost
                   FROM usage u WHERE {source_where} GROUP BY model,provider,backend
                   ORDER BY known_cost_usd DESC""",
                source_params,
            ).fetchall()
            daily = conn.execute(
                f"""SELECT date(timestamp,'localtime'),model,SUM({cost}),SUM({strict_unknown})
                   FROM usage u WHERE {source_where} GROUP BY date(timestamp,'localtime'),model
                   HAVING date(timestamp,'localtime') IS NOT NULL ORDER BY 1,2""",
                source_params,
            ).fetchall()
        return render(
            request, "models.html", active="models", source=source, backend=backend,
            include_subscription=include_subscription, rows=rows, daily=daily,
            model_composition=_model_composition(rows),
        )

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(
        request: Request, source: str = "all", backend: str = "all", include_subscription: bool = False,
    ):
        source = _source(source)
        backend = _backend(backend)
        source_where = "1=1" if source == "all" else "s.source_app=?"
        source_params: tuple[Any, ...] = () if source == "all" else (source,)
        # Rows are joined per usage record, so the backend filter applies to
        # the record rather than to the whole session.
        if backend != "all":
            source_where += " AND u.backend=?"
            source_params = (*source_params, backend)
            if backend != BACKEND_MIXED:
                source_where += (
                    " AND NOT EXISTS(SELECT 1 FROM usage mixed_state "
                    "WHERE mixed_state.session_id=s.id "
                    "AND mixed_state.source_event_type='claude_cost_state' "
                    "AND mixed_state.backend='mixed')"
                )
        cost, unknown = cost_sql(include_subscription)
        strict_unknown = f"(({unknown}) OR s.accounting_status NOT IN ('complete','estimated'))"
        total_tokens = token_sum_sql("total_tokens")
        input_tokens = token_sum_sql("input_tokens")
        cached_tokens = token_sum_sql("cached_input_tokens")
        with database(settings.database, readonly=True) as conn:
            rows = conn.execute(
                f"""SELECT COALESCE(s.repo_name,s.cwd,'unknown') project,COUNT(DISTINCT s.id) sessions,
                   {total_tokens} total_tokens,SUM({cost}) known_cost_usd,
                   SUM(u.id IS NOT NULL AND {strict_unknown}) unknown_cost_records,
                   CASE WHEN SUM(u.id IS NOT NULL AND {strict_unknown})=0
                        THEN SUM({cost})/COUNT(DISTINCT s.id) END average_cost,
                   COUNT(DISTINCT u.thread_id) agents,
                   COALESCE(SUM(u.id IS NOT NULL AND u.source_event_type!='claude_cost_state'),0) usage_events,
                   100.0*{cached_tokens}/NULLIF({input_tokens},0) cached_pct
                   FROM sessions s LEFT JOIN usage u ON u.session_id=s.id WHERE {source_where}
                   GROUP BY project ORDER BY known_cost_usd DESC""",
                source_params,
            ).fetchall()
        return render(
            request, "projects.html", active="projects", source=source, backend=backend,
            include_subscription=include_subscription, rows=rows,
        )

    @app.get("/trends", response_class=HTMLResponse)
    def trends_page(
        request: Request, source: str = "all", backend: str = "all", include_subscription: bool = False,
    ):
        source = _source(source)
        backend = _backend(backend)
        source_where, source_params = _source_usage_clause(source, backend)
        cost, unknown = cost_sql(include_subscription)
        strict_unknown = _strict_unknown_cost(unknown)
        total_tokens = token_sum_sql("total_tokens")
        input_tokens = token_sum_sql("input_tokens")
        cached_tokens = token_sum_sql("cached_input_tokens")
        with database(settings.database, readonly=True) as conn:
            daily = conn.execute(
                f"""SELECT date(timestamp,'localtime') period,SUM({cost}) cost,
                   {total_tokens} tokens,COUNT(DISTINCT session_id) sessions,
                   SUM({strict_unknown}) unknown_cost_records,
                   100.0*{cached_tokens}/NULLIF({input_tokens},0) cached_pct,
                   CASE WHEN SUM({strict_unknown})=0
                        THEN SUM({cost})/COUNT(DISTINCT session_id) END average_cost
                   FROM usage u WHERE {source_where} GROUP BY date(timestamp,'localtime')
                   HAVING period IS NOT NULL ORDER BY period""",
                source_params,
            ).fetchall()
            weekly = conn.execute(
                f"""SELECT strftime('%Y-W%W',timestamp,'localtime') period,SUM({cost}) cost,
                   {total_tokens} tokens,COUNT(DISTINCT session_id) sessions,
                   SUM({strict_unknown}) unknown_cost_records,
                   100.0*{cached_tokens}/NULLIF({input_tokens},0) cached_pct,
                   CASE WHEN SUM({strict_unknown})=0
                        THEN SUM({cost})/COUNT(DISTINCT session_id) END average_cost
                   FROM usage u WHERE {source_where} GROUP BY strftime('%Y-W%W',timestamp,'localtime')
                   HAVING period IS NOT NULL ORDER BY period""",
                source_params,
            ).fetchall()
            by_model = conn.execute(
                f"""SELECT date(timestamp,'localtime') period,model,SUM({cost}) cost,
                   {total_tokens} tokens,SUM({strict_unknown}) unknown_cost_records
                   FROM usage u WHERE {source_where} GROUP BY date(timestamp,'localtime'),model
                   HAVING period IS NOT NULL ORDER BY period,model""",
                source_params,
            ).fetchall()
        return render(
            request, "trends.html", active="trends", source=source, backend=backend,
            include_subscription=include_subscription, daily=daily, weekly=weekly,
            by_model=by_model, spend_svg=_svg_trend(daily),
        )

    @app.get("/compare", response_class=HTMLResponse)
    def compare_page(
        request: Request, session: list[str] = Query(default=[]), source: str = "all",
        backend: str = "all", include_subscription: bool = False,
    ):
        source = _source(source)
        backend = _backend(backend)
        selected = session[:4]
        cost, unknown = cost_sql(include_subscription)
        strict_unknown = _strict_unknown_cost(unknown)
        with database(settings.database, readonly=True) as conn:
            rows = [
                session_detail(
                    conn, sid, include_subscription=include_subscription, backend=backend
                )
                for sid in selected
            ]
            rows = [
                row for row in rows
                if row is not None and (source == "all" or row["source_app"] == source)
            ]
            selected = [row["id"] for row in rows]
            model_backend = "" if backend == "all" else " AND backend=?"
            model_costs = {
                sid: {r[0]: (r[1], r[2]) for r in conn.execute(
                    f"""SELECT model,SUM({cost}),SUM({strict_unknown})
                       FROM usage u WHERE session_id=?{model_backend} GROUP BY model""",
                    (sid,) if backend == "all" else (sid, backend),
                )} for sid in selected
            }
            compared_models = sorted({model for costs in model_costs.values() for model in costs})
        return render(
            request, "compare.html", active="compare", source=source, backend=backend,
            include_subscription=include_subscription, rows=rows,
            model_costs=model_costs, compared_models=compared_models,
        )

    @app.get("/healthz")
    def health():
        return {
            "status": "ok", "database": str(settings.database),
            "codex_home": str(settings.codex_home), "opencode_database": str(settings.opencode_database),
            "claude_home": str(settings.claude_home),
        }

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app
