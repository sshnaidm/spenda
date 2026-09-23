from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spenda.config import Settings


@pytest.fixture(autouse=True)
def _isolated_cursor_history(tmp_path: Path, monkeypatch) -> None:
    """Keep every test away from the developer's real Cursor history."""

    monkeypatch.setenv("CURSOR_HOME", str(tmp_path / "missing-cursor"))
    monkeypatch.setenv("CURSOR_USER_DIR", str(tmp_path / "missing-cursor-user"))


@pytest.fixture
def dashboard_settings(tmp_path: Path) -> Settings:
    home = tmp_path / "codex"
    (home / "sessions" / "2026" / "09" / "08").mkdir(parents=True)
    return Settings(
        home, tmp_path / "dashboard.sqlite", True, running_window_seconds=0,
        opencode_database=tmp_path / "missing-opencode.sqlite",
        claude_home=tmp_path / "missing-claude",
        cursor_home=tmp_path / "missing-cursor", cursor_user_dir=tmp_path / "missing-cursor-user",
    )


def make_state(home: Path, threads: list[dict], edges: list[tuple[str, str]] = ()) -> Path:
    path = home / "state_1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE threads(
          id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER,
          source TEXT, model_provider TEXT, cwd TEXT, title TEXT, tokens_used INTEGER,
          git_sha TEXT, git_branch TEXT, git_origin_url TEXT, cli_version TEXT,
          first_user_message TEXT, agent_nickname TEXT, agent_role TEXT, model TEXT,
          reasoning_effort TEXT, agent_path TEXT, thread_source TEXT, name TEXT
        );
        CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT PRIMARY KEY,status TEXT);
        CREATE TABLE _sqlx_migrations(version INTEGER,success INTEGER);
        INSERT INTO _sqlx_migrations VALUES(1,1);
        """
    )
    fields = [
        "id", "rollout_path", "created_at", "updated_at", "source", "model_provider", "cwd",
        "title", "tokens_used", "git_sha", "git_branch", "git_origin_url", "cli_version",
        "first_user_message", "agent_nickname", "agent_role", "model", "reasoning_effort",
        "agent_path", "thread_source", "name",
    ]
    for item in threads:
        values = [item.get(field) for field in fields]
        conn.execute(f"INSERT INTO threads VALUES({','.join('?' for _ in fields)})", values)
    conn.executemany("INSERT INTO thread_spawn_edges VALUES(?,?,'open')", edges)
    conn.commit()
    conn.close()
    return path


def thread(
    thread_id: str,
    rollout: Path,
    *,
    source: str | dict = "cli",
    model: str = "gpt-5.6-sol",
    agent_path: str | None = None,
    role: str | None = None,
) -> dict:
    return {
        "id": thread_id, "rollout_path": str(rollout), "created_at": 1788860000,
        "updated_at": 1788860060, "source": json.dumps(source) if isinstance(source, dict) else source,
        "model_provider": "openai", "cwd": "/tmp/example-project", "title": "Synthetic task",
        "tokens_used": 0, "git_sha": "abc123", "git_branch": "main", "git_origin_url": None,
        "cli_version": "0.153.4", "first_user_message": "Synthetic task full prompt",
        "agent_nickname": None, "agent_role": role, "model": model, "reasoning_effort": "high",
        "agent_path": agent_path, "thread_source": "subagent" if source != "cli" else "user", "name": None,
    }


def session_meta(thread_id: str, source: str | dict = "cli", ordinal: int = 0) -> dict:
    return {
        "timestamp": "2026-09-08T10:00:00Z", "type": "session_meta", "ordinal": ordinal,
        "payload": {"id": thread_id, "cwd": "/tmp/example-project", "cli_version": "0.153.4",
                    "source": source, "model_provider": "openai", "git": {"branch": "main", "commit_hash": "abc123"}},
    }


def turn(turn_id: str, model: str = "gpt-5.6-sol", ordinal: int = 1) -> dict:
    return {"timestamp": "2026-09-08T10:00:01Z", "type": "turn_context", "ordinal": ordinal,
            "payload": {"turn_id": turn_id, "model": model, "reasoning_effort": "high"}}


def usage_values(input_tokens=1000, cached=500, write=400, output=100, reasoning=40) -> dict:
    return {"input_tokens": input_tokens, "cached_input_tokens": cached,
            "cache_write_input_tokens": write, "output_tokens": output,
            "reasoning_output_tokens": reasoning, "total_tokens": input_tokens + output}


def atomic(
    thread_id: str,
    turn_id: str,
    response_id: str,
    *,
    ordinal: int = 2,
    values: dict | None = None,
    timestamp: str = "2026-09-08T10:00:02Z",
) -> dict:
    values = values or usage_values()
    return {"timestamp": timestamp, "type": "token_usage_record", "ordinal": ordinal,
            "payload": {"thread_id": thread_id, "turn_id": turn_id, "session_id": thread_id,
                        "root_turn_id": turn_id, "response_id": response_id, "usage": values,
                        "turn_token_usage": values, "thread_token_usage": values}}


def token_count(values: dict, *, ordinal: int = 3, total: dict | None = None) -> dict:
    return {"timestamp": "2026-09-08T10:00:02.010Z", "type": "event_msg", "ordinal": ordinal,
            "payload": {"type": "token_count", "info": {"last_token_usage": values,
                        "total_token_usage": total or values, "model_context_window": 258400}}}


def write_rollout(path: Path, records: list[dict], *, final_newline: bool = True) -> None:
    text = "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
    path.write_text(text + ("\n" if final_newline else ""), encoding="utf-8")
