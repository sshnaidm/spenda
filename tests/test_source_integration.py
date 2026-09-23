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
        cursor_home=tmp_path / "missing-cursor", cursor_user_dir=tmp_path / "missing-cursor-user",
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


def _add_session(settings, source: str, *, title: str, project: str, model: str, provider: str, home, event: str):
    session_id = f"{source}:session"
    with database(settings.database) as conn:
        conn.execute(
            """INSERT INTO sessions(
                   id,root_thread_id,title,cwd,repo_name,created_at,updated_at,
                   root_model,root_provider,source_app,source_home
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, session_id, title, f"/work/{source}", project, "2026-09-08T10:00:00Z",
                "2026-09-08T10:01:00Z", model, provider, source, str(home),
            ),
        )
        conn.execute(
            "INSERT INTO agents(thread_id,session_id,agent_role,source_kind) VALUES(?,?,?,?)",
            (session_id, session_id, "root", source),
        )
        conn.execute(
            """INSERT INTO usage(
                   source_record_identity,session_id,thread_id,timestamp,model,provider,
                   input_tokens,cached_input_tokens,cache_write_input_tokens,uncached_input_tokens,
                   output_tokens,reasoning_output_tokens,total_tokens,source_file,source_event_type,cost_usd
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"{source}:message", session_id, session_id, "2026-09-08T10:00:30Z",
                model, provider, 100, 20, 0, 80, 30, 10, 130,
                str(home / "projects" / "session.jsonl"), event, "0.12",
            ),
        )


def _add_claude_session(settings):
    _add_session(
        settings, "claude", title="Synthetic Claude task", project="Synthetic Claude project",
        model="claude-model", provider="anthropic", home=settings.claude_home, event="claude_assistant_message",
    )


def _add_cursor_session(settings):
    _add_session(
        settings, "cursor", title="Synthetic Cursor task", project="Synthetic Cursor project",
        model="cursor-model", provider="cursor", home=settings.cursor_home, event="cursor_assistant_message",
    )


def test_combined_sources_are_available_in_every_source_filtered_report(tmp_path):
    settings = _combined_settings(tmp_path)
    summary = ingest_all(settings)
    assert (summary.root_sessions, summary.subagent_sessions) == (2, 2)
    assert summary.cursor is None
    _add_claude_session(settings)
    _add_cursor_session(settings)

    app = create_app(settings)
    health = next(route.endpoint for route in app.routes if route.path == "/healthz")
    assert health()["claude_home"] == str(settings.claude_home)
    assert health()["cursor_home"] == str(settings.cursor_home)
    # Each view needs to retain every source filter.  These values occur in
    # the report body rather than in the shared navigation.
    pages = {
        "/": {
            "codex": "Synthetic Codex task", "opencode": "Synthetic OpenCode task",
            "claude": "Synthetic Claude task", "cursor": "Synthetic Cursor task",
        },
        "/sessions": {
            "codex": "Synthetic Codex task", "opencode": "Synthetic OpenCode task",
            "claude": "Synthetic Claude task", "cursor": "Synthetic Cursor task",
        },
        "/models": {
            "codex": "gpt-5.6-sol", "opencode": "opencode-model", "claude": "claude-model", "cursor": "cursor-model",
        },
        "/projects": {
            "codex": "example-project", "opencode": "Synthetic OpenCode project",
            "claude": "Synthetic Claude project", "cursor": "Synthetic Cursor project",
        },
        "/trends": {
            "codex": "gpt-5.6-sol", "opencode": "opencode-model", "claude": "claude-model", "cursor": "cursor-model",
        },
    }
    for path, values in pages.items():
        all_body = _page(app, path, "all")
        for value in values.values():
            assert value in all_body
        for source, value in values.items():
            body = _page(app, path, source)
            assert value in body
            for other_source, other_value in values.items():
                if other_source != source:
                    assert other_value not in body

    compare = next(route.endpoint for route in app.routes if route.path == "/compare")
    request = Request(
        {
            "type": "http", "method": "GET", "path": "/compare",
            "headers": [], "query_string": b"", "app": app,
        }
    )
    response = compare(
        request, session=["codex-root", "opencode:root", "claude:session", "cursor:session"], source="cursor"
    )
    assert [row["id"] for row in response.context["rows"]] == ["cursor:session"]
    assert set(response.context["model_costs"]) == {"cursor:session"}


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

    _add_cursor_session(settings)
    assert export_command(settings, "json", "sessions", "-", source="cursor") == 0
    exported = json.loads(capsys.readouterr().out)
    assert [(row["session_id"], row["source_app"]) for row in exported] == [
        ("cursor:session", "cursor")
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
        cursor_home=tmp_path / "missing-cursor", cursor_user_dir=tmp_path / "missing-cursor-user",
    )

    summary = ingest_all(settings)

    assert "opencode" in summary.source_errors
    assert summary.claude is not None
    with database(settings.database, readonly=True) as conn:
        claude_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_app='claude'"
        ).fetchone()[0]
    assert claude_sessions == 1


def test_cursor_adapter_runs_through_ingest_all_when_history_exists(tmp_path):
    from test_cursor_ingestion import _fixture as cursor_fixture

    home, user_dir = cursor_fixture(tmp_path)
    settings = Settings(
        tmp_path / "codex", tmp_path / "dashboard.sqlite", running_window_seconds=0,
        opencode_database=tmp_path / "missing-opencode.sqlite", claude_home=tmp_path / "missing-claude",
        cursor_home=home, cursor_user_dir=user_dir,
    )

    summary = ingest_all(settings)

    assert summary.source_errors == {}
    assert summary.cursor is not None and summary.cursor.root_sessions == 3
    assert summary.root_sessions == 3
    with database(settings.database, readonly=True) as conn:
        sources = {row[0] for row in conn.execute("SELECT DISTINCT source_app FROM sessions")}
    assert sources == {"cursor"}
