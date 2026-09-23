from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from spenda.config import Settings
from spenda.db import database
from spenda.ingestion.opencode import ingest_opencode
from spenda.pricing import add_price, reprice_usage


def _source_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE project(
              id TEXT PRIMARY KEY, worktree TEXT NOT NULL, name TEXT,
              time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
            );
            CREATE TABLE session(
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
              directory TEXT NOT NULL, title TEXT NOT NULL, version TEXT NOT NULL,
              agent TEXT, model TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
            );
            CREATE TABLE message(
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
              time_updated INTEGER NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE part(
              id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
              time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO project VALUES('project','/work/repo','Repo',1000,1000)"
        )
        sessions = (
            ("root", None, "build"),
            ("child", "root", "explore"),
            ("grandchild", "child", "review"),
        )
        for ordinal, (session_id, parent_id, agent) in enumerate(sessions):
            conn.execute(
                "INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id, "project", parent_id, "/work/repo", session_id,
                    "2.0", agent, json.dumps({"id": "model", "providerID": "provider"}),
                    1000 + ordinal, 2000 + ordinal,
                ),
            )
        for ordinal, session_id in enumerate(("root", "child", "grandchild")):
            data = {
                "role": "assistant", "providerID": "provider", "modelID": "model",
                "cost": "0.25", "tokens": {
                    "input": 10, "cache": {"read": 20, "write": 30},
                    "output": 40, "reasoning": 5,
                    # A zero source total is common while streaming.  The
                    # importer must retain a useful normalized total.
                    "total": 0,
                },
            }
            conn.execute(
                "INSERT INTO message VALUES(?,?,?,?,?)",
                (f"message-{session_id}", session_id, 3000 + ordinal, 3000 + ordinal, json.dumps(data)),
            )
        conn.execute(
            "INSERT INTO message VALUES(?,?,?,?,?)",
            ("user-message", "root", 4000, 4000, json.dumps({"role": "user"})),
        )
        parts = (
            (
                "part-root", "message-root", "root",
                {"type": "tool", "tool": "bash", "state": {"input": {"command": "private-command"}}},
            ),
            (
                "part-child", "message-child", "child",
                {"type": "tool", "tool": "read", "state": {"input": {"filePath": "/private/file"}}},
            ),
            (
                "part-grandchild", "message-grandchild", "grandchild",
                {"type": "text", "text": "private assistant response"},
            ),
        )
        for ordinal, (part_id, message_id, session_id, data) in enumerate(parts):
            conn.execute(
                "INSERT INTO part VALUES(?,?,?,?,?,?)",
                (part_id, message_id, session_id, 5000 + ordinal, 5000 + ordinal, json.dumps(data)),
            )


def _settings(tmp_path: Path, source: Path) -> Settings:
    return Settings(
        tmp_path / "codex", tmp_path / "dashboard.sqlite", running_window_seconds=0,
        opencode_database=source,
    )


def test_opencode_paths_totals_and_turn_count(tmp_path):
    source = tmp_path / "opencode.sqlite"
    _source_db(source)
    settings = _settings(tmp_path, source)

    summary = ingest_opencode(settings)

    assert (summary.root_sessions, summary.subagent_sessions, summary.usage_records) == (1, 2, 3)
    assert summary.recorded_spend == 0.75
    assert summary.estimated_spend == 0
    with database(settings.database, readonly=True) as conn:
        paths = dict(conn.execute("SELECT thread_id,agent_path FROM agents"))
        usage = conn.execute(
            "SELECT input_tokens,cached_input_tokens,cache_write_input_tokens,uncached_input_tokens,"
            "output_tokens,reasoning_output_tokens,total_tokens,cost_usd FROM usage "
            "WHERE source_record_identity='opencode:message-child'"
        ).fetchone()
        turns = conn.execute(
            "SELECT turn_count FROM sessions WHERE id='opencode:root'"
        ).fetchone()[0]
        labels = dict(conn.execute(
            "SELECT source_record_identity,call_label FROM usage ORDER BY source_record_identity"
        ))

    assert paths == {
        "opencode:root": "/root",
        "opencode:child": "/root/child",
        "opencode:grandchild": "/root/child/grandchild",
    }
    assert tuple(usage) == (60, 20, 30, 10, 45, 5, 105, "0.25")
    assert turns == 3
    assert labels == {
        "opencode:message-child": "Read files",
        "opencode:message-grandchild": "Assistant response",
        "opencode:message-root": "Run command",
    }
    with sqlite3.connect(settings.database) as conn:
        logical_dump = "\n".join(conn.iterdump())
    assert "private-command" not in logical_dump
    assert "/private/file" not in logical_dump
    assert "private assistant response" not in logical_dump


def test_opencode_backfills_labels_for_existing_usage(tmp_path):
    source = tmp_path / "opencode.sqlite"
    _source_db(source)
    settings = _settings(tmp_path, source)
    ingest_opencode(settings)
    with database(settings.database) as conn:
        conn.execute(
            "UPDATE usage SET call_label=NULL WHERE source_event_type='opencode_assistant_message'"
        )

    ingest_opencode(settings)

    with database(settings.database, readonly=True) as conn:
        labels = [row[0] for row in conn.execute(
            "SELECT call_label FROM usage WHERE source_event_type='opencode_assistant_message'"
        )]
    assert all(labels)


def test_opencode_reconciles_removed_rows_without_touching_other_source(tmp_path):
    source = tmp_path / "opencode.sqlite"
    _source_db(source)
    settings = _settings(tmp_path, source)
    ingest_opencode(settings)

    with database(settings.database) as conn:
        conn.execute(
            "INSERT INTO sessions(id,root_thread_id,source_app,source_home) VALUES(?,?,?,?)",
            ("codex:kept", "codex:kept", "codex", "/tmp/codex"),
        )
        conn.execute(
            "INSERT INTO agents(thread_id,session_id,agent_role,source_kind) VALUES(?,?,?,?)",
            ("codex:kept", "codex:kept", "root", "cli"),
        )

    with sqlite3.connect(source) as conn:
        conn.execute("DELETE FROM message WHERE session_id IN ('child','grandchild')")
        conn.execute("DELETE FROM session WHERE id IN ('child','grandchild')")

    summary = ingest_opencode(settings)

    assert (summary.root_sessions, summary.subagent_sessions) == (1, 0)
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agents WHERE thread_id LIKE 'opencode:%'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity LIKE 'opencode:%'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT turn_count FROM sessions WHERE id='opencode:root'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='codex:kept'").fetchone()[0] == 1


def test_opencode_reparents_older_usage_before_deleting_removed_root(tmp_path):
    source = tmp_path / "opencode.sqlite"
    _source_db(source)
    settings = _settings(tmp_path, source)
    ingest_opencode(settings)

    with sqlite3.connect(source) as conn:
        conn.execute("DELETE FROM message WHERE session_id='root'")
        conn.execute("DELETE FROM session WHERE id='root'")

    ingest_opencode(settings)

    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT source_record_identity,session_id FROM usage ORDER BY source_record_identity"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("opencode:message-child", "opencode:child"),
        ("opencode:message-grandchild", "opencode:child"),
    ]


def test_codex_repricing_does_not_overwrite_opencode_reported_cost(tmp_path):
    source = tmp_path / "opencode.sqlite"
    _source_db(source)
    settings = _settings(tmp_path, source)
    ingest_opencode(settings)

    with database(settings.database) as conn:
        before = conn.execute(
            "SELECT cost_usd FROM usage WHERE source_record_identity='opencode:message-root'"
        ).fetchone()[0]
        add_price(
            conn, model="model", provider="provider", effective_from="1970-01-01T00:00:00Z",
            input_per_million="999", cached_input_per_million="999",
            cache_write_per_million="999", output_per_million="999", source="test",
        )
        assert reprice_usage(conn, provider="provider") == 0
        after = conn.execute(
            "SELECT cost_usd FROM usage WHERE source_record_identity='opencode:message-root'"
        ).fetchone()[0]

    assert before == after == "0.25"
