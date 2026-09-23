"""Read-only ingestion of OpenCode's SQLite session ledger.

The adapter deliberately queries scalar JSON fields from ``message.data`` in
SQLite. For action labels it extracts only part types and tool names from
``part.data``. It never selects either JSON document itself and never reads the
``credential`` table, so prompts, responses, tool arguments/output, and
credentials do not enter the dashboard database.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import database, initialize
from .action_labels import prefer_action_label, safe_action_label

PARSER_VERSION = 1
SOURCE_APP = "opencode"
_ID_PREFIX = "opencode:"


class OpenCodeSchemaError(ValueError):
    """The configured database is not a compatible OpenCode session database."""


@dataclass(slots=True)
class OpenCodeIngestSummary:
    """Results from one OpenCode import.

    The common fields intentionally match the existing Codex ingest summary so
    the CLI can print either source without source-specific special cases.
    """

    scanned_files: int = 1
    scanned_sessions: int = 0
    root_sessions: int = 0
    subagent_sessions: int = 0
    usage_records: int = 0
    duplicate_records: int = 0
    malformed_lines: int = 0
    parser_warnings: int = 0
    unknown_models: set[str] = field(default_factory=set)
    unknown_prices: set[str] = field(default_factory=set)
    estimated_spend: float = 0.0
    recorded_spend: float = 0.0
    source_database: Path | None = None


def discover_opencode_database(settings: Settings) -> Path:
    """Return the configured OpenCode database without opening it.

    ``opencode_db`` is added to Settings by the integration layer.  Attribute
    lookup remains tolerant while this module is independently importable by
    older dashboard installations.
    """

    configured = (
        getattr(settings, "opencode_database", None)
        or getattr(settings, "opencode_db", None)
        or os.environ.get("OPENCODE_DB")
    )
    if configured:
        return Path(configured).expanduser().resolve()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (data_home / "opencode" / "opencode.db").expanduser().resolve()


@contextmanager
def _source_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise FileNotFoundError(f"OpenCode database not found: {path}")
    # as_uri quotes path characters correctly; mode=ro prevents SQLite from
    # creating a database if the source disappears between the stat and open.
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _validate_source_schema(conn: sqlite3.Connection) -> None:
    # These are the OpenCode v2 fields used below.  In particular, do not use
    # a broad "any table exists" check: a future migration should fail loudly
    # rather than quietly importing a misleading partial ledger.
    required = {
        "session": {
            "id", "project_id", "parent_id", "directory", "title", "version",
            "agent", "model", "time_created", "time_updated",
        },
        "message": {"id", "session_id", "time_created", "time_updated", "data"},
        "project": {"id", "worktree", "name"},
    }
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing_tables = sorted(set(required) - tables)
    if missing_tables:
        raise OpenCodeSchemaError(
            "OpenCode database is missing required table(s): " + ", ".join(missing_tables)
        )
    problems = []
    for table, expected in required.items():
        missing = sorted(expected - _columns(conn, table))
        if missing:
            problems.append(f"{table}: {', '.join(missing)}")
    if problems:
        raise OpenCodeSchemaError(
            "OpenCode database schema is incompatible (missing columns: "
            + "; ".join(problems)
            + ")"
        )
    try:
        # Validate JSON1 availability before a mutation begins.  The contents
        # are never returned, only a scalar discriminator is evaluated.
        conn.execute("SELECT json_extract(data, '$.role') FROM message LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        raise OpenCodeSchemaError("OpenCode message JSON cannot be queried") from exc


def _dashboard_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return _columns(conn, table)


def _ns(value: str) -> str:
    return value if value.startswith(_ID_PREFIX) else f"{_ID_PREFIX}{value}"


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # OpenCode uses epoch milliseconds.  Supporting epoch seconds makes
    # fixtures and older snapshots harmless without changing normal behavior.
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _nonnegative_int(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return 0
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _cost(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value if value is not None else 0))
        return max(Decimal("0"), parsed)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _repo_name(worktree: str | None, project_name: str | None, directory: str | None) -> str | None:
    if project_name:
        return project_name
    candidate = worktree or directory
    return Path(candidate).name if candidate else None


def _root_for(session_id: str, parents: dict[str, str | None]) -> tuple[str, bool]:
    """Return the top-level external session and whether an ancestor is absent."""

    current = session_id
    seen: set[str] = set()
    while True:
        parent = parents.get(current)
        if not parent:
            return current, False
        if parent not in parents or parent in seen:
            return current if parent not in parents else session_id, True
        seen.add(current)
        current = parent


def _agent_paths(parents: dict[str, str | None]) -> dict[str, str]:
    """Build display hierarchy paths from OpenCode's parent graph.

    ``agents.agent_path`` is consumed by the dashboard as a hierarchy, not a
    working directory.  External session IDs make sibling path components
    stable even when several children use the same OpenCode agent name.
    """

    paths: dict[str, str] = {}
    resolving: set[str] = set()

    def path_for(session_id: str) -> str:
        existing = paths.get(session_id)
        if existing:
            return existing
        parent = parents.get(session_id)
        if not parent:
            result = "/root"
        elif parent not in parents or session_id in resolving:
            # Keep an incomplete/cyclic relation visibly below root instead of
            # inventing a filesystem path or recursing forever.
            result = f"/root/{session_id}"
        else:
            resolving.add(session_id)
            result = f"{path_for(parent)}/{session_id}"
            resolving.discard(session_id)
        paths[session_id] = result
        return result

    for session_id in parents:
        path_for(session_id)
    return paths


def _ensure_session(
    conn: sqlite3.Connection,
    columns: set[str],
    *,
    root_id: str,
    source_path: Path,
    version: str | None,
) -> None:
    """Create the root task while supporting the pre-generalization schema."""

    names = ["id", "root_thread_id"]
    values: list[Any] = [root_id, root_id]
    updates: list[str] = []
    if "source_app" in columns:
        names.append("source_app")
        values.append(SOURCE_APP)
        updates.append("source_app=excluded.source_app")
    if "source_home" in columns:
        names.append("source_home")
        values.append(str(source_path))
        updates.append("source_home=excluded.source_home")
    elif "source_codex_home" in columns:
        # Compatibility only; normal integration migrates this column to
        # source_home before calling the adapter.
        names.append("source_codex_home")
        values.append(str(source_path))
        updates.append("source_codex_home=excluded.source_codex_home")
    if "source_version" in columns:
        names.append("source_version")
        values.append(version)
        updates.append("source_version=COALESCE(excluded.source_version,source_version)")
    elif "source_codex_version" in columns:
        names.append("source_codex_version")
        values.append(version)
        updates.append("source_codex_version=COALESCE(excluded.source_codex_version,source_codex_version)")
    placeholders = ",".join("?" for _ in names)
    update_sql = ",".join(updates) or "root_thread_id=excluded.root_thread_id"
    conn.execute(
        f"INSERT INTO sessions({','.join(names)}) VALUES({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_sql}",
        values,
    )


def _upsert_session_metadata(
    conn: sqlite3.Connection,
    root_id: str,
    row: sqlite3.Row,
) -> None:
    created_at = _timestamp(row["time_created"])
    updated_at = _timestamp(row["time_updated"])
    directory = row["directory"]
    worktree = row["worktree"]
    conn.execute(
        """UPDATE sessions SET
           title=?,cwd=?,repo_root=?,repo_name=?,created_at=?,updated_at=?,
           root_model=?,root_provider=?,status='completed',finished_at=?,
           accounting_status='complete',accounting_note=NULL
           WHERE id=?""",
        (
            row["title"], directory, worktree or directory,
            _repo_name(worktree, row["project_name"], directory), created_at, updated_at,
            row["session_model"], row["session_provider"], updated_at, root_id,
        ),
    )


def _upsert_agent(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    root_id: str,
    root_external_id: str,
    parent_id: str | None,
    agent_path: str,
    orphan: bool,
    source_path: Path,
) -> None:
    external_id = str(row["id"])
    thread_id = _ns(external_id)
    conn.execute(
        """INSERT INTO agents(thread_id,session_id,parent_thread_id,agent_role,agent_nickname,
           agent_path,created_at,updated_at,model,model_provider,source_rollout_path,
           source_kind,orphan,source_available)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(thread_id) DO UPDATE SET
             session_id=excluded.session_id,parent_thread_id=excluded.parent_thread_id,
             agent_role=excluded.agent_role,agent_nickname=excluded.agent_nickname,
             agent_path=excluded.agent_path,created_at=excluded.created_at,updated_at=excluded.updated_at,
             model=COALESCE(excluded.model,agents.model),
             model_provider=COALESCE(excluded.model_provider,agents.model_provider),
             source_rollout_path=excluded.source_rollout_path,source_kind=excluded.source_kind,
             orphan=excluded.orphan,source_available=1""",
        (
            thread_id, root_id, _ns(parent_id) if parent_id else None,
            "root" if external_id == root_external_id else "subagent",
            row["agent"], agent_path, _timestamp(row["time_created"]),
            _timestamp(row["time_updated"]), row["session_model"], row["session_provider"],
            str(source_path), SOURCE_APP, int(orphan),
        ),
    )


def _source_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Read session metadata plus only the scalar data required for accounting."""

    return conn.execute(
        """SELECT s.id,s.project_id,s.parent_id,s.directory,s.title,s.version,s.agent,
                  s.time_created,s.time_updated,p.worktree,p.name AS project_name,
                  json_extract(s.model,'$.id') AS session_model,
                  json_extract(s.model,'$.providerID') AS session_provider
           FROM session s LEFT JOIN project p ON p.id=s.project_id
           ORDER BY s.time_created,s.id"""
    ).fetchall()


def _message_rows(
    conn: sqlite3.Connection, cursor_timestamp: int | None = None
) -> Iterable[sqlite3.Row]:
    # data is intentionally absent from SELECT. JSON extraction occurs within
    # SQLite and returns only role/model/usage scalar values to Python.
    cursor_where = "" if cursor_timestamp is None else "AND m.time_updated>=?"
    params = () if cursor_timestamp is None else (cursor_timestamp,)
    return conn.execute(
        f"""SELECT m.id,m.session_id,m.time_created,m.time_updated,
                  json_extract(m.data,'$.role') AS role,
                  json_extract(m.data,'$.providerID') AS provider,
                  COALESCE(json_extract(m.data,'$.modelID'),json_extract(m.data,'$.model.id')) AS model,
                  json_extract(m.data,'$.cost') AS cost,
                  json_extract(m.data,'$.tokens.input') AS token_input,
                  json_extract(m.data,'$.tokens.output') AS token_output,
                  json_extract(m.data,'$.tokens.reasoning') AS token_reasoning,
                  json_extract(m.data,'$.tokens.cache.read') AS token_cache_read,
                  json_extract(m.data,'$.tokens.cache.write') AS token_cache_write,
                  json_extract(m.data,'$.tokens.total') AS token_total,
                  json_extract(s.model,'$.id') AS session_model,
                  json_extract(s.model,'$.providerID') AS session_provider
           FROM message m JOIN session s ON s.id=m.session_id
           WHERE json_extract(m.data,'$.role')='assistant'
             {cursor_where}
           ORDER BY m.time_updated,m.id""",
        params,
    )


def _message_action_labels(conn: sqlite3.Connection) -> dict[str, str]:
    """Read only part type/tool-name scalars, never part content or arguments."""

    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {"id", "message_id", "time_created", "data"}
    if "part" not in tables or not required.issubset(_columns(conn, "part")):
        return {}
    labels: dict[str, str] = {}
    rows = conn.execute(
        """SELECT p.message_id,
                  json_extract(p.data,'$.type') AS part_type,
                  CASE WHEN json_extract(p.data,'$.type')='tool'
                       THEN json_extract(p.data,'$.tool') END AS tool_name
           FROM part p JOIN message m ON m.id=p.message_id
           WHERE json_valid(p.data) AND json_extract(m.data,'$.role')='assistant'
           ORDER BY p.time_created,p.id"""
    )
    for row in rows:
        message_id = str(row["message_id"])
        labels[message_id] = prefer_action_label(
            labels.get(message_id), safe_action_label(row["part_type"], row["tool_name"])
        ) or "Assistant response"
    return labels


def _source_message_ids(conn: sqlite3.Connection) -> tuple[set[str], int]:
    rows = conn.execute(
        """SELECT m.id,m.time_updated FROM message m
           WHERE json_extract(m.data,'$.role')='assistant'"""
    ).fetchall()
    return ({_ns(str(row["id"])) for row in rows}, max((int(row["time_updated"]) for row in rows), default=0))


def _delete_missing_source_rows(
    conn: sqlite3.Connection,
    *,
    session_ids: set[str],
    message_ids: set[str],
) -> None:
    """Remove stale OpenCode-derived rows and leave every other source alone."""

    # Compute the difference in Python so reconciliation is not constrained by
    # SQLite's host-parameter limit on large histories.
    stored_messages = {
        row[0] for row in conn.execute(
            """SELECT source_record_identity FROM usage
               WHERE source_event_type='opencode_assistant_message'
                 AND source_record_identity LIKE 'opencode:%'"""
        )
    }
    conn.executemany(
        """DELETE FROM usage WHERE source_record_identity=?
           AND source_event_type='opencode_assistant_message'""",
        ((identity,) for identity in stored_messages - message_ids),
    )
    stored_sessions = {
        row[0] for row in conn.execute(
            "SELECT thread_id FROM agents WHERE source_kind='opencode' AND thread_id LIKE 'opencode:%'"
        )
    }
    conn.executemany(
        "DELETE FROM agents WHERE thread_id=? AND source_kind='opencode'",
        ((identity,) for identity in stored_sessions - session_ids),
    )
    # Only source_app identifies an imported task as OpenCode.  This is vital
    # when a user happens to have a Codex ID with the same textual prefix.
    conn.execute(
        "DELETE FROM sessions WHERE source_app='opencode' AND id LIKE 'opencode:%' "
        "AND NOT EXISTS(SELECT 1 FROM agents WHERE agents.session_id=sessions.id)"
    )


def _refresh_opencode_turn_counts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """UPDATE sessions SET turn_count=(
               SELECT COUNT(*) FROM usage
               WHERE usage.session_id=sessions.id
                 AND usage.source_event_type='opencode_assistant_message'
           )
           WHERE source_app='opencode' AND id LIKE 'opencode:%'"""
    )


def ingest_opencode(settings: Settings, force_all: bool = False) -> OpenCodeIngestSummary:
    """Import OpenCode v2 accounting without mutating the OpenCode database.

    Stable message identifiers make changed-message upserts idempotent. Normal
    passes resume from a dashboard-owned update cursor; ``force_all`` rereads
    the complete source ledger, and both modes reconcile source deletions.
    """

    settings.validate()
    source_path = discover_opencode_database(settings)
    if source_path == settings.database.resolve():
        raise ValueError("dashboard database must not be the OpenCode source database")
    summary = OpenCodeIngestSummary(source_database=source_path)

    with _source_connection(source_path) as source:
        _validate_source_schema(source)
        # Keep session ancestry and its messages on one consistent WAL snapshot
        # while OpenCode may still be appending to the source database.
        source.execute("BEGIN")
        source_sessions = _source_rows(source)
        parents = {
            str(row["id"]): str(row["parent_id"]) if row["parent_id"] else None
            for row in source_sessions
        }
        root_info = {str(row["id"]): _root_for(str(row["id"]), parents) for row in source_sessions}
        agent_paths = _agent_paths(parents)

        initialize(settings.database)
        with database(settings.database) as target:
            session_columns = _dashboard_columns(target, "sessions")
            expected_session_columns = {"id", "root_thread_id", "title", "cwd", "repo_root", "repo_name"}
            if missing := expected_session_columns - session_columns:
                raise RuntimeError("dashboard sessions schema is missing: " + ", ".join(sorted(missing)))

            # Roots own dashboard tasks.  Children are represented as agents
            # in their root task, preserving OpenCode's immediate parent link.
            rows_by_id = {str(row["id"]): row for row in source_sessions}
            roots: dict[str, sqlite3.Row] = {}
            for row in source_sessions:
                root_external_id, _ = root_info[str(row["id"])]
                roots.setdefault(root_external_id, rows_by_id.get(root_external_id, row))
            for root_external_id, root_row in roots.items():
                root_id = _ns(root_external_id)
                _ensure_session(
                    target, session_columns, root_id=root_id, source_path=source_path,
                    version=root_row["version"],
                )
                _upsert_session_metadata(target, root_id, root_row)

            for row in source_sessions:
                external_id = str(row["id"])
                root_external_id, missing_parent = root_info[external_id]
                _upsert_agent(
                    target, row, root_id=_ns(root_external_id), root_external_id=root_external_id,
                    parent_id=row["parent_id"], agent_path=agent_paths[external_id],
                    orphan=missing_parent, source_path=source_path,
                )
            summary.scanned_sessions = len(source_sessions)

            # Reparenting can change the dashboard task that owns an existing
            # agent. Move older usage before obsolete roots are deleted; those
            # rows may be behind the incremental message cursor.
            target.execute(
                """UPDATE usage SET session_id=(
                       SELECT agents.session_id FROM agents
                       WHERE agents.thread_id=usage.thread_id
                   )
                   WHERE source_event_type='opencode_assistant_message'
                     AND thread_id IN (
                       SELECT thread_id FROM agents WHERE source_kind='opencode'
                   )"""
            )

            sync = target.execute(
                """SELECT cursor_timestamp FROM source_sync_state
                   WHERE source_app=? AND source_path=?""",
                (SOURCE_APP, str(source_path)),
            ).fetchone()
            needs_label_backfill = target.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM usage
                       WHERE source_event_type='opencode_assistant_message'
                         AND source_file=? AND call_label IS NULL
                   )""",
                (str(source_path),),
            ).fetchone()[0]
            cursor_timestamp = None if force_all or sync is None or needs_label_backfill else int(sync[0])
            source_message_ids, latest_message_timestamp = _source_message_ids(source)
            action_labels = _message_action_labels(source)
            for ordinal, row in enumerate(_message_rows(source, cursor_timestamp)):
                external_session_id = str(row["session_id"])
                root_external_id, _ = root_info[external_session_id]
                raw_input = _nonnegative_int(row["token_input"])
                cached = _nonnegative_int(row["token_cache_read"])
                cache_write = _nonnegative_int(row["token_cache_write"])
                reasoning = _nonnegative_int(row["token_reasoning"])
                raw_output = _nonnegative_int(row["token_output"])
                identity = _ns(str(row["id"]))
                model = str(row["model"] or row["session_model"] or "unknown-model")
                provider = str(row["provider"] or row["session_provider"] or "unknown-provider")
                call_label = action_labels.get(str(row["id"]), "Assistant response")
                normalized_input = raw_input + cached + cache_write
                normalized_output = raw_output + reasoning
                source_total = _nonnegative_int(row["token_total"])
                total = source_total or (normalized_input + normalized_output)
                values = (
                    identity, _ns(root_external_id), _ns(external_session_id), identity, identity,
                    _timestamp(row["time_created"]) or _timestamp(row["time_updated"]) or datetime.now(UTC).isoformat(),
                    model, provider, normalized_input, cached, cache_write, raw_input,
                    normalized_output, reasoning, total, str(source_path),
                    ordinal, "opencode_assistant_message", call_label, None, None, None, None, None,
                    format(_cost(row["cost"]), "f"), "reported by OpenCode",
                )
                exists = target.execute(
                    "SELECT 1 FROM usage WHERE source_record_identity=?", (identity,)
                ).fetchone() is not None
                target.execute(
                    """INSERT INTO usage(source_record_identity,session_id,thread_id,turn_id,response_id,
                       timestamp,model,provider,input_tokens,cached_input_tokens,cache_write_input_tokens,
                       uncached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,source_file,
                       source_ordinal,source_event_type,call_label,price_id,uncached_input_usd,cached_input_usd,
                       cache_write_usd,output_usd,cost_usd,pricing_note)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_record_identity) DO UPDATE SET
                         session_id=excluded.session_id,thread_id=excluded.thread_id,turn_id=excluded.turn_id,
                         response_id=excluded.response_id,timestamp=excluded.timestamp,model=excluded.model,
                         provider=excluded.provider,input_tokens=excluded.input_tokens,
                         cached_input_tokens=excluded.cached_input_tokens,
                         cache_write_input_tokens=excluded.cache_write_input_tokens,
                         uncached_input_tokens=excluded.uncached_input_tokens,output_tokens=excluded.output_tokens,
                         reasoning_output_tokens=excluded.reasoning_output_tokens,total_tokens=excluded.total_tokens,
                         source_file=excluded.source_file,source_ordinal=excluded.source_ordinal,
                         source_event_type=excluded.source_event_type,call_label=excluded.call_label,price_id=NULL,
                         uncached_input_usd=NULL,cached_input_usd=NULL,cache_write_usd=NULL,output_usd=NULL,
                         cost_usd=excluded.cost_usd,pricing_note=excluded.pricing_note""",
                    values,
                )
                if exists:
                    summary.duplicate_records += 1
                else:
                    summary.usage_records += 1
                    summary.recorded_spend += float(_cost(row["cost"]))

            _delete_missing_source_rows(
                target,
                session_ids={_ns(str(row["id"])) for row in source_sessions},
                message_ids=source_message_ids,
            )
            _refresh_opencode_turn_counts(target)
            target.execute(
                """INSERT INTO source_sync_state(
                       source_app,source_path,cursor_timestamp,last_successful_ingestion
                   ) VALUES(?,?,?,?)
                   ON CONFLICT(source_app,source_path) DO UPDATE SET
                     cursor_timestamp=excluded.cursor_timestamp,
                     last_successful_ingestion=excluded.last_successful_ingestion""",
                (
                    SOURCE_APP, str(source_path), latest_message_timestamp,
                    datetime.now(UTC).isoformat(),
                ),
            )
            scoped = "id LIKE 'opencode:%'"
            summary.root_sessions = int(target.execute(
                f"SELECT COUNT(*) FROM sessions WHERE {scoped}"
            ).fetchone()[0])
            summary.subagent_sessions = int(target.execute(
                "SELECT COUNT(*) FROM agents WHERE session_id LIKE 'opencode:%' AND parent_thread_id IS NOT NULL"
            ).fetchone()[0])
    return summary
