from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import database, initialize
from ..pricing import calculate_cost, seed_prices
from .codex_state import StateSnapshot, read_state, resolve_root
from .rollout import RolloutParser, context_from_row, context_json

log = logging.getLogger(__name__)
PARSER_VERSION = 3


@dataclass(slots=True)
class IngestSummary:
    scanned_files: int = 0
    root_sessions: int = 0
    subagent_sessions: int = 0
    usage_records: int = 0
    duplicate_records: int = 0
    malformed_lines: int = 0
    parser_warnings: int = 0
    unknown_models: set[str] = field(default_factory=set)
    unknown_prices: set[str] = field(default_factory=set)
    estimated_spend: float = 0.0


def _first_line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.strip().splitlines()[0:1]).strip()
    return value[:limit] or None


def _source_kind(value: Any) -> str:
    if isinstance(value, str) and not value.startswith("{"):
        return value
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        if isinstance(parsed, dict) and "subagent" in parsed:
            return "subagent"
    except json.JSONDecodeError:
        pass
    return "unknown"


def _repo_from_cwd(cwd: str | None) -> tuple[str | None, str | None]:
    if not cwd:
        return None, None
    path = Path(cwd)
    if path.exists():
        start = path if path.is_dir() else path.parent
        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                return str(candidate), candidate.name
    return cwd, Path(cwd).name or cwd


def _ensure_session(conn: sqlite3.Connection, root_id: str, settings: Settings) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sessions(id,root_thread_id,source_app,source_home) VALUES(?,?,'codex',?)",
        (root_id, root_id, str(settings.codex_home)),
    )


def _sync_state(
    conn: sqlite3.Connection,
    snapshot: StateSnapshot,
    settings: Settings,
    available_rollout_names: set[str],
) -> dict[str, str]:
    parents = dict(snapshot.edges)
    # Relationships learned from rollout session_meta are more complete than
    # some older state DB snapshots. Preserve them on later incremental scans.
    for existing in conn.execute(
        "SELECT thread_id,parent_thread_id FROM agents WHERE parent_thread_id IS NOT NULL"
    ):
        parents.setdefault(existing["thread_id"], existing["parent_thread_id"])
    by_id = {row["id"]: row for row in snapshot.threads}
    roots: set[str] = set()
    for row in snapshot.threads:
        root, cycle = resolve_root(row["id"], parents)
        roots.add(root)
        _ensure_session(conn, root, settings)
        root_row = by_id.get(root, row if root == row["id"] else {})
        repo_root, repo_name = _repo_from_cwd(root_row.get("cwd"))
        title = _first_line(root_row.get("name") or root_row.get("title") or root_row.get("first_user_message"), 200)
        preview = (
            _first_line(root_row.get("first_user_message"), settings.preview_chars) if settings.keep_preview else None
        )
        conn.execute(
            """UPDATE sessions SET
               title=COALESCE(?,title), first_user_message_preview=COALESCE(?,first_user_message_preview),
               cwd=COALESCE(?,cwd), repo_root=COALESCE(?,repo_root), repo_name=COALESCE(?,repo_name),
               git_branch=COALESCE(?,git_branch), git_commit=COALESCE(?,git_commit),
               git_origin_url=COALESCE(?,git_origin_url), created_at=COALESCE(?,created_at),
               updated_at=CASE WHEN updated_at IS NULL OR ?>updated_at THEN ? ELSE updated_at END,
               root_model=COALESCE(?,root_model), root_reasoning_effort=COALESCE(?,root_reasoning_effort),
               root_provider=COALESCE(?,root_provider), source_version=COALESCE(?,source_version),
               parser_warnings=parser_warnings+?
               WHERE id=?""",
            (
                title, preview, root_row.get("cwd"), repo_root, repo_name,
                root_row.get("git_branch"), root_row.get("git_sha"), root_row.get("git_origin_url"),
                root_row.get("created_iso"), row.get("updated_iso"), row.get("updated_iso"),
                root_row.get("model"), root_row.get("reasoning_effort"), root_row.get("model_provider"),
                root_row.get("cli_version"), 1 if cycle else 0, root,
            ),
        )
        spawn = row.get("structured_spawn") or {}
        parent = parents.get(row["id"])
        orphan = int(_source_kind(row.get("source")) == "subagent" and not parent)
        agent_path = row.get("agent_path") or spawn.get("agent_path")
        role = row.get("agent_role") or spawn.get("agent_role") or ("root" if row["id"] == root else "subagent")
        nickname = row.get("agent_nickname") or spawn.get("agent_nickname")
        rollout_path = row.get("rollout_path")
        source_available = int(
            bool(rollout_path)
            and (Path(rollout_path).is_file() or Path(rollout_path).name in available_rollout_names)
        )
        if not source_available:
            _warn(
                conn,
                Path(rollout_path).name if rollout_path else row["id"],
                -1,
                "missing_rollout",
                "state thread references no readable rollout; accounting is incomplete",
                emit_log=False,
            )
        conn.execute(
            """INSERT INTO agents(thread_id,session_id,parent_thread_id,agent_role,agent_nickname,
               agent_path,created_at,updated_at,model,model_provider,reasoning_effort,
               source_rollout_path,source_kind,orphan,source_tokens_used,source_available)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 parent_thread_id=COALESCE(excluded.parent_thread_id,agents.parent_thread_id),
                 agent_role=COALESCE(agents.agent_role,excluded.agent_role),
                 agent_nickname=COALESCE(excluded.agent_nickname,agents.agent_nickname),
                 agent_path=COALESCE(excluded.agent_path,agents.agent_path),
                 created_at=COALESCE(agents.created_at,excluded.created_at),updated_at=excluded.updated_at,
                 model=COALESCE(excluded.model,agents.model),model_provider=COALESCE(excluded.model_provider,agents.model_provider),
                 reasoning_effort=COALESCE(excluded.reasoning_effort,agents.reasoning_effort),
                 source_rollout_path=COALESCE(excluded.source_rollout_path,agents.source_rollout_path),
                 source_kind=excluded.source_kind,orphan=excluded.orphan,
                 source_tokens_used=COALESCE(excluded.source_tokens_used,agents.source_tokens_used),
                 source_available=excluded.source_available""",
            (
                row["id"], root, parent, role, nickname, agent_path,
                row.get("created_iso"), row.get("updated_iso"), row.get("model"),
                row.get("model_provider"), row.get("reasoning_effort"), row.get("rollout_path"),
                _source_kind(row.get("source")), orphan, row.get("tokens_used"), source_available,
            ),
        )
    return parents


def discover_rollouts(codex_home: Path) -> list[Path]:
    found: dict[str, Path] = {}
    for directory in (codex_home / "sessions", codex_home / "archived_sessions"):
        if not directory.exists():
            continue
        for path in directory.rglob("rollout-*.jsonl"):
            # Prefer the active-session copy if an archive move is observed mid-scan.
            found.setdefault(path.name, path)
            if "archived_sessions" not in path.parts:
                found[path.name] = path
    return sorted(found.values(), key=lambda p: p.name)


def _owner_by_filename(snapshot: StateSnapshot) -> dict[str, str]:
    result = {}
    for row in snapshot.threads:
        rollout = row.get("rollout_path")
        if rollout:
            result[Path(rollout).name] = row["id"]
    return result


def _warn(
    conn: sqlite3.Connection,
    source_key: str,
    ordinal: int | None,
    code: str,
    message: str,
    *,
    emit_log: bool = True,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO parser_warnings(source_key,source_ordinal,timestamp,code,message) "
        "VALUES(?,?,?,?,?)",
        (source_key, -1 if ordinal is None else ordinal, datetime.now(UTC).isoformat(), code, message[:500]),
    )
    if emit_log:
        log.warning("%s (%s): %s", code, source_key, message)


def _root_for(conn: sqlite3.Connection, thread_id: str, parents: dict[str, str]) -> str:
    if thread_id in parents:
        root, _ = resolve_root(thread_id, parents)
        return root
    found = conn.execute("SELECT session_id FROM agents WHERE thread_id=?", (thread_id,)).fetchone()
    if found:
        return found[0]
    root, _ = resolve_root(thread_id, parents)
    return root


def _ensure_rollout_agent(
    conn: sqlite3.Connection,
    thread_id: str,
    parents: dict[str, str],
    settings: Settings,
    *,
    model: str | None = None,
    provider: str | None = None,
    source_path: str | None = None,
    reasoning_effort: str | None = None,
    created_at: str | None = None,
) -> str:
    root = _root_for(conn, thread_id, parents)
    _ensure_session(conn, root, settings)
    conn.execute(
        """INSERT INTO agents(thread_id,session_id,parent_thread_id,agent_role,model,model_provider,
           reasoning_effort,source_rollout_path,created_at,source_available)
           VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(thread_id) DO UPDATE SET
           parent_thread_id=COALESCE(excluded.parent_thread_id,agents.parent_thread_id),
           model=COALESCE(excluded.model,agents.model),model_provider=COALESCE(excluded.model_provider,agents.model_provider),
           reasoning_effort=COALESCE(excluded.reasoning_effort,agents.reasoning_effort),
           source_rollout_path=COALESCE(excluded.source_rollout_path,agents.source_rollout_path),
           created_at=COALESCE(agents.created_at,excluded.created_at),source_available=1""",
        (thread_id, root, parents.get(thread_id), "root" if thread_id == root else "subagent",
         model, provider, reasoning_effort, source_path, created_at),
    )
    if thread_id == root:
        conn.execute(
            """UPDATE sessions SET root_model=COALESCE(root_model,?),
               root_reasoning_effort=COALESCE(root_reasoning_effort,?),
               root_provider=COALESCE(root_provider,?),created_at=COALESCE(created_at,?) WHERE id=?""",
            (model, reasoning_effort, provider, created_at, root),
        )
    return root


def _apply_session_meta(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    parents: dict[str, str],
    settings: Settings,
    source_path: Path,
    source_timestamp: str | None,
) -> None:
    thread_id = payload.get("id")
    if not isinstance(thread_id, str):
        return
    source = payload.get("source")
    explicit_parent = payload.get("parent_thread_id")
    if isinstance(explicit_parent, str) and explicit_parent:
        parents[thread_id] = explicit_parent
    if isinstance(source, dict):
        try:
            spawn = source["subagent"]["thread_spawn"]
            parent = spawn.get("parent_thread_id")
            if isinstance(parent, str):
                parents.setdefault(thread_id, parent)
        except (KeyError, TypeError):
            pass
    root_hint = payload.get("session_id")
    if thread_id not in parents and isinstance(root_hint, str) and root_hint and root_hint != thread_id:
        _ensure_session(conn, root_hint, settings)
        conn.execute(
            "INSERT OR IGNORE INTO agents(thread_id,session_id,agent_role,orphan) VALUES(?,?,?,1)",
            (thread_id, root_hint, "subagent"),
        )
        conn.execute("UPDATE agents SET session_id=?,orphan=1 WHERE thread_id=?", (root_hint, thread_id))
    root = _ensure_rollout_agent(
        conn, thread_id, parents, settings,
        provider=payload.get("model_provider") if isinstance(payload.get("model_provider"), str) else None,
        source_path=str(source_path),
        created_at=source_timestamp,
    )
    if thread_id in parents:
        role = None
        try:
            other = source["subagent"]["other"] if isinstance(source, dict) else None
            role = other if isinstance(other, str) else None
        except (KeyError, TypeError):
            pass
        conn.execute(
            "UPDATE agents SET parent_thread_id=?,orphan=0,agent_role=COALESCE(?,agent_role) WHERE thread_id=?",
            (parents[thread_id], role, thread_id),
        )
    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    repo_root, repo_name = _repo_from_cwd(cwd)
    conn.execute(
        """UPDATE sessions SET cwd=COALESCE(cwd,?),repo_root=COALESCE(repo_root,?),repo_name=COALESCE(repo_name,?),
           git_branch=COALESCE(git_branch,?),git_commit=COALESCE(git_commit,?),git_origin_url=COALESCE(git_origin_url,?),
           source_version=COALESCE(source_version,?),root_provider=COALESCE(root_provider,?),
           created_at=COALESCE(created_at,?) WHERE id=?""",
        (cwd, repo_root, repo_name, git.get("branch"), git.get("commit_hash"), git.get("repository_url"),
         payload.get("cli_version"), payload.get("model_provider"), source_timestamp, root),
    )


def _reconcile_session_ownership(
    conn: sqlite3.Connection, parents: dict[str, str], settings: Settings
) -> None:
    """Move agents and their ledger rows together after relationship changes."""
    codex_agents = """SELECT a.thread_id,a.parent_thread_id,a.session_id,a.orphan
                      FROM agents a JOIN sessions s ON s.id=a.session_id
                      WHERE s.source_app='codex'"""
    for row in conn.execute(codex_agents):
        if row["parent_thread_id"]:
            parents[row["thread_id"]] = row["parent_thread_id"]
    for row in conn.execute(codex_agents):
        thread_id = row["thread_id"]
        if thread_id not in parents and row["orphan"] and row["session_id"] != thread_id:
            root = row["session_id"]
        else:
            root, _ = resolve_root(thread_id, parents)
        _ensure_session(conn, root, settings)
        if row["session_id"] != root:
            conn.execute(
                """INSERT OR IGNORE INTO session_tags(session_id,tag_id)
                   SELECT ?,tag_id FROM session_tags WHERE session_id=?""",
                (root, row["session_id"]),
            )
        conn.execute("UPDATE agents SET session_id=? WHERE thread_id=?", (root, thread_id))
        conn.execute("UPDATE usage SET session_id=? WHERE thread_id=?", (root, thread_id))


def _scan_file(
    conn: sqlite3.Connection,
    path: Path,
    parents: dict[str, str],
    owners: dict[str, str],
    settings: Settings,
    summary: IngestSummary,
    force_all: bool,
) -> None:
    source_key = path.name
    conn.execute(
        "DELETE FROM parser_warnings WHERE source_key=? AND code='missing_rollout'",
        (source_key,),
    )
    try:
        before = path.stat()
    except OSError as exc:
        _warn(conn, source_key, None, "source_stat_failed", str(exc))
        summary.parser_warnings += 1
        return
    state = conn.execute("SELECT * FROM ingestion_state WHERE source_key=?", (source_key,)).fetchone()
    parser_changed = state is not None and state["parser_version"] != PARSER_VERSION
    start = 0 if force_all or state is None or parser_changed else state["last_offset"]
    if state is not None and (before.st_size < start or (state["inode"] and state["inode"] != before.st_ino)):
        _warn(
            conn, source_key, None, "source_reset", "source inode changed or file shrank; rescanning with deduplication"
        )
        summary.parser_warnings += 1
        start = 0
        state = None
    context = context_from_row(None if force_all or parser_changed else state)
    context.owner_thread_id = context.owner_thread_id or owners.get(source_key)
    parser = RolloutParser(context)
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(max(0, before.st_size - start))
    except OSError as exc:
        _warn(conn, source_key, None, "source_read_failed", str(exc))
        summary.parser_warnings += 1
        return
    complete_bytes = len(data)
    if data and not data.endswith(b"\n"):
        newline = data.rfind(b"\n")
        complete_bytes = newline + 1 if newline >= 0 else 0
    complete = data[:complete_bytes]
    for raw in complete.splitlines():
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            summary.malformed_lines += 1
            _warn(conn, source_key, None, "malformed_jsonl", str(exc))
            continue
        result = parser.parse(record, source_key)
        ordinal = record.get("ordinal") if isinstance(record, dict) else None
        if result.warning:
            summary.parser_warnings += 1
            _warn(conn, source_key, ordinal, *result.warning)
        if result.session_meta:
            _apply_session_meta(
                conn, result.session_meta, parents, settings, path, result.session_timestamp
            )
        if not result.usage:
            continue
        parsed = result.usage
        root = _ensure_rollout_agent(
            conn, parsed.thread_id, parents, settings, model=parsed.model,
            provider=parsed.provider, source_path=str(path),
            reasoning_effort=parsed.reasoning_effort,
        )
        cost = calculate_cost(
            conn, parsed.usage, parsed.model, parsed.provider, parsed.timestamp, parsed.service_tier
        )
        values = (
            parsed.identity, root, parsed.thread_id, parsed.turn_id, parsed.response_id,
            parsed.timestamp, parsed.model, parsed.provider, parsed.service_tier, parsed.usage.input_tokens,
            parsed.usage.cached_input_tokens, parsed.usage.cache_write_input_tokens,
            parsed.usage.uncached_input_tokens, parsed.usage.output_tokens,
            parsed.usage.reasoning_output_tokens, parsed.usage.total_tokens, str(path),
            parsed.ordinal, parsed.event_type, parsed.call_label, cost.price_id,
            str(cost.uncached_input_usd) if cost.uncached_input_usd is not None else None,
            str(cost.cached_input_usd) if cost.cached_input_usd is not None else None,
            str(cost.cache_write_usd) if cost.cache_write_usd is not None else None,
            str(cost.output_usd) if cost.output_usd is not None else None,
            str(cost.total_usd) if cost.total_usd is not None else None,
            cost.note,
        )
        cursor = conn.execute(
            """INSERT OR IGNORE INTO usage(source_record_identity,session_id,thread_id,turn_id,response_id,
               timestamp,model,provider,service_tier,input_tokens,cached_input_tokens,cache_write_input_tokens,
               uncached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,source_file,
               source_ordinal,source_event_type,call_label,price_id,uncached_input_usd,cached_input_usd,
               cache_write_usd,output_usd,cost_usd,pricing_note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        if cursor.rowcount:
            summary.usage_records += 1
            if cost.total_usd is None:
                summary.unknown_prices.add(f"{parsed.provider}:{parsed.model}")
            else:
                summary.estimated_spend += float(cost.total_usd)
        else:
            if parsed.call_label:
                conn.execute(
                    "UPDATE usage SET call_label=? WHERE source_record_identity=? AND call_label IS NULL",
                    (parsed.call_label, parsed.identity),
                )
            if parsed.service_tier:
                summary.estimated_spend += _backfill_service_tier(conn, parsed.identity, parsed.service_tier, cost)
            summary.duplicate_records += 1
    end_offset = start + complete_bytes
    previous_json = context_json(parser.context.previous_cumulative)
    recent_json = json.dumps(parser.context.recent_atomic) if parser.context.recent_atomic else None
    conn.execute(
        """INSERT INTO ingestion_state(source_key,source_path,inode,last_offset,mtime_ns,size,parser_version,
           last_successful_ingestion,owner_thread_id,current_turn_id,current_model,current_reasoning_effort,
           current_provider,current_service_tier,previous_cumulative_json,recent_atomic_json,pending_call_label,
           pending_call_priority)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET
           source_path=excluded.source_path,inode=excluded.inode,last_offset=excluded.last_offset,
           mtime_ns=excluded.mtime_ns,size=excluded.size,parser_version=excluded.parser_version,
           last_successful_ingestion=excluded.last_successful_ingestion,owner_thread_id=excluded.owner_thread_id,
           current_turn_id=excluded.current_turn_id,current_model=excluded.current_model,
           current_reasoning_effort=excluded.current_reasoning_effort,current_provider=excluded.current_provider,
           current_service_tier=excluded.current_service_tier,previous_cumulative_json=excluded.previous_cumulative_json,recent_atomic_json=excluded.recent_atomic_json,
           pending_call_label=excluded.pending_call_label,pending_call_priority=excluded.pending_call_priority""",
        (
            source_key, str(path), before.st_ino, end_offset, before.st_mtime_ns, before.st_size,
            PARSER_VERSION, datetime.now(UTC).isoformat(), parser.context.owner_thread_id,
            parser.context.turn_id, parser.context.model, parser.context.reasoning_effort,
            parser.context.provider, parser.context.service_tier, previous_json, recent_json,
            parser.context.pending_call_label, parser.context.pending_call_priority,
        ),
    )


def _backfill_service_tier(conn: sqlite3.Connection, identity: str, tier: str, cost) -> float:
    """Record the tier of a call ingested before tiers were parsed and reprice it.

    Returns the change in cost_usd (new_cost - old_cost) if the row was updated.
    """
    old_row = conn.execute(
        "SELECT cost_usd FROM usage WHERE source_record_identity=? AND service_tier IS NULL AND source_event_type NOT GLOB 'opencode_*'",
        (identity,)
    ).fetchone()
    cursor = conn.execute(
        """UPDATE usage SET service_tier=?,price_id=?,uncached_input_usd=?,cached_input_usd=?,
           cache_write_usd=?,output_usd=?,cost_usd=?,pricing_note=?
           WHERE source_record_identity=? AND service_tier IS NULL AND source_event_type NOT GLOB 'opencode_*'""",
        (
            tier, cost.price_id,
            *(str(value) if value is not None else None for value in (
                cost.uncached_input_usd, cost.cached_input_usd, cost.cache_write_usd, cost.output_usd, cost.total_usd,
            )),
            cost.note, identity,
        ),
    )
    if cursor.rowcount > 0 and old_row:
        old_cost = float(old_row[0]) if old_row[0] is not None else 0.0
        new_cost = float(cost.total_usd) if cost.total_usd is not None else 0.0
        return max(0.0, new_cost - old_cost)
    return 0.0


def _refresh_sessions(conn: sqlite3.Connection, settings: Settings) -> None:
    now = datetime.now(UTC).timestamp()
    rows = conn.execute(
        "SELECT id,updated_at FROM sessions WHERE source_app='codex'"
    ).fetchall()
    for row in rows:
        latest = conn.execute(
            "SELECT MAX(timestamp),COUNT(DISTINCT turn_id) FROM usage WHERE session_id=?", (row["id"],)
        ).fetchone()
        candidates = [value for value in (latest[0], row["updated_at"]) if value]
        updated = max(candidates) if candidates else None
        running = False
        if updated:
            try:
                updated_ts = datetime.fromisoformat(updated.replace("Z", "+00:00")).timestamp()
                running = now - updated_ts <= settings.running_window_seconds
            except ValueError:
                pass
        coverage = conn.execute(
            """SELECT COUNT(*),SUM(source_available=0),
               SUM(CASE WHEN source_available=0 THEN COALESCE(source_tokens_used,0) ELSE 0 END)
               FROM agents WHERE session_id=?""",
            (row["id"],),
        ).fetchone()
        missing = int(coverage[1] or 0)
        if missing:
            accounting_status = "partial" if latest[0] else "unavailable"
            accounting_note = (
                f"{missing} source rollout(s) unavailable; "
                f"{int(coverage[2] or 0)} state tokens lack detailed accounting"
            )
        else:
            accounting_status, accounting_note = "complete", None
        conn.execute(
            """UPDATE sessions SET updated_at=COALESCE(?,updated_at),finished_at=?,status=?,turn_count=?,
               accounting_status=?,accounting_note=? WHERE id=?""",
            (updated, None if running else updated, "running" if running else "completed",
             latest[1] or 0, accounting_status, accounting_note, row["id"]),
        )


def ingest(settings: Settings, *, force_all: bool = False) -> IngestSummary:
    settings.validate()
    initialize(settings.database)
    snapshot = read_state(settings.codex_home)
    summary = IngestSummary()
    with database(settings.database) as conn:
        seed_prices(conn)
        if not settings.keep_preview:
            conn.execute("UPDATE sessions SET first_user_message_preview=NULL")
        rollouts = discover_rollouts(settings.codex_home)
        parents = _sync_state(conn, snapshot, settings, {path.name for path in rollouts})
        owners = _owner_by_filename(snapshot)
        summary.scanned_files = len(rollouts)
        for path in rollouts:
            _scan_file(conn, path, parents, owners, settings, summary, force_all)
        _reconcile_session_ownership(conn, parents, settings)
        conn.execute(
            "DELETE FROM sessions WHERE NOT EXISTS(SELECT 1 FROM agents a WHERE a.session_id=sessions.id)"
        )
        _refresh_sessions(conn, settings)
        counts = conn.execute(
            """SELECT COUNT(*),SUM(CASE WHEN a.parent_thread_id IS NOT NULL THEN 1 ELSE 0 END)
               FROM agents a JOIN sessions s ON s.id=a.session_id WHERE s.source_app='codex'"""
        ).fetchone()
        summary.subagent_sessions = int(counts[1] or 0)
        summary.root_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_app='codex'"
        ).fetchone()[0]
        summary.unknown_models = {
            r[0] for r in conn.execute(
                """SELECT DISTINCT u.model FROM usage u JOIN sessions s ON s.id=u.session_id
                   WHERE s.source_app='codex' AND u.model='unknown-model'"""
            )
        }
        summary.unknown_prices.update(
            f"{r[0]}:{r[1]}" for r in conn.execute(
                """SELECT DISTINCT u.provider,u.model FROM usage u JOIN sessions s ON s.id=u.session_id
                   WHERE s.source_app='codex' AND u.price_id IS NULL"""
            )
        )
    return summary
