from __future__ import annotations

import sqlite3

import pytest

from spenda.config import Settings
from spenda.db import SCHEMA, SCHEMA_VERSION, initialize


def test_v3_sessions_migrate_to_generic_source_columns(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    v3_schema = SCHEMA.replace(
        "    source_app TEXT NOT NULL DEFAULT 'codex',\n"
        "    source_home TEXT NOT NULL,\n"
        "    source_version TEXT,",
        "    source_codex_home TEXT NOT NULL,\n"
        "    source_codex_version TEXT,",
    ).replace(
        "CREATE INDEX IF NOT EXISTS idx_sessions_source_created ON sessions(source_app, created_at);\n",
        "",
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(v3_schema)
        conn.execute(
            "INSERT INTO sessions(id, root_thread_id, source_codex_home, source_codex_version) "
            "VALUES ('session-1', 'thread-1', '/tmp/codex', '0.1.0')"
        )
        conn.execute("INSERT INTO dashboard_meta(key, value) VALUES ('schema_version', '3')")

    initialize(path)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        row = conn.execute(
            "SELECT source_app, source_home, source_version FROM sessions WHERE id='session-1'"
        ).fetchone()
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(sessions)")}
        version = conn.execute(
            "SELECT value FROM dashboard_meta WHERE key='schema_version'"
        ).fetchone()[0]

    assert {"source_app", "source_home", "source_version"} <= columns
    assert "source_codex_home" not in columns
    assert "source_codex_version" not in columns
    assert row == ("codex", "/tmp/codex", "0.1.0")
    assert "idx_sessions_source_created" in indexes
    assert version == str(SCHEMA_VERSION)


def test_opencode_database_uses_environment_override(monkeypatch, tmp_path):
    source = tmp_path / "opencode.sqlite"
    monkeypatch.setenv("OPENCODE_DB", str(source))

    settings = Settings.load(tmp_path / "codex", tmp_path / "dashboard.sqlite")

    assert settings.opencode_database == source.resolve()


def test_claude_home_uses_environment_override(monkeypatch, tmp_path):
    source = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(source))

    settings = Settings.load(tmp_path / "codex", tmp_path / "dashboard.sqlite")

    assert settings.claude_home == source.resolve()


def test_dashboard_database_cannot_equal_opencode_source(tmp_path):
    path = tmp_path / "shared.sqlite"
    settings = Settings(tmp_path / "codex", path, opencode_database=path)

    with pytest.raises(ValueError, match="OpenCode source"):
        settings.validate()


def test_dashboard_database_cannot_be_inside_claude_home(tmp_path):
    claude_home = tmp_path / "claude"
    settings = Settings(tmp_path / "codex", claude_home / "dashboard.sqlite", claude_home=claude_home)

    with pytest.raises(ValueError, match="CLAUDE_CONFIG_DIR"):
        settings.validate()


def test_cursor_paths_use_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path / "cursor"))
    monkeypatch.setenv("CURSOR_USER_DIR", str(tmp_path / "cursor-user"))

    settings = Settings.load(tmp_path / "codex", tmp_path / "dashboard.sqlite")

    assert settings.cursor_home == (tmp_path / "cursor").resolve()
    assert settings.cursor_user_dir == (tmp_path / "cursor-user").resolve()


def test_cursor_user_dir_defaults_to_platform_location(monkeypatch, tmp_path):
    monkeypatch.delenv("CURSOR_USER_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("spenda.config.sys.platform", "linux")
    monkeypatch.setattr("spenda.config.os.name", "posix")

    settings = Settings.load(tmp_path / "codex", tmp_path / "dashboard.sqlite")

    assert settings.cursor_user_dir == (tmp_path / "config" / "Cursor" / "User").resolve()


def test_dashboard_database_cannot_be_inside_cursor_directories(tmp_path):
    cursor_home = tmp_path / "cursor"
    settings = Settings(tmp_path / "codex", cursor_home / "dashboard.sqlite", cursor_home=cursor_home)
    with pytest.raises(ValueError, match="CURSOR_HOME"):
        settings.validate()

    user_dir = tmp_path / "cursor-user"
    settings = Settings(tmp_path / "codex", user_dir / "dashboard.sqlite", cursor_user_dir=user_dir)
    with pytest.raises(ValueError, match="CURSOR_USER_DIR"):
        settings.validate()
