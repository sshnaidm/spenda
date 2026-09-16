from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 7

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS dashboard_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    root_thread_id TEXT NOT NULL UNIQUE,
    title TEXT,
    first_user_message_preview TEXT,
    cwd TEXT,
    repo_root TEXT,
    repo_name TEXT,
    git_branch TEXT,
    git_commit TEXT,
    git_origin_url TEXT,
    created_at TEXT,
    updated_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    root_model TEXT,
    root_reasoning_effort TEXT,
    root_provider TEXT,
    root_backend TEXT,
    source_app TEXT NOT NULL DEFAULT 'codex',
    source_home TEXT NOT NULL,
    source_version TEXT,
    turn_count INTEGER NOT NULL DEFAULT 0,
    parser_warnings INTEGER NOT NULL DEFAULT 0,
    accounting_status TEXT NOT NULL DEFAULT 'complete',
    accounting_note TEXT
);
CREATE TABLE IF NOT EXISTS agents (
    thread_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    parent_thread_id TEXT,
    agent_role TEXT,
    agent_nickname TEXT,
    agent_path TEXT,
    created_at TEXT,
    updated_at TEXT,
    model TEXT,
    model_provider TEXT,
    backend TEXT,
    reasoning_effort TEXT,
    source_rollout_path TEXT,
    source_kind TEXT,
    orphan INTEGER NOT NULL DEFAULT 0,
    source_tokens_used INTEGER,
    source_available INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'openai',
    effective_from TEXT NOT NULL,
    effective_until TEXT,
    input_per_million TEXT NOT NULL,
    cached_input_per_million TEXT NOT NULL,
    cache_write_per_million TEXT NOT NULL,
    output_per_million TEXT NOT NULL,
    long_context_threshold INTEGER,
    long_input_multiplier TEXT NOT NULL DEFAULT '1',
    long_output_multiplier TEXT NOT NULL DEFAULT '1',
    source TEXT NOT NULL,
    notes TEXT,
    UNIQUE(model, provider, effective_from)
);
CREATE TABLE IF NOT EXISTS model_aliases (
    alias TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'openai',
    PRIMARY KEY(alias, provider)
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    source_record_identity TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    thread_id TEXT NOT NULL REFERENCES agents(thread_id) ON DELETE CASCADE,
    turn_id TEXT,
    response_id TEXT,
    timestamp TEXT NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    backend TEXT,
    billing_mode TEXT NOT NULL DEFAULT 'metered',
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    cache_write_input_tokens INTEGER NOT NULL,
    cache_write_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
    uncached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    counts_toward_totals INTEGER NOT NULL DEFAULT 1,
    source_file TEXT NOT NULL,
    source_ordinal INTEGER,
    source_event_type TEXT NOT NULL,
    call_label TEXT,
    price_id INTEGER REFERENCES prices(id),
    uncached_input_usd TEXT,
    cached_input_usd TEXT,
    cache_write_usd TEXT,
    output_usd TEXT,
    cost_usd TEXT,
    equivalent_cost_usd TEXT,
    pricing_note TEXT
);
CREATE TABLE IF NOT EXISTS ingestion_state (
    source_key TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    inode INTEGER,
    last_offset INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    parser_version INTEGER NOT NULL,
    last_successful_ingestion TEXT,
    owner_thread_id TEXT,
    current_turn_id TEXT,
    current_model TEXT,
    current_reasoning_effort TEXT,
    current_provider TEXT,
    previous_cumulative_json TEXT,
    recent_atomic_json TEXT,
    pending_call_label TEXT,
    pending_call_priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_sync_state (
    source_app TEXT NOT NULL,
    source_path TEXT NOT NULL,
    cursor_timestamp INTEGER NOT NULL DEFAULT 0,
    last_successful_ingestion TEXT,
    PRIMARY KEY(source_app, source_path)
);
CREATE TABLE IF NOT EXISTS parser_warnings (
    id INTEGER PRIMARY KEY,
    source_key TEXT,
    source_ordinal INTEGER,
    timestamp TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    UNIQUE(source_key, source_ordinal, code, message)
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY(session_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_source_created ON sessions(source_app, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(repo_name, cwd);
CREATE INDEX IF NOT EXISTS idx_sessions_root_model ON sessions(root_model);
CREATE INDEX IF NOT EXISTS idx_agents_session ON agents(session_id);
CREATE INDEX IF NOT EXISTS idx_agents_parent ON agents(parent_thread_id);
CREATE INDEX IF NOT EXISTS idx_agents_model ON agents(model);
CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_thread ON usage(thread_id);
CREATE INDEX IF NOT EXISTS idx_usage_model_time ON usage(model, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_response ON usage(response_id);
CREATE INDEX IF NOT EXISTS idx_usage_backend ON usage(backend);
"""


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_session_source_columns(conn: sqlite3.Connection) -> None:
    """Upgrade the v3 Codex-specific session provenance columns in place."""
    session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if not session_columns:
        return
    if "source_codex_home" in session_columns and "source_home" not in session_columns:
        conn.execute("ALTER TABLE sessions RENAME COLUMN source_codex_home TO source_home")
        session_columns.remove("source_codex_home")
        session_columns.add("source_home")
    if "source_codex_version" in session_columns and "source_version" not in session_columns:
        conn.execute("ALTER TABLE sessions RENAME COLUMN source_codex_version TO source_version")
        session_columns.remove("source_codex_version")
        session_columns.add("source_version")
    if "source_app" not in session_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN source_app TEXT NOT NULL DEFAULT 'codex'")


def _migrate_backend_columns(conn: sqlite3.Connection) -> None:
    """Add post-v5 API-backend, billing, and authoritative-ledger columns."""
    additions = {
        "sessions": (("root_backend", "TEXT"),),
        "agents": (("backend", "TEXT"),),
        "usage": (
            ("backend", "TEXT"),
            ("billing_mode", "TEXT NOT NULL DEFAULT 'metered'"),
            ("cache_write_1h_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("equivalent_cost_usd", "TEXT"),
            ("counts_toward_totals", "INTEGER NOT NULL DEFAULT 1"),
        ),
    }
    for table, columns in additions.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def initialize(path: Path) -> None:
    if path.exists() and path.stat().st_size:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)) as existing:
            tables = {row[0] for row in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "dashboard_meta" not in tables:
            raise ValueError(f"refusing to modify a non-dashboard SQLite database: {path}")
    with closing(connect(path)) as conn, conn:
        # Upgrade before applying the complete schema, which includes an index
        # over ``source_app`` that does not exist in v3.
        _migrate_session_source_columns(conn)
        _migrate_backend_columns(conn)
        conn.executescript(SCHEMA)
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "accounting_status" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN accounting_status TEXT NOT NULL DEFAULT 'complete'")
        if "accounting_note" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN accounting_note TEXT")
        usage_columns = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
        if "call_label" not in usage_columns:
            conn.execute("ALTER TABLE usage ADD COLUMN call_label TEXT")
        ingestion_columns = {row[1] for row in conn.execute("PRAGMA table_info(ingestion_state)")}
        if "pending_call_label" not in ingestion_columns:
            conn.execute("ALTER TABLE ingestion_state ADD COLUMN pending_call_label TEXT")
        if "pending_call_priority" not in ingestion_columns:
            conn.execute("ALTER TABLE ingestion_state ADD COLUMN pending_call_priority INTEGER NOT NULL DEFAULT 0")
        agent_columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        if "source_tokens_used" not in agent_columns:
            conn.execute("ALTER TABLE agents ADD COLUMN source_tokens_used INTEGER")
        if "source_available" not in agent_columns:
            conn.execute("ALTER TABLE agents ADD COLUMN source_available INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "INSERT INTO dashboard_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


@contextmanager
def database(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(path, readonly=readonly)
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()
