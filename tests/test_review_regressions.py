from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime

import pytest
from starlette.requests import Request

import spenda.web.app as web_module
from conftest import atomic, make_state, session_meta, thread, turn, usage_values, write_rollout
from spenda.cli import rebuild, tag_command
from spenda.config import Settings
from spenda.db import database, initialize
from spenda.ingestion.scanner import ingest
from spenda.pricing import add_price, reprice_usage
from spenda.reports import session_rows
from spenda.web.app import SESSION_SORT_KEYS, _model_style, _period_boundary, create_app


def _root(settings: Settings, *, model: str = "gpt-5.6-sol"):
    path = settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-root.jsonl"
    write_rollout(path, [session_meta("root"), turn("t", model), atomic("root", "t", "root-response")])
    state = make_state(settings.codex_home, [thread("root", path, model=model)])
    return state, path


def test_late_parent_edge_moves_existing_agent_usage(dashboard_settings):
    state, _ = _root(dashboard_settings)
    child_path = dashboard_settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-child.jsonl"
    source = {"subagent": {"other": "guardian"}}
    write_rollout(child_path, [session_meta("child", source), turn("ct"), atomic("child", "ct", "child-response")])
    item = thread("child", child_path, source=source)
    with sqlite3.connect(state) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(threads)")]
        conn.execute(
            f"INSERT INTO threads VALUES({','.join('?' for _ in columns)})",
            [item.get(column) for column in columns],
        )
    ingest(dashboard_settings)
    tag_command(dashboard_settings, "child", ["orphan-benchmark"])
    with sqlite3.connect(state) as conn:
        conn.execute("INSERT INTO thread_spawn_edges VALUES('root','child','open')")
    ingest(dashboard_settings)
    with database(dashboard_settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 2
        assert {row[0] for row in conn.execute("SELECT session_id FROM usage")} == {"root"}
        assert conn.execute(
            """SELECT COUNT(*) FROM session_tags st JOIN tags t ON t.id=st.tag_id
               WHERE st.session_id='root' AND t.name='orphan-benchmark'"""
        ).fetchone()[0] == 1


def test_rebuild_preserves_custom_prices_and_tags(dashboard_settings, capsys):
    _root(dashboard_settings, model="future-model")
    ingest(dashboard_settings)
    with database(dashboard_settings.database) as conn:
        add_price(
            conn,
            model="future-model",
            effective_from="2026-01-01T00:00:00Z",
            input_per_million="1",
            cached_input_per_million="0.1",
            cache_write_per_million="1",
            output_per_million="2",
            source="test",
        )
        reprice_usage(conn, provider="openai")
    tag_command(dashboard_settings, "root", ["baseline"])
    assert rebuild(dashboard_settings, True) == 0
    capsys.readouterr()
    with database(dashboard_settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM prices WHERE model='future-model'").fetchone()[0] == 1
        assert conn.execute("SELECT cost_usd FROM usage").fetchone()[0] is not None
        assert conn.execute(
            """SELECT COUNT(*) FROM session_tags st JOIN tags t ON t.id=st.tag_id
               WHERE st.session_id='root' AND t.name='baseline'"""
        ).fetchone()[0] == 1


def test_dashboard_database_cannot_be_inside_codex_home(dashboard_settings):
    state, _ = _root(dashboard_settings)
    unsafe = Settings(dashboard_settings.codex_home, state)
    with pytest.raises(ValueError, match="outside CODEX_HOME"):
        ingest(unsafe)
    with sqlite3.connect(state) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sessions" not in tables and "usage" not in tables


def test_existing_non_dashboard_database_is_rejected(tmp_path):
    path = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE user_data(value TEXT)")
    with pytest.raises(ValueError, match="non-dashboard"):
        initialize(path)
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"user_data"}


def test_missing_rollout_marks_accounting_unavailable(dashboard_settings):
    missing = dashboard_settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-missing.jsonl"
    item = thread("root", missing)
    item["tokens_used"] = 1_000_000
    make_state(dashboard_settings.codex_home, [item])
    ingest(dashboard_settings)
    with database(dashboard_settings.database, readonly=True) as conn:
        row = session_rows(conn)[0]
        assert row["accounting_status"] == "unavailable"
        assert row["unknown_cost_records"] > 0
        assert "1000000 state tokens" in row["accounting_note"]
        assert conn.execute("SELECT COUNT(*) FROM parser_warnings WHERE code='missing_rollout'").fetchone()[0] == 1


def test_period_boundary_uses_exact_utc_instant():
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    boundary = _period_boundary("7d", now)
    with sqlite3.connect(":memory:") as conn:
        included = conn.execute(
            "SELECT julianday('2026-09-01T01:00:00Z')>=julianday(?)", (boundary,)
        ).fetchone()[0]
    assert included == 0


def test_rollout_only_populates_root_metadata(tmp_path):
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    settings = Settings(home, tmp_path / "dashboard.sqlite", running_window_seconds=0)
    meta = session_meta("root")
    path = home / "sessions" / "rollout-root.jsonl"
    write_rollout(path, [meta, turn("turn", "gpt-5.6-terra"), atomic("root", "turn", "response")])
    ingest(settings)
    with database(settings.database, readonly=True) as conn:
        row = conn.execute(
            "SELECT created_at,root_model,root_reasoning_effort FROM sessions"
        ).fetchone()
    assert tuple(row) == (meta["timestamp"], "gpt-5.6-terra", "high")


def test_unknown_cost_is_ignored_in_trend_totals(dashboard_settings):
    _root(dashboard_settings, model="future-model")
    ingest(dashboard_settings)
    app = create_app(dashboard_settings)
    request = Request(
        {"type": "http", "method": "GET", "path": "/trends", "headers": [], "query_string": b"", "app": app}
    )
    route = next(route.endpoint for route in app.routes if route.path == "/trends")
    body = route(request).body.decode().split("<main>", 1)[1]
    assert "$0.0000" in body
    assert ">=$" not in body


def test_invalid_usage_timestamp_does_not_break_date_based_pages(dashboard_settings):
    _root(dashboard_settings)
    ingest(dashboard_settings)
    with database(dashboard_settings.database) as conn:
        conn.execute("UPDATE usage SET timestamp='not-a-timestamp'")
    app = create_app(dashboard_settings)

    for path in ("/", "/models", "/trends"):
        request = Request(
            {"type": "http", "method": "GET", "path": path, "headers": [],
             "query_string": b"", "app": app}
        )
        route = next(route.endpoint for route in app.routes if route.path == path)
        kwargs = {"period": "all"} if path == "/" else {}
        assert route(request, **kwargs).status_code == 200


def test_codex_ingest_accepts_json_non_finite_token_values(dashboard_settings):
    path = dashboard_settings.codex_home / "sessions" / "rollout-root.jsonl"
    values = usage_values(
        input_tokens=float("inf"), cached=float("nan"), write=0,
        output=float("-inf"), reasoning=float("nan"),
    )
    write_rollout(path, [session_meta("root"), turn("t"), atomic("root", "t", "response", values=values)])
    make_state(dashboard_settings.codex_home, [thread("root", path)])

    summary = ingest(dashboard_settings)

    assert summary.usage_records == 1
    with database(dashboard_settings.database, readonly=True) as conn:
        usage = conn.execute(
            "SELECT input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens "
            "FROM usage"
        ).fetchone()
    assert tuple(usage) == (0, 0, 0, 0, 0)


def test_tagging_unknown_session_returns_404_without_creating_tag(dashboard_settings):
    app = create_app(dashboard_settings)
    route = next(
        route.endpoint for route in app.routes
        if route.path == "/sessions/{session_id}/tags" and "POST" in route.methods
    )

    response = route("missing", tags="unexpected", source="all")

    assert response.status_code == 404
    with database(dashboard_settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0


def test_session_page_has_model_charts_and_readable_call_labels(dashboard_settings):
    _root(dashboard_settings)
    ingest(dashboard_settings)
    app = create_app(dashboard_settings)
    request = Request(
        {"type": "http", "method": "GET", "path": "/sessions/root", "headers": [], "query_string": b"", "app": app}
    )
    route = next(route.endpoint for route in app.routes if route.path == "/sessions/{session_id}")
    body = route(request, "root").body.decode()
    assert "Model composition" in body
    assert "Known cost" in body and "Tokens" in body and "Calls" in body
    assert "Call #001" in body and "root-response" in body
    assert _model_style("gpt-5.6-sol") == "sol"
    assert _model_style("future-model") == _model_style("future-model")


def test_claude_models_have_distinct_styles():
    models = (
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-haiku-4-5@20251001",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-sonnet-4-5-20250929",
    )

    assert len({_model_style(model) for model in models}) == len(models)
    assert _model_style("claude-opus-4-8@default") == _model_style("claude-opus-4-8")


def test_session_page_shows_safe_call_action_when_available(dashboard_settings):
    path = dashboard_settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-root.jsonl"
    action = {
        "timestamp": "2026-09-08T10:00:01.500Z",
        "type": "response_item",
        "ordinal": 2,
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "input": 'const r = await tools.exec_command({cmd:"uv run pytest"});',
        },
    }
    write_rollout(path, [session_meta("root"), turn("t"), action, atomic("root", "t", "response", ordinal=3)])
    make_state(dashboard_settings.codex_home, [thread("root", path)])
    ingest(dashboard_settings)
    with database(dashboard_settings.database, readonly=True) as conn:
        assert conn.execute("SELECT call_label FROM usage").fetchone()[0] == "Run tests"

    app = create_app(dashboard_settings)
    request = Request(
        {"type": "http", "method": "GET", "path": "/sessions/root", "headers": [], "query_string": b"", "app": app}
    )
    route = next(route.endpoint for route in app.routes if route.path == "/sessions/{session_id}")
    body = route(request, "root").body.decode()
    assert "Call #001 · Run tests" in body


def test_sessions_table_has_top_scrolling_compact_models_and_pinned_cost(dashboard_settings):
    _root(dashboard_settings)
    ingest(dashboard_settings)
    app = create_app(dashboard_settings)
    request = Request(
        {"type": "http", "method": "GET", "path": "/sessions", "headers": [], "query_string": b"", "app": app}
    )
    route = next(route.endpoint for route in app.routes if route.path == "/sessions")
    body = route(request).body.decode().split("<main>", 1)[1]

    assert 'class="table-scroll-top"' in body
    assert 'aria-label="Scroll columns right"' in body
    assert 'class="model-pill model-sol-text">gpt-5.6-sol</span>' in body
    assert 'class="cost-column ">' in body
    assert body.index("Duration</a>") < body.index("Cost</a>")


def test_task_sorting_uses_raw_numeric_values_for_every_column(dashboard_settings):
    directory = dashboard_settings.codex_home / "sessions" / "2026" / "09" / "08"
    state_rows = []
    for ident, title, tokens, cached in (
        ("small", "Zeta task", 900, 800),
        ("large", "Alpha task", 1_200, 0),
    ):
        path = directory / f"rollout-{ident}.jsonl"
        values = usage_values(
            input_tokens=tokens, cached=cached, write=0, output=0, reasoning=0
        )
        write_rollout(
            path,
            [
                session_meta(ident),
                turn(f"turn-{ident}"),
                atomic(ident, f"turn-{ident}", f"response-{ident}", values=values),
            ],
        )
        state_row = thread(ident, path)
        state_row["title"] = title
        state_row["cwd"] = f"/tmp/{ident}-project"
        state_rows.append(state_row)
    make_state(dashboard_settings.codex_home, state_rows)
    ingest(dashboard_settings)

    with database(dashboard_settings.database, readonly=True) as conn:
        for key in SESSION_SORT_KEYS:
            assert len(session_rows(conn, order=key, direction="asc")) == 2
        assert [row["id"] for row in session_rows(conn, order="total", direction="asc")] == [
            "small", "large"
        ]
        assert [row["id"] for row in session_rows(conn, order="cost", direction="desc")] == [
            "large", "small"
        ]
        assert [row["id"] for row in session_rows(conn, order="cached", direction="asc")] == [
            "large", "small"
        ]

    app = create_app(dashboard_settings)
    request = Request(
        {
            "type": "http", "method": "GET", "path": "/sessions", "headers": [],
            "query_string": b"sort=total&direction=asc", "app": app,
        }
    )
    route = next(route.endpoint for route in app.routes if route.path == "/sessions")
    body = route(request, sort="total", direction="asc").body.decode()
    assert body.index("/sessions/small") < body.index("/sessions/large")
    assert "aria-sort=\"ascending\"" in body

    home_request = Request(
        {
            "type": "http", "method": "GET", "path": "/", "headers": [],
            "query_string": b"period=all&sort=total&direction=asc", "app": app,
        }
    )
    home = next(route.endpoint for route in app.routes if route.path == "/")
    home_body = home(home_request, period="all", sort="total", direction="asc").body.decode()
    assert home_body.index("/sessions/small") < home_body.index("/sessions/large")


def test_background_ingestion_does_not_block_event_loop(dashboard_settings, monkeypatch):
    _root(dashboard_settings)
    app = create_app(dashboard_settings, ingest_interval=1)
    calls = 0

    def slow_ingest(*args, **kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.1)

    monkeypatch.setattr(web_module, "ingest", slow_ingest)

    async def exercise():
        gaps = []
        async with app.router.lifespan_context(app):
            previous = time.monotonic()
            end = previous + 1.2
            while time.monotonic() < end:
                await asyncio.sleep(0.01)
                current = time.monotonic()
                gaps.append(current - previous)
                previous = current
        return gaps

    gaps = asyncio.run(exercise())
    assert calls >= 2
    assert max(gaps) < 0.075
