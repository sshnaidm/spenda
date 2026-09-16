from __future__ import annotations

import json
import sqlite3

from starlette.requests import Request

from conftest import atomic, make_state, session_meta, thread, turn, write_rollout
from spenda.cli import export_command
from spenda.config import Settings
from spenda.db import database
from spenda.ingestion.service import ingest_all
from spenda.web.app import create_app
from test_claude_ingestion import _fixture
from test_opencode_ingestion import _source_db


def _combined_settings(tmp_path):
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "09" / "08" / "rollout-codex-root.jsonl"
    rollout.parent.mkdir(parents=True)
    write_rollout(
        rollout,
        [
            session_meta("codex-root"),
            turn("codex-turn", "gpt-5.6-sol"),
            atomic("codex-root", "codex-turn", "codex-response"),
        ],
    )
    codex_thread = thread("codex-root", rollout, model="gpt-5.6-sol")
    codex_thread["title"] = "Synthetic Codex task"
    make_state(codex_home, [codex_thread])

    opencode_database = tmp_path / "opencode.sqlite"
    _source_db(opencode_database)
    with sqlite3.connect(opencode_database) as conn:
        conn.execute("UPDATE project SET name='Synthetic OpenCode project'")
        conn.execute("UPDATE session SET title='Synthetic OpenCode task' WHERE id='root'")
        conn.execute(
            "UPDATE session SET model=? WHERE id='root'",
            (json.dumps({"id": "opencode-model", "providerID": "provider"}),),
        )
        conn.execute(
            "UPDATE message SET data=json_set(data, '$.modelID', 'opencode-model')"
        )
    return Settings(
        codex_home, tmp_path / "dashboard.sqlite", running_window_seconds=0,
        opencode_database=opencode_database,
        claude_home=tmp_path / "missing-claude",
    )


def _page(app, path: str, source: str):
    route = next(route.endpoint for route in app.routes if route.path == path)
    request = Request(
        {
            "type": "http", "method": "GET", "path": path,
            "headers": [], "query_string": b"", "app": app,
        }
    )
    kwargs = {"source": source}
    if path == "/":
        kwargs["period"] = "all"
    return route(request, **kwargs).body.decode()


def _add_claude_session(
    settings, *, session_id: str = "claude:session", title: str = "Synthetic Claude task",
    project: str = "Synthetic Claude project", model: str = "claude-model", backend: str = "vertex",
    cost: str | None = "0.12", equivalent: str | None = None,
):
    billing = "subscription" if backend == "anthropic-oauth" else "metered"
    with database(settings.database) as conn:
        conn.execute(
            """INSERT INTO sessions(
                   id,root_thread_id,title,cwd,repo_name,created_at,updated_at,
                   root_model,root_provider,root_backend,source_app,source_home
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, session_id, title, "/work/claude",
                project, "2026-09-08T10:00:00Z", "2026-09-08T10:01:00Z",
                model, "anthropic", backend, "claude", str(settings.claude_home),
            ),
        )
        conn.execute(
            "INSERT INTO agents(thread_id,session_id,agent_role,source_kind,backend) VALUES(?,?,?,?,?)",
            (session_id, session_id, "root", "claude", backend),
        )
        conn.execute(
            """INSERT INTO usage(
                   source_record_identity,session_id,thread_id,timestamp,model,provider,backend,billing_mode,
                   input_tokens,cached_input_tokens,cache_write_input_tokens,uncached_input_tokens,
                   output_tokens,reasoning_output_tokens,total_tokens,source_file,source_event_type,cost_usd,
                   equivalent_cost_usd
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{session_id}:message", session_id, session_id, "2026-09-08T10:00:30Z",
                model, "anthropic", backend, billing, 100, 20, 0, 80, 30, 10, 130,
                str(settings.claude_home / "projects" / f"{session_id}.jsonl"), "claude_assistant_message",
                cost, equivalent,
            ),
        )


def test_combined_sources_are_available_in_every_source_filtered_report(tmp_path):
    settings = _combined_settings(tmp_path)
    summary = ingest_all(settings)
    assert (summary.root_sessions, summary.subagent_sessions) == (2, 2)
    _add_claude_session(settings)

    app = create_app(settings)
    health = next(route.endpoint for route in app.routes if route.path == "/healthz")
    assert health()["claude_home"] == str(settings.claude_home)
    # Each view needs to retain both source filters.  These two values occur
    # in the report body rather than in the shared navigation.
    pages = {
        "/": ("Synthetic Codex task", "Synthetic OpenCode task", "Synthetic Claude task"),
        "/sessions": ("Synthetic Codex task", "Synthetic OpenCode task", "Synthetic Claude task"),
        "/models": ("gpt-5.6-sol", "opencode-model", "claude-model"),
        "/projects": ("example-project", "Synthetic OpenCode project", "Synthetic Claude project"),
        "/trends": ("gpt-5.6-sol", "opencode-model", "claude-model"),
    }
    for path, (codex_value, opencode_value, claude_value) in pages.items():
        all_body = _page(app, path, "all")
        assert codex_value in all_body
        assert opencode_value in all_body
        assert claude_value in all_body

        codex_body = _page(app, path, "codex")
        opencode_body = _page(app, path, "opencode")
        claude_body = _page(app, path, "claude")
        assert codex_value in codex_body and opencode_value not in codex_body and claude_value not in codex_body
        assert (
            opencode_value in opencode_body and codex_value not in opencode_body and claude_value not in opencode_body
        )
        assert claude_value in claude_body and codex_value not in claude_body and opencode_value not in claude_body

    compare = next(route.endpoint for route in app.routes if route.path == "/compare")
    request = Request(
        {
            "type": "http", "method": "GET", "path": "/compare",
            "headers": [], "query_string": b"", "app": app,
        }
    )
    response = compare(
        request, session=["codex-root", "opencode:root", "claude:session"], source="claude"
    )
    assert [row["id"] for row in response.context["rows"]] == ["claude:session"]
    assert set(response.context["model_costs"]) == {"claude:session"}


def test_cli_export_filters_combined_sources(tmp_path, capsys):
    settings = _combined_settings(tmp_path)
    ingest_all(settings)
    _add_claude_session(settings)

    assert export_command(settings, "json", "sessions", "-", source="opencode") == 0
    exported = json.loads(capsys.readouterr().out)

    assert [(row["session_id"], row["source_app"]) for row in exported] == [
        ("opencode:root", "opencode")
    ]

    assert export_command(settings, "json", "sessions", "-", source="claude") == 0
    exported = json.loads(capsys.readouterr().out)
    assert [(row["session_id"], row["source_app"]) for row in exported] == [
        ("claude:session", "claude")
    ]


def test_codex_refresh_preserves_other_source_accounting_status(tmp_path):
    settings = _combined_settings(tmp_path)
    ingest_all(settings)
    _add_claude_session(settings)
    with database(settings.database) as conn:
        conn.execute(
            "UPDATE sessions SET accounting_status='partial',accounting_note='source-owned note' "
            "WHERE id='claude:session'"
        )

    ingest_all(settings)

    with database(settings.database, readonly=True) as conn:
        status = conn.execute(
            "SELECT accounting_status,accounting_note FROM sessions WHERE id='claude:session'"
        ).fetchone()
    assert tuple(status) == ("partial", "source-owned note")


def test_source_failure_is_reported_without_blocking_other_adapters(tmp_path):
    claude_home = tmp_path / "claude"
    _fixture(claude_home)
    opencode_database = tmp_path / "opencode.sqlite"
    with sqlite3.connect(opencode_database) as conn:
        conn.execute("CREATE TABLE incompatible(id TEXT)")
    settings = Settings(
        tmp_path / "codex", tmp_path / "dashboard.sqlite",
        opencode_database=opencode_database, claude_home=claude_home,
    )

    summary = ingest_all(settings)

    assert "opencode" in summary.source_errors
    assert summary.claude is not None
    with database(settings.database, readonly=True) as conn:
        claude_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_app='claude'"
        ).fetchone()[0]
    assert claude_sessions == 1


def _page_with(app, path: str, **query):
    route = next(route.endpoint for route in app.routes if route.path == path)
    query_string = "&".join(f"{key}={value}" for key, value in query.items()).encode()
    request = Request(
        {
            "type": "http", "method": "GET", "path": path,
            "headers": [], "query_string": query_string, "app": app,
        }
    )
    if path == "/":
        query.setdefault("period", "all")
    return route(request, **query).body.decode()


def test_backend_filter_and_subscription_toggle_in_every_report(tmp_path):
    settings = _combined_settings(tmp_path)
    ingest_all(settings)
    _add_claude_session(
        settings, session_id="claude:vertex", title="Synthetic Vertex task",
        project="Vertex project", model="vertex-model", backend="vertex", cost="0.12",
    )
    _add_claude_session(
        settings, session_id="claude:oauth", title="Synthetic subscription task",
        project="Subscription project", model="subscription-model", backend="anthropic-oauth",
        cost="0", equivalent="0.5",
    )
    app = create_app(settings)

    pages = {
        "/": ("Synthetic Vertex task", "Synthetic subscription task"),
        "/sessions": ("Synthetic Vertex task", "Synthetic subscription task"),
        "/models": ("vertex-model", "subscription-model"),
        "/projects": ("Vertex project", "Subscription project"),
        "/trends": ("vertex-model", "subscription-model"),
    }
    for path, (vertex_value, oauth_value) in pages.items():
        everything = _page_with(app, path, source="claude")
        assert vertex_value in everything and oauth_value in everything
        assert 'aria-label="Claude Code API backend"' in everything
        vertex_only = _page_with(app, path, source="claude", backend="vertex")
        assert vertex_value in vertex_only and oauth_value not in vertex_only
        oauth_only = _page_with(app, path, source="all", backend="anthropic-oauth")
        assert oauth_value in oauth_only and vertex_value not in oauth_only
        # Codex pages have no Claude backend and therefore no backend switch.
        assert 'aria-label="Claude Code API backend"' not in _page_with(app, path, source="codex")

    # Real spend excludes the subscription value; the toggle adds it.
    overview = _page_with(app, "/", source="claude", backend="anthropic-oauth")
    assert "$0.0000" in overview and "$0.5000" in overview and "Real spend" in overview
    included = _page_with(app, "/", source="claude", backend="anthropic-oauth", include_subscription=True)
    assert "Spend incl. subscription value" in included
    assert included.count("$0.5000") >= 2
    assert "include_subscription=1" in included

    sessions = _page_with(app, "/sessions", source="claude")
    assert "+$0.5000 sub." in sessions and "Claude subscription" in sessions and "Vertex AI" in sessions

    detail_route = next(route.endpoint for route in app.routes if route.path == "/sessions/{session_id}")
    request = Request(
        {"type": "http", "method": "GET", "path": "/sessions/x", "headers": [], "query_string": b"", "app": app}
    )
    detail = detail_route(request, "claude:oauth").body.decode()
    assert "Claude subscription / subscription (no metered charge)" in detail
    assert "Subscription equivalent" in detail and "eq.</span>" in detail
    detail_included = detail_route(request, "claude:oauth", include_subscription=True).body.decode()
    assert "Cost incl. subscription value" in detail_included


def test_cli_export_filters_by_backend_and_includes_subscription_value(tmp_path, capsys):
    settings = _combined_settings(tmp_path)
    ingest_all(settings)
    _add_claude_session(settings, session_id="claude:vertex", backend="vertex", cost="0.12")
    _add_claude_session(settings, session_id="claude:oauth", backend="anthropic-oauth", cost="0", equivalent="0.5")

    assert export_command(settings, "json", "sessions", "-", source="claude", backend="vertex") == 0
    vertex = json.loads(capsys.readouterr().out)
    assert [(row["session_id"], row["root_backend"], row["known_cost_usd"]) for row in vertex] == [
        ("claude:vertex", "vertex", "0.12")
    ]

    assert export_command(settings, "json", "sessions", "-", source="claude", backend="anthropic-oauth") == 0
    real = json.loads(capsys.readouterr().out)
    assert [(row["known_cost_usd"], row["equivalent_cost_usd"]) for row in real] == [("0", "0.5")]

    assert export_command(
        settings, "json", "models", "-", source="claude", backend="anthropic-oauth", include_subscription=True
    ) == 0
    included = json.loads(capsys.readouterr().out)
    assert [(row["backend"], row["billing_mode"], row["known_cost_usd"]) for row in included] == [
        ("anthropic-oauth", "subscription", "0.5")
    ]
