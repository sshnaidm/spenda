"""Privacy-preserving, read-only ingestion of Cursor agent history.

Cursor keeps three kinds of local history that this adapter reads:

* agent transcripts under ``<cursor-home>/projects/<slug>/agent-transcripts``
  (one JSONL file per agent session, plus ``subagents/*.jsonl``), written by
  both the editor and the ``cursor-agent`` CLI;
* the small ``meta`` table of each CLI conversation store under
  ``<cursor-home>/chats/<workspace>/<session>/store.db``;
* composer and bubble metadata in the editor's ``globalStorage/state.vscdb``.

Only envelope metadata is retained: roles, content-block types, tool names,
timestamps, model names, workspace paths, generated titles, and the token
counts Cursor records for editor bubbles.  Prompt text, assistant text,
thinking, tool arguments and results, the CLI ``blobs`` table, and the
editor's ``ItemTable`` (which holds credentials) are never read into the
dashboard.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Settings, _default_cursor_home, _default_cursor_user_dir
from ..db import database, initialize
from ..models import TokenUsage
from ..pricing import calculate_cost, seed_prices
from .action_labels import prefer_action_label, safe_action_label

SOURCE_APP = "cursor"
PROVIDER = "cursor"
_PREFIX = "cursor:"
_ASSISTANT_EVENT = "cursor_assistant_message"
_COMPOSER_MARK = "cursor:composer:"
_NO_ACCOUNTING_NOTE = "Cursor keeps no token or cost accounting in its local history"
# Bump when the values derived from a transcript change, so recorded
# fingerprints from an older parser stop suppressing a reread.
PARSER_VERSION = 1
_USER_BUBBLE = 1
_ASSISTANT_BUBBLE = 2


@dataclass(slots=True)
class CursorIngestSummary:
    scanned_files: int = 0
    unchanged_files: int = 0
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
    source_home: Path | None = None
    source_user_dir: Path | None = None
    editor_composers: int = 0
    cli_chats: int = 0


@dataclass(slots=True)
class _Transcript:
    path: Path
    slug: str
    root_external_id: str
    agent_external_id: str | None
    updated_at: str | None = None

    @property
    def is_subagent(self) -> bool:
        return self.agent_external_id is not None

    @property
    def external_id(self) -> str:
        return self.agent_external_id or self.root_external_id

    @property
    def thread_id(self) -> str:
        return _thread_id(self.root_external_id, self.agent_external_id)


@dataclass(slots=True)
class _Call:
    """One assistant message of a transcript; never its content."""

    ordinal: int
    call_label: str


@dataclass(slots=True)
class _Composer:
    """Scalar session metadata from the editor state or a CLI store."""

    external_id: str
    title: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    cwd: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    parent_id: str | None = None
    is_draft: bool = False
    # Raw lastUpdatedAt from the editor; recorded after a successful read so a
    # later pass can tell that the composer changed even when its transcript
    # did not.
    updated_mark: int | None = None


@dataclass(slots=True)
class _BubbleCall:
    """One model response reconstructed from consecutive editor bubbles."""

    bubble_id: str
    timestamp: str | None
    model: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    call_label: str | None = None
    model_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Fingerprint:
    inode: int
    size: int
    mtime_ns: int


def discover_cursor_home(settings: Settings) -> Path:
    """Return Cursor's agent history directory from the shared settings object."""

    configured = getattr(settings, "cursor_home", None)
    if configured:
        return Path(configured).expanduser().resolve()
    return _default_cursor_home().resolve()


def discover_cursor_user_dir(settings: Settings) -> Path:
    """Return the Cursor editor's per-user data directory."""

    configured = getattr(settings, "cursor_user_dir", None)
    if configured:
        return Path(configured).expanduser().resolve()
    return _default_cursor_user_dir().resolve()


def editor_state_path(user_dir: Path) -> Path:
    return user_dir / "globalStorage" / "state.vscdb"


def cursor_sources_present(settings: Settings) -> bool:
    """Return whether any Cursor history location exists on this machine."""

    home = discover_cursor_home(settings)
    return (
        (home / "projects").is_dir()
        or (home / "chats").is_dir()
        or editor_state_path(discover_cursor_user_dir(settings)).is_file()
    )


def _ns(value: str) -> str:
    return value if value.startswith(_PREFIX) else f"{_PREFIX}{value}"


def _thread_id(root_external_id: str, agent_external_id: str | None) -> str:
    if agent_external_id:
        return _ns(f"{root_external_id}:agent:{agent_external_id}")
    return _ns(root_external_id)


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_scalar(value: Any, limit: int = 100) -> str | None:
    """Keep bounded scalar metadata, never an arbitrary payload."""

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] or None


def _timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _latest(*values: str | None) -> str | None:
    return max((item for item in values if item), default=None)


def _earliest(*values: str | None) -> str | None:
    return min((item for item in values if item), default=None)


def _fingerprint(path: Path) -> _Fingerprint | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _Fingerprint(stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _mtime(path: Path) -> str | None:
    try:
        return _timestamp(path.stat().st_mtime)
    except OSError:
        return None


def _warn(conn: sqlite3.Connection, source_key: str, code: str, message: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO parser_warnings(source_key,source_ordinal,timestamp,code,message) "
        "VALUES(?,?,?,?,?)",
        (source_key, -1, datetime.now(UTC).isoformat(), code, message[:500]),
    )


# --------------------------------------------------------------------------
# Transcripts: <home>/projects/<slug>/agent-transcripts/<id>/<id>.jsonl
# --------------------------------------------------------------------------


def _transcript_for(path: Path, projects: Path) -> _Transcript | None:
    try:
        relative = path.relative_to(projects)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 4 or parts[1] != "agent-transcripts" or not parts[-1].endswith(".jsonl"):
        return None
    slug, root = parts[0], parts[2]
    if len(parts) == 4 and parts[3] == f"{root}.jsonl":
        return _Transcript(path, slug, root, None)
    if len(parts) == 5 and parts[3] == "subagents":
        return _Transcript(path, slug, root, path.stem)
    return None


def _transcript_paths(home: Path, summary: CursorIngestSummary) -> tuple[list[Path], bool]:
    """Return transcript paths and whether the directory walk was complete."""

    projects = home / "projects"
    if not projects.is_dir():
        # Absence is not evidence that history was deleted; keep imported rows.
        return [], False
    found: list[Path] = []
    complete = True

    def unreadable(_error: OSError) -> None:
        nonlocal complete
        complete = False
        summary.parser_warnings += 1

    try:
        for directory, _subdirectories, filenames in os.walk(projects, onerror=unreadable, followlinks=False):
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = Path(directory) / filename
                if _transcript_for(path, projects) is not None:
                    found.append(path)
    except OSError:
        summary.parser_warnings += 1
        return [], False
    return sorted(found), complete


def _message_action_label(message: dict[str, Any]) -> str:
    """Inspect only block types and tool names; never retain block content."""

    content = message.get("content")
    if isinstance(content, str):
        return "Assistant response"
    label = None
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict):
            label = prefer_action_label(label, safe_action_label(block.get("type"), block.get("name")))
    return label or "Assistant response"


def _read_transcript(transcript: _Transcript, summary: CursorIngestSummary) -> tuple[list[_Call], bool, bool]:
    """Return assistant calls, whether every line parsed, and readability."""

    calls: list[_Call] = []
    try:
        handle = transcript.path.open("rb")
    except OSError:
        summary.parser_warnings += 1
        return calls, False, False
    complete = True
    with handle:
        for ordinal, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                summary.malformed_lines += 1
                complete = False
                continue
            if not isinstance(record, dict) or record.get("role") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            calls.append(_Call(ordinal, _message_action_label(message)))
    return calls, complete, True


# --------------------------------------------------------------------------
# Workspace slugs: "home-user-sources-project" -> /home/user/sources/project
# --------------------------------------------------------------------------


def _slug_token(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")


def resolve_slug(slug: str, root: Path | None = None) -> Path | None:
    """Recover a local working directory from Cursor's flattened project slug.

    Cursor replaces every path separator and punctuation character with ``-``,
    so the slug alone is ambiguous.  The resolver walks the local filesystem
    from the root and accepts the longest directory name that matches the next
    slug tokens, which makes ``ai-shell`` and ``project.code-workspace``
    resolvable without any table of known projects.
    """

    tokens = [token for token in slug.split("-") if token]
    if not tokens or slug == "empty-window":
        return None
    if root is None:
        if os.name == "nt":
            root = Path(f"{tokens[0].upper()}:\\")
            tokens = tokens[1:]
        else:
            root = Path("/")

    def walk(directory: Path, remaining: list[str], depth: int) -> Path | None:
        if not remaining:
            return directory
        if depth > 48:
            return None
        try:
            entries: dict[str, os.DirEntry[str]] = {}
            for entry in os.scandir(directory):
                entries.setdefault(_slug_token(entry.name), entry)
        except OSError:
            return None
        for count in range(len(remaining), 0, -1):
            entry = entries.get("-".join(remaining[:count]))
            if entry is None:
                continue
            rest = remaining[count:]
            if not rest:
                return Path(entry.path)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                found = walk(Path(entry.path), rest, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(root, tokens, 0)


def _project_name(cwd: str | None, slug: str) -> str | None:
    if cwd:
        name = Path(cwd).name
        return name.removesuffix(".code-workspace") or name
    return None if slug == "empty-window" else slug


# --------------------------------------------------------------------------
# CLI stores: <home>/chats/<workspace>/<session>/store.db (meta table only)
# --------------------------------------------------------------------------


@contextmanager
def _readonly(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.2)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _decode_meta(value: Any) -> dict[str, Any] | None:
    """Decode a CLI store ``meta`` value, which is JSON or hex-encoded JSON."""

    raw = value
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped and len(stripped) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", stripped):
            try:
                raw = bytes.fromhex(stripped)
            except ValueError:
                raw = stripped
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def cli_store_paths(home: Path) -> list[Path]:
    chats = home / "chats"
    found: list[Path] = []
    try:
        for workspace in os.scandir(chats):
            if not workspace.is_dir(follow_symlinks=False):
                continue
            for session in os.scandir(workspace.path):
                store = Path(session.path) / "store.db"
                if session.is_dir(follow_symlinks=False) and store.is_file():
                    found.append(store)
    except OSError:
        return found
    return sorted(found)


def _cli_store_for(home: Path, session_id: str) -> Path | None:
    chats = home / "chats"
    try:
        workspaces = [entry for entry in os.scandir(chats) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for workspace in workspaces:
        store = Path(workspace.path) / session_id / "store.db"
        if store.is_file():
            return store
    return None


def _read_cli_store(path: Path, session_id: str) -> _Composer | None:
    """Read scalar session metadata from a CLI store; never the blobs table.

    A store that cannot be opened or queried (for example one locked by a
    running CLI) raises ``sqlite3.DatabaseError`` so the caller can retry the
    unit later instead of recording the absence of metadata as final.
    """

    with _readonly(path) as conn:
        if "meta" not in _tables(conn) or not {"key", "value"} <= _columns(conn, "meta"):
            return None
        rows = conn.execute("SELECT value FROM meta").fetchall()
    for row in rows:
        meta = _decode_meta(row[0])
        if meta is None:
            continue
        agent_id = meta.get("agentId")
        if isinstance(agent_id, str) and agent_id and agent_id != session_id:
            continue
        return _Composer(
            session_id,
            title=_safe_scalar(meta.get("name"), 200),
            model=_safe_scalar(meta.get("lastUsedModel")),
            created_at=_timestamp(meta.get("createdAt")),
        )
    return None


# --------------------------------------------------------------------------
# Editor state: <user-dir>/globalStorage/state.vscdb
# --------------------------------------------------------------------------


def _effort_from_parameters(value: Any) -> str | None:
    """Return the reasoning setting from Cursor's selected-model parameters."""

    try:
        parameters = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None
    if not isinstance(parameters, list):
        return None
    for item in parameters:
        if isinstance(item, dict) and item.get("id") in {"effort", "reasoning", "thinking"}:
            setting = item.get("value")
            if isinstance(setting, str) and setting not in {"false", ""}:
                return _safe_scalar(setting, 40)
    return None


def _id_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str) and item] if isinstance(parsed, list) else []


def _read_editor_composers(conn: sqlite3.Connection) -> tuple[dict[str, _Composer], set[str]]:
    """Extract composer scalars inside SQLite; the JSON document never leaves it.

    Returns the decoded composers and the identifiers of composer documents
    that exist but could not be decoded, so a corrupt document is never
    mistaken for a deleted session.
    """

    if "cursorDiskKV" not in _tables(conn) or not {"key", "value"} <= _columns(conn, "cursorDiskKV"):
        return {}, set()
    composers: dict[str, _Composer] = {}
    children: dict[str, str] = {}
    # One json_extract call parses each document once.  Documents can be
    # large in older Cursor releases, so the validated query is a fallback.
    fields = (
        "'$.name','$.createdAt','$.lastUpdatedAt','$.modelConfig.modelName',"
        "'$.modelConfig.selectedModels[0].modelId','$.modelConfig.selectedModels[0].parameters',"
        "'$.workspaceIdentifier.uri.fsPath','$.subagentComposerIds','$.subComposerIds','$.isDraft'"
    )
    source = (
        "(SELECT key,CAST(value AS TEXT) AS document FROM cursorDiskKV "
        "WHERE key>'composerData:' AND key<'composerData;')"
    )
    select = f"SELECT substr(key,length('composerData:')+1) AS composer_id,json_extract(document,{fields}) AS fields"
    try:
        rows = conn.execute(f"{select} FROM {source}").fetchall()
    except sqlite3.DatabaseError:
        rows = conn.execute(f"{select} FROM {source} WHERE json_valid(document)").fetchall()
    for row in rows:
        composer_id = str(row["composer_id"])
        try:
            values = json.loads(row["fields"])
        except (TypeError, ValueError):
            continue
        if not composer_id or not isinstance(values, list) or len(values) != 10:
            continue
        name, created_at, updated_at, model_name, model_id, parameters, cwd, subagents, subcomposers, is_draft = values
        composers[composer_id] = _Composer(
            composer_id,
            title=_safe_scalar(name, 200),
            model=_safe_scalar(model_name) or _safe_scalar(model_id),
            reasoning_effort=_effort_from_parameters(parameters),
            cwd=_safe_scalar(cwd, 1000),
            created_at=_timestamp(created_at),
            updated_at=_timestamp(updated_at),
            is_draft=bool(is_draft),
            updated_mark=_safe_int(updated_at) if isinstance(updated_at, (int, float)) else None,
        )
        for child in _id_list(subagents) + _id_list(subcomposers):
            children.setdefault(child, composer_id)
    for child, parent in children.items():
        if child in composers and parent != child:
            composers[child].parent_id = parent
    present = {
        str(row[0]) for row in conn.execute(
            "SELECT substr(key,length('composerData:')+1) FROM cursorDiskKV "
            "WHERE key>'composerData:' AND key<'composerData;'"
        )
    }
    return composers, present - set(composers)


def _model_call_key(tool_call_id: Any, model_call_id: Any) -> str | None:
    """Return the model call shared by parallel tool calls, from either identifier."""

    for value in (tool_call_id, model_call_id):
        if not isinstance(value, str):
            continue
        first = value.split("\n", 1)[0].strip()
        if first:
            return re.sub(r"-\d+$", "", first)[:120]
    return None


class _BubbleReadError(sqlite3.DatabaseError):
    """A composer has bubble documents that cannot be decoded."""


def _read_bubble_calls(conn: sqlite3.Connection, composer_id: str) -> list[_BubbleCall]:
    """Group a composer's bubbles into model responses using only scalar fields.

    A response starts at a thinking bubble, at a text bubble that does not
    directly follow thinking, or at a tool bubble whose model call differs
    from the one already seen in the current response.  Cursor's tool call
    identifiers have the form ``call-<model call uuid>-<tool index>``, so
    parallel tool calls from one model call share the uuid.  Other tool bubbles
    extend the current response.  Only presence flags, tool names, call
    identifiers, timestamps, model names, and token counts are read; bubble
    text and thinking bodies stay inside SQLite.
    """

    prefix = f"bubbleId:{composer_id}:"
    invalid = conn.execute(
        "SELECT COUNT(*) FROM cursorDiskKV WHERE key>? AND key<? AND NOT json_valid(CAST(value AS TEXT))",
        (prefix, prefix[:-1] + ";"),
    ).fetchone()[0]
    if invalid:
        # A skipped bubble would look like a deleted response; refuse the
        # whole read so existing rows and marks are left alone.
        raise _BubbleReadError(f"{invalid} bubble document(s) could not be decoded")
    rows = conn.execute(
        """SELECT substr(key,?) AS bubble_id,
                  json_extract(document,'$.type') AS kind,
                  json_extract(document,'$.createdAt') AS created_at,
                  json_extract(document,'$.tokenCount.inputTokens') AS input_tokens,
                  json_extract(document,'$.tokenCount.outputTokens') AS output_tokens,
                  json_extract(document,'$.toolFormerData.name') AS tool_name,
                  json_extract(document,'$.toolFormerData.toolCallId') AS tool_call_id,
                  json_extract(document,'$.toolFormerData.modelCallId') AS model_call_id,
                  json_type(document,'$.thinking') IN ('object','text') AS has_thinking,
                  COALESCE(length(json_extract(document,'$.text')),0)>0 AS has_text,
                  json_extract(document,'$.modelInfo.modelName') AS model
           FROM (SELECT key,CAST(value AS TEXT) AS document FROM cursorDiskKV WHERE key>? AND key<?)
           WHERE json_valid(document)
           ORDER BY created_at,bubble_id""",
        (len(prefix) + 1, prefix, prefix[:-1] + ";"),
    )
    calls: list[_BubbleCall] = []
    current: _BubbleCall | None = None
    previous_kind: str | None = None
    model: str | None = None
    for row in rows:
        if row["kind"] == _USER_BUBBLE:
            model = _safe_scalar(row["model"]) or model
            current, previous_kind = None, None
            continue
        if row["kind"] != _ASSISTANT_BUBBLE:
            continue
        tool_name = row["tool_name"] if isinstance(row["tool_name"], str) else None
        if row["has_thinking"]:
            kind = "thinking"
        elif row["has_text"]:
            kind = "text"
        elif tool_name:
            kind = "tool"
        else:
            kind = None
        input_tokens, output_tokens = _safe_int(row["input_tokens"]), _safe_int(row["output_tokens"])
        if kind is None:
            if current is not None:
                current.input_tokens += input_tokens
                current.output_tokens += output_tokens
            continue
        call_id = _model_call_key(row["tool_call_id"], row["model_call_id"]) if kind == "tool" else None
        starts_new = (
            current is None
            or kind == "thinking"
            or (kind == "text" and previous_kind != "thinking")
            or (kind == "tool" and call_id is not None and current.model_call_id not in (None, call_id))
        )
        if starts_new:
            current = _BubbleCall(str(row["bubble_id"]), _timestamp(row["created_at"]), model)
            calls.append(current)
        assert current is not None
        current.input_tokens += input_tokens
        current.output_tokens += output_tokens
        if kind == "text":
            current.call_label = prefer_action_label(current.call_label, "Assistant response")
        elif kind == "tool":
            current.call_label = prefer_action_label(current.call_label, safe_action_label("tool", tool_name))
            current.model_call_id = current.model_call_id or call_id
        previous_kind = kind
    return calls


def _read_tracking_models(home: Path) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Return model and activity range per conversation from Cursor's edit tracking DB.

    Only the conversation identifier, model, and timestamps are selected; the
    table's file names, hashes, and stored file contents are never read.
    """

    path = home / "ai-tracking" / "ai-code-tracking.db"
    if not path.is_file():
        return {}
    result: dict[str, tuple[str | None, str | None, str | None]] = {}
    try:
        with _readonly(path) as conn:
            if "ai_code_hashes" not in _tables(conn):
                return {}
            columns = _columns(conn, "ai_code_hashes")
            if not {"conversationId", "model", "createdAt"} <= columns:
                return {}
            rows = conn.execute(
                """SELECT conversationId,MAX(model),MIN(createdAt),MAX(createdAt)
                   FROM ai_code_hashes WHERE conversationId IS NOT NULL AND conversationId!=''
                   GROUP BY conversationId"""
            ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    for row in rows:
        result[str(row[0])] = (_safe_scalar(row[1]), _timestamp(row[2]), _timestamp(row[3]))
    return result


# --------------------------------------------------------------------------
# Dashboard writes
# --------------------------------------------------------------------------


def _ensure_session(conn: sqlite3.Connection, root_id: str, source_home: Path) -> None:
    conn.execute(
        """INSERT INTO sessions(id,root_thread_id,source_app,source_home,accounting_status,accounting_note)
           VALUES(?,?,?,?,'partial',?)
           ON CONFLICT(id) DO UPDATE SET source_app=excluded.source_app,source_home=excluded.source_home""",
        (root_id, root_id, SOURCE_APP, str(source_home), _NO_ACCOUNTING_NOTE),
    )


def _update_session_metadata(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    title: str | None,
    cwd: str | None,
    repo_name: str | None,
    created_at: str | None,
    updated_at: str | None,
    model: str | None,
    reasoning_effort: str | None,
) -> None:
    conn.execute(
        """UPDATE sessions SET title=COALESCE(?,title,'Cursor session'),cwd=COALESCE(?,cwd),
           repo_root=COALESCE(?,repo_root),repo_name=COALESCE(?,repo_name),
           created_at=COALESCE(?,created_at),
           updated_at=CASE WHEN updated_at IS NULL OR ?>updated_at THEN ? ELSE updated_at END,
           root_model=COALESCE(?,root_model),root_reasoning_effort=COALESCE(?,root_reasoning_effort),
           root_provider=? WHERE id=?""",
        (title, cwd, cwd, repo_name, created_at, updated_at, updated_at, model, reasoning_effort, PROVIDER, root_id),
    )


def _upsert_agent(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    root_id: str,
    is_subagent: bool,
    nickname: str | None,
    created_at: str | None,
    updated_at: str | None,
    model: str | None,
    reasoning_effort: str | None,
    source_path: str,
    orphan: bool,
    parent_thread_id: str | None = None,
    agent_path: str | None = None,
) -> None:
    if is_subagent:
        parent_thread_id = parent_thread_id or root_id
        agent_path = agent_path or (f"/root/{nickname}" if nickname else "/root/subagent")
    else:
        parent_thread_id, agent_path = None, "/root"
    conn.execute(
        """INSERT INTO agents(thread_id,session_id,parent_thread_id,agent_role,agent_nickname,agent_path,
           created_at,updated_at,model,model_provider,reasoning_effort,source_rollout_path,source_kind,
           orphan,source_available)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(thread_id) DO UPDATE SET
             session_id=excluded.session_id,parent_thread_id=excluded.parent_thread_id,
             agent_role=excluded.agent_role,agent_nickname=excluded.agent_nickname,agent_path=excluded.agent_path,
             created_at=COALESCE(excluded.created_at,agents.created_at),
             updated_at=COALESCE(excluded.updated_at,agents.updated_at),
             model=COALESCE(excluded.model,agents.model),model_provider=excluded.model_provider,
             reasoning_effort=COALESCE(excluded.reasoning_effort,agents.reasoning_effort),
             source_rollout_path=excluded.source_rollout_path,source_kind=excluded.source_kind,
             orphan=excluded.orphan,source_available=1""",
        (
            thread_id, root_id, parent_thread_id, "subagent" if is_subagent else "root",
            nickname, agent_path, created_at, updated_at,
            model, PROVIDER, reasoning_effort, source_path, SOURCE_APP, int(orphan),
        ),
    )


def _upsert_usage(
    conn: sqlite3.Connection,
    summary: CursorIngestSummary,
    *,
    identity: str,
    root_id: str,
    thread_id: str,
    timestamp: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    source_path: str,
    ordinal: int,
    call_label: str,
) -> None:
    usage = TokenUsage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens
    )
    if input_tokens or output_tokens:
        cost = calculate_cost(conn, usage, model, PROVIDER, timestamp)
        price_id, cost_note = cost.price_id, cost.note
        amounts = tuple(
            str(value) if value is not None else None
            for value in (cost.uncached_input_usd, cost.cached_input_usd, cost.cache_write_usd, cost.output_usd,
                          cost.total_usd)
        )
        if cost.total_usd is None:
            summary.unknown_prices.add(f"{PROVIDER}:{model}")
    else:
        price_id, cost_note, amounts = None, _NO_ACCOUNTING_NOTE, (None,) * 5
    if model == "unknown-model":
        summary.unknown_models.add(model)
    exists = conn.execute("SELECT 1 FROM usage WHERE source_record_identity=?", (identity,)).fetchone()
    conn.execute(
        """INSERT INTO usage(source_record_identity,session_id,thread_id,turn_id,response_id,timestamp,model,
           provider,input_tokens,cached_input_tokens,cache_write_input_tokens,uncached_input_tokens,output_tokens,
           reasoning_output_tokens,total_tokens,source_file,source_ordinal,source_event_type,call_label,price_id,
           uncached_input_usd,cached_input_usd,cache_write_usd,output_usd,cost_usd,pricing_note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_record_identity) DO UPDATE SET
             session_id=excluded.session_id,thread_id=excluded.thread_id,timestamp=excluded.timestamp,
             model=excluded.model,provider=excluded.provider,input_tokens=excluded.input_tokens,
             uncached_input_tokens=excluded.uncached_input_tokens,output_tokens=excluded.output_tokens,
             total_tokens=excluded.total_tokens,source_file=excluded.source_file,
             source_ordinal=excluded.source_ordinal,call_label=excluded.call_label,price_id=excluded.price_id,
             uncached_input_usd=excluded.uncached_input_usd,cached_input_usd=excluded.cached_input_usd,
             cache_write_usd=excluded.cache_write_usd,output_usd=excluded.output_usd,cost_usd=excluded.cost_usd,
             pricing_note=excluded.pricing_note""",
        (
            identity, root_id, thread_id, identity, identity, timestamp, model, PROVIDER, input_tokens, 0, 0,
            input_tokens, output_tokens, 0, input_tokens + output_tokens, source_path, ordinal, _ASSISTANT_EVENT,
            call_label, price_id, *amounts, cost_note,
        ),
    )
    if exists:
        summary.duplicate_records += 1
    else:
        summary.usage_records += 1
        if amounts[4] is not None:
            summary.estimated_spend += float(amounts[4])


def _remove_stale_usage(conn: sqlite3.Connection, *, scope: str, params: tuple[Any, ...], keep: set[str]) -> None:
    stored = {
        row[0] for row in conn.execute(
            f"SELECT source_record_identity FROM usage WHERE source_event_type=? AND {scope}",
            (_ASSISTANT_EVENT, *params),
        )
    }
    conn.executemany(
        "DELETE FROM usage WHERE source_event_type=? AND source_record_identity=?",
        ((_ASSISTANT_EVENT, identity) for identity in stored - keep),
    )


def _stored_fingerprints(conn: sqlite3.Connection) -> dict[str, _Fingerprint]:
    return {
        row[0]: _Fingerprint(int(row[1] or 0), int(row[2]), int(row[3]))
        for row in conn.execute(
            "SELECT source_path,inode,size,mtime_ns FROM ingestion_state "
            "WHERE source_key LIKE 'cursor:%' AND source_key NOT LIKE ? AND parser_version=?",
            (_COMPOSER_MARK + "%", PARSER_VERSION),
        )
    }


def _stored_composer_marks(conn: sqlite3.Connection) -> dict[str, int]:
    """Return the editor lastUpdatedAt recorded after each composer's last full read."""

    return {
        row[0][len(_COMPOSER_MARK):]: int(row[1])
        for row in conn.execute(
            "SELECT source_key,mtime_ns FROM ingestion_state WHERE source_key LIKE ? AND parser_version=?",
            (_COMPOSER_MARK + "%", PARSER_VERSION),
        )
    }


def _store_composer_marks(
    conn: sqlite3.Connection, marks: dict[str, int], state_path: Path, scanned_at: str
) -> None:
    conn.executemany(
        """INSERT INTO ingestion_state(source_key,source_path,inode,last_offset,mtime_ns,size,parser_version,
             last_successful_ingestion) VALUES(?,?,NULL,0,?,0,?,?)
           ON CONFLICT(source_key) DO UPDATE SET
             source_path=excluded.source_path,mtime_ns=excluded.mtime_ns,parser_version=excluded.parser_version,
             last_successful_ingestion=excluded.last_successful_ingestion""",
        ((_COMPOSER_MARK + composer_id, str(state_path), mark, PARSER_VERSION, scanned_at)
         for composer_id, mark in marks.items()),
    )


def _prune_composer_marks(conn: sqlite3.Connection, current_ids: set[str]) -> None:
    stored = {
        row[0][len(_COMPOSER_MARK):]
        for row in conn.execute(
            "SELECT source_key FROM ingestion_state WHERE source_key LIKE ?", (_COMPOSER_MARK + "%",)
        )
    }
    conn.executemany(
        "DELETE FROM ingestion_state WHERE source_key=?",
        ((_COMPOSER_MARK + composer_id,) for composer_id in stored - current_ids),
    )


def _store_fingerprints(conn: sqlite3.Connection, fingerprints: dict[str, _Fingerprint], scanned_at: str) -> None:
    conn.executemany(
        """INSERT INTO ingestion_state(source_key,source_path,inode,last_offset,mtime_ns,size,parser_version,
             last_successful_ingestion) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(source_key) DO UPDATE SET
             source_path=excluded.source_path,inode=excluded.inode,last_offset=excluded.last_offset,
             mtime_ns=excluded.mtime_ns,size=excluded.size,parser_version=excluded.parser_version,
             last_successful_ingestion=excluded.last_successful_ingestion""",
        (
            (_ns(path), path, item.inode, item.size, item.mtime_ns, item.size, PARSER_VERSION, scanned_at)
            for path, item in fingerprints.items()
        ),
    )


def _prune_fingerprints(conn: sqlite3.Connection, home: Path, current_paths: set[str]) -> None:
    stored = {
        row[0] for row in conn.execute(
            "SELECT source_path FROM ingestion_state WHERE source_key LIKE 'cursor:%' AND source_key NOT LIKE ?",
            (_COMPOSER_MARK + "%",),
        )
    }
    prefixes = (str(home / "projects") + os.sep, str(home / "chats") + os.sep)
    conn.executemany(
        "DELETE FROM ingestion_state WHERE source_key=?",
        ((_ns(path),) for path in stored - current_paths if path.startswith(prefixes)),
    )


def _session_liveness(updated_at: str | None, window: int, now: datetime) -> tuple[str, str | None]:
    if updated_at and window > 0:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated = None
        if updated is not None and updated >= now - timedelta(seconds=window):
            return "running", None
    return "completed", updated_at


def _refresh_session_state(conn: sqlite3.Connection, root_ids: set[str], window: int, now: datetime) -> None:
    for root_id in root_ids:
        row = conn.execute(
            """SELECT s.updated_at,
                      (SELECT COUNT(*) FROM usage WHERE session_id=s.id AND source_event_type=?) AS turns,
                      (SELECT COALESCE(SUM(total_tokens=0),0) FROM usage WHERE session_id=s.id) AS unaccounted
               FROM sessions s WHERE s.id=?""",
            (_ASSISTANT_EVENT, root_id),
        ).fetchone()
        if row is None:
            continue
        status, finished_at = _session_liveness(row[0], window, now)
        # Every response needs a token count before the session can be
        # priced; one uncounted response leaves the total incomplete.
        accounted = int(row[1] or 0) > 0 and int(row[2] or 0) == 0
        conn.execute(
            """UPDATE sessions SET status=?,finished_at=?,turn_count=?,accounting_status=?,accounting_note=?
               WHERE id=?""",
            (
                status, finished_at, int(row[1] or 0), "complete" if accounted else "partial",
                None if accounted else _NO_ACCOUNTING_NOTE, root_id,
            ),
        )


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Unit:
    """A root transcript with its subagent transcripts."""

    root_external_id: str
    items: list[_Transcript]
    store: Path | None = None
    # Every file whose change must trigger a reread of this unit, fixed at
    # the start of the pass.
    paths: list[Path] = field(default_factory=list)

    @property
    def root(self) -> _Transcript | None:
        return next((item for item in self.items if not item.is_subagent), None)


def _unit_fingerprints(unit: _Unit) -> dict[str, _Fingerprint | None]:
    fingerprints: dict[str, _Fingerprint | None] = {}
    for path in unit.paths:
        fingerprint = _fingerprint(path)
        if path.name.endswith("-wal") and (fingerprint is None or fingerprint.size == 0):
            # Opening a WAL-mode store read-only creates an empty WAL file;
            # only a WAL that holds committed pages is a change worth a reread.
            fingerprint = _Fingerprint(0, 0, 0)
        fingerprints[str(path)] = fingerprint
    return fingerprints


def _ancestry(composer_id: str, composers: dict[str, _Composer]) -> list[str]:
    """Return the editor parent chain from the root down to ``composer_id``."""

    chain = [composer_id]
    seen = {composer_id}
    while True:
        parent = composers[chain[-1]].parent_id
        if not parent or parent in seen or parent not in composers:
            return list(reversed(chain))
        seen.add(parent)
        chain.append(parent)


def ingest_cursor(settings: Settings, force_all: bool = False, *, now: datetime | None = None) -> CursorIngestSummary:
    """Ingest Cursor agent history without persisting conversation content.

    Transcripts under ``projects`` define sessions, subagents, and one usage
    row per assistant message.  Editor composer state supplies titles, working
    directories, models, timestamps, and, when the number of reconstructed
    bubble responses matches the transcript, per-call timestamps and token
    counts.  CLI stores supply titles, models, and creation times.  Editor
    composers that have no transcript (older Cursor releases) are imported
    from their bubbles alone.  Unchanged transcripts are skipped by
    fingerprint; ``force_all`` rereads everything.
    """

    settings.validate()
    home = discover_cursor_home(settings)
    user_dir = discover_cursor_user_dir(settings)
    summary = CursorIngestSummary(source_home=home, source_user_dir=user_dir)
    projects = home / "projects"
    paths, transcripts_complete = _transcript_paths(home, summary)
    transcripts = [item for item in (_transcript_for(path, projects) for path in paths) if item is not None]
    summary.scanned_files = len(transcripts)
    units: dict[str, _Unit] = {}
    for transcript in transcripts:
        transcript.updated_at = _mtime(transcript.path)
        units.setdefault(transcript.root_external_id, _Unit(transcript.root_external_id, [])).items.append(transcript)
    transcript_ids = {item.external_id for item in transcripts}
    # A CLI store carries the session title, model, and creation time, so a
    # change to it must reread the unit exactly like a transcript change.
    fingerprints: dict[str, _Fingerprint | None] = {}
    for root, unit in units.items():
        unit.store = _cli_store_for(home, root)
        unit.paths = [item.path for item in unit.items]
        if unit.store is not None:
            # A live WAL-mode connection commits into store.db-wal without
            # touching store.db until a checkpoint.
            unit.paths += [unit.store, unit.store.with_name(unit.store.name + "-wal")]
        fingerprints.update(_unit_fingerprints(unit))
    state_path = editor_state_path(user_dir)
    scan_now = (now or datetime.now(UTC)).astimezone(UTC)
    window = int(getattr(settings, "running_window_seconds", 0))

    initialize(settings.database)
    with database(settings.database) as conn, ExitStack() as editor_stack:
        seed_prices(conn)
        # Editor state is consulted every pass: it is one cheap query and it
        # is the only place titles, working directories, and models live.
        composers: dict[str, _Composer] = {}
        unreadable_composers: set[str] = set()
        editor_available = False
        editor_conn: sqlite3.Connection | None = None
        if state_path.is_file():
            try:
                editor_conn = editor_stack.enter_context(_readonly(state_path))
                composers, unreadable_composers = _read_editor_composers(editor_conn)
                editor_available = True
            except sqlite3.DatabaseError:
                summary.parser_warnings += 1
                _warn(conn, str(state_path), "cursor_editor_state_unreadable", "editor state could not be read")
                editor_conn = None
        if unreadable_composers:
            summary.parser_warnings += 1
            _warn(
                conn, str(state_path), "cursor_composer_unreadable",
                "an editor composer document could not be decoded; editor sessions are not reconciled",
            )
        # The editor gave nothing this pass although it exists, or existed
        # before (composer marks were recorded): rows written now would lack
        # its timestamps and tokens, so nothing read now may be fingerprinted.
        # A forced reimport with no editor database drops the marks, so a
        # machine whose editor was removed stops rereading every pass.
        if force_all and not state_path.is_file():
            conn.execute("DELETE FROM ingestion_state WHERE source_key LIKE ?", (_COMPOSER_MARK + "%",))
        editor_known = state_path.is_file() or bool(_stored_composer_marks(conn))
        editor_outage = editor_known and not editor_available
        # Removal of editor-only sessions is safe only when every composer
        # document was decoded; an undecodable one may still be a session.
        editor_complete = editor_available and not unreadable_composers
        summary.editor_composers = sum(1 for item in composers.values() if not item.is_draft)

        stored = {} if force_all else _stored_fingerprints(conn)
        stored_marks = {} if force_all else _stored_composer_marks(conn)
        stored_members: dict[str, set[str]] = {}
        chats_prefix = str(home / "chats") + os.sep
        for path in stored:
            transcript = _transcript_for(Path(path), projects)
            if transcript is not None:
                stored_members.setdefault(transcript.root_external_id, set()).add(path)
            elif path.startswith(chats_prefix) and Path(path).name in {"store.db", "store.db-wal"}:
                stored_members.setdefault(Path(path).parent.name, set()).add(path)

        def composer_unchanged(composer_id: str) -> bool:
            composer = composers.get(composer_id)
            if composer is None:
                return True
            return composer.updated_mark is not None and stored_marks.get(composer_id) == composer.updated_mark

        # A unit is unchanged only when every transcript file is unchanged and
        # every editor composer behind it still carries the lastUpdatedAt that
        # was recorded after its bubbles were last read.
        skipped_roots = {
            root for root, unit in units.items()
            if stored_members.get(root) == {str(path) for path in unit.paths}
            and all(
                fingerprints[str(path)] is not None and stored[str(path)] == fingerprints[str(path)]
                for path in unit.paths
            )
            and all(composer_unchanged(item.external_id) for item in unit.items)
        }
        summary.unchanged_files = sum(len(units[root].items) for root in skipped_roots)
        changed_roots = [root for root in units if root not in skipped_roots]
        tracking = _read_tracking_models(home) if changed_roots or force_all else {}
        bubble_failures: set[str] = set()
        marked_composers: set[str] = set()

        def bubble_calls(composer_id: str) -> list[_BubbleCall]:
            if editor_conn is None or composer_id not in composers:
                return []
            try:
                return _read_bubble_calls(editor_conn, composer_id)
            except sqlite3.DatabaseError as exc:
                summary.parser_warnings += 1
                bubble_failures.add(composer_id)
                _warn(conn, composer_id, "cursor_bubble_unreadable", str(exc))
                return []

        slug_cache: dict[str, Path | None] = {}

        def slug_cwd(slug: str) -> str | None:
            if slug not in slug_cache:
                slug_cache[slug] = resolve_slug(slug)
            resolved = slug_cache[slug]
            return str(resolved) if resolved is not None else None

        complete_units: set[str] = set()
        current_thread_ids = {item.thread_id for item in transcripts}
        for root in changed_roots:
            unit = units[root]
            root_id = _ns(root)
            root_item = unit.root
            editor_root = composers.get(root)
            cli_root, store_unreadable = None, False
            if unit.store is not None:
                try:
                    cli_root = _read_cli_store(unit.store, root)
                except sqlite3.DatabaseError as exc:
                    store_unreadable = True
                    summary.parser_warnings += 1
                    _warn(conn, str(unit.store), "cursor_cli_store_unreadable", str(exc))
            if cli_root is not None:
                summary.cli_chats += 1
            tracked_model, tracked_start, tracked_end = tracking.get(root, (None, None, None))
            slug = unit.items[0].slug
            cwd = (editor_root.cwd if editor_root else None) or slug_cwd(slug)
            created_at = _earliest(
                editor_root.created_at if editor_root else None,
                cli_root.created_at if cli_root else None,
                tracked_start,
            ) or _earliest(*(item.updated_at for item in unit.items))
            updated_at = _latest(
                editor_root.updated_at if editor_root else None, tracked_end,
                *(item.updated_at for item in unit.items),
            )
            root_model = (
                (editor_root.model if editor_root else None) or (cli_root.model if cli_root else None) or tracked_model
            )
            unit_complete = transcripts_complete and not editor_outage and not store_unreadable
            readings = [(transcript, *_read_transcript(transcript, summary)) for transcript in unit.items]
            if not any(calls for _transcript, calls, _complete, _readable in readings):
                # A transcript with no assistant message is a prompt that has
                # not been answered yet or a turn that failed before any
                # response.  It is not a session until a response exists.
                if unit_complete and all(complete for _transcript, _calls, complete, _readable in readings):
                    complete_units.add(root)
                continue
            _ensure_session(conn, root_id, home)
            _update_session_metadata(
                conn, root_id,
                title=(editor_root.title if editor_root else None) or (cli_root.title if cli_root else None),
                cwd=cwd, repo_name=_project_name(cwd, slug), created_at=created_at, updated_at=updated_at,
                model=root_model, reasoning_effort=editor_root.reasoning_effort if editor_root else None,
            )
            for transcript, calls, complete, readable in readings:
                unit_complete = unit_complete and complete
                if not readable:
                    continue
                if not complete and conn.execute(
                    "SELECT 1 FROM usage WHERE thread_id=? AND source_file!=? LIMIT 1",
                    (transcript.thread_id, str(transcript.path)),
                ).fetchone():
                    # The thread still has rows imported from editor bubbles.
                    # Importing a partially readable transcript beside them
                    # would count the same responses twice, and evicting them
                    # would count fewer; keep the editor rows until the
                    # transcript can be read completely.
                    continue
                composer = composers.get(transcript.external_id)
                agent_model = root_model if not transcript.is_subagent else (composer.model if composer else None)
                # The subagents directory is flat; the editor's parent links
                # restore nesting when they resolve to this same root.
                parent_thread_id, agent_path = None, None
                if transcript.is_subagent and composer is not None:
                    ancestry = _ancestry(transcript.external_id, composers)
                    if ancestry[0] == root and len(ancestry) > 2:
                        parent_thread_id = _thread_id(root, ancestry[-2])
                        agent_path = "/root/" + "/".join(ancestry[1:])
                _upsert_agent(
                    conn, thread_id=transcript.thread_id, root_id=root_id, is_subagent=transcript.is_subagent,
                    nickname=transcript.agent_external_id,
                    created_at=(composer.created_at if composer else None)
                    or (created_at if not transcript.is_subagent else transcript.updated_at),
                    updated_at=_latest(composer.updated_at if composer else None, transcript.updated_at),
                    model=agent_model, reasoning_effort=composer.reasoning_effort if composer else None,
                    source_path=str(transcript.path), orphan=transcript.is_subagent and root_item is None,
                    parent_thread_id=parent_thread_id, agent_path=agent_path,
                )
                aligned = bubble_calls(transcript.external_id) if composer is not None else []
                if aligned and len(aligned) != len(calls):
                    summary.parser_warnings += 1
                    _warn(
                        conn, transcript.thread_id, "cursor_bubble_alignment",
                        "editor responses could not be aligned with transcript responses; "
                        "per-call timestamps and token counts are unavailable",
                    )
                    aligned = []
                if composer is not None and transcript.external_id not in bubble_failures:
                    marked_composers.add(transcript.external_id)
                fallback_timestamp = created_at or transcript.updated_at or scan_now.isoformat()
                identities: set[str] = set()
                for index, call in enumerate(calls):
                    bubble = aligned[index] if aligned else None
                    identity = _ns(
                        f"{transcript.root_external_id}:{transcript.agent_external_id or 'root'}:{call.ordinal}"
                    )
                    identities.add(identity)
                    _upsert_usage(
                        conn, summary, identity=identity, root_id=root_id, thread_id=transcript.thread_id,
                        timestamp=(bubble.timestamp if bubble else None) or fallback_timestamp,
                        model=(bubble.model if bubble else None) or agent_model or "unknown-model",
                        input_tokens=bubble.input_tokens if bubble else 0,
                        output_tokens=bubble.output_tokens if bubble else 0,
                        source_path=str(transcript.path), ordinal=call.ordinal, call_label=call.call_label,
                    )
                if complete:
                    _remove_stale_usage(conn, scope="source_file=?", params=(str(transcript.path),), keep=identities)
                    # Rows imported earlier from editor bubbles for this thread
                    # would otherwise be counted alongside the transcript's
                    # rows.  A partially written transcript keeps them until
                    # it can be read completely.
                    _remove_stale_usage(
                        conn, scope="thread_id=? AND source_file!=?",
                        params=(transcript.thread_id, str(transcript.path)), keep=identities,
                    )
            if unit_complete:
                complete_units.add(root)
        # Skipped units keep their rows, but editor metadata such as a title
        # generated later or a rename is still applied.
        for root in skipped_roots:
            editor_root = composers.get(root)
            if editor_root is None:
                continue
            _update_session_metadata(
                conn, _ns(root), title=editor_root.title, cwd=editor_root.cwd,
                repo_name=_project_name(editor_root.cwd, units[root].items[0].slug), created_at=editor_root.created_at,
                updated_at=editor_root.updated_at, model=editor_root.model,
                reasoning_effort=editor_root.reasoning_effort,
            )

        # Editor composers without a transcript: older Cursor releases kept the
        # whole conversation in the editor state.  Their bubbles are the only
        # per-response record, so they become usage rows directly.
        editor_only_threads: set[str] = set()
        for composer_id, composer in composers.items():
            if composer.is_draft or composer_id in transcript_ids:
                continue
            ancestry = _ancestry(composer_id, composers)
            root, lineage = ancestry[0], ancestry[1:]
            is_subagent = bool(lineage)
            root_id = _ns(root)
            thread_id = _thread_id(root, composer_id if is_subagent else None)
            parent_thread_id = _thread_id(root, lineage[-2]) if len(lineage) > 1 else root_id
            editor_only_threads.add(thread_id)
            stored_agent = conn.execute(
                "SELECT source_rollout_path FROM agents WHERE thread_id=?", (thread_id,)
            ).fetchone()
            # Only an agent already imported from the editor can be skipped; one
            # whose transcript just disappeared must be rebuilt from bubbles.
            already_editor_sourced = stored_agent is not None and stored_agent[0] == str(state_path)
            transcript_sourced = stored_agent is not None and str(stored_agent[0] or "").startswith(
                str(projects) + os.sep
            )
            if transcript_sourced and not transcripts_complete:
                # The transcript may only be hidden by an incomplete scan of
                # projects; its absence is not evidence that it was deleted.
                continue
            if not force_all and already_editor_sourced and composer_unchanged(composer_id):
                marked_composers.add(composer_id)
                continue
            if unreadable_composers and stored_agent is None:
                # The undecodable document may be this composer's parent, in
                # which case importing it as a root would duplicate a thread
                # that already exists under that parent.
                continue
            calls = bubble_calls(composer_id)
            if composer_id in bubble_failures or (not calls and stored_agent is None):
                continue
            marked_composers.add(composer_id)
            _ensure_session(conn, root_id, home)
            if not is_subagent:
                _update_session_metadata(
                    conn, root_id, title=composer.title, cwd=composer.cwd,
                    repo_name=_project_name(composer.cwd, "empty-window"), created_at=composer.created_at,
                    updated_at=composer.updated_at, model=composer.model, reasoning_effort=composer.reasoning_effort,
                )
            _upsert_agent(
                conn, thread_id=thread_id, root_id=root_id, is_subagent=is_subagent,
                nickname=composer_id if is_subagent else None, created_at=composer.created_at,
                updated_at=composer.updated_at, model=composer.model, reasoning_effort=composer.reasoning_effort,
                source_path=str(state_path), orphan=False, parent_thread_id=parent_thread_id,
                agent_path="/root/" + "/".join(lineage) if lineage else None,
            )
            identities = set()
            for index, call in enumerate(calls):
                identity = _ns(f"{root}:{composer_id if is_subagent else 'root'}:bubble:{call.bubble_id}")
                identities.add(identity)
                _upsert_usage(
                    conn, summary, identity=identity, root_id=root_id, thread_id=thread_id,
                    timestamp=call.timestamp or composer.created_at or scan_now.isoformat(),
                    model=call.model or composer.model or "unknown-model", input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens, source_path=str(state_path), ordinal=index,
                    call_label=call.call_label or "Assistant response",
                )
            _remove_stale_usage(conn, scope="thread_id=?", params=(thread_id,), keep=identities)

        # Reconcile removed history only within scope that was fully observed.
        stored_agents = conn.execute(
            "SELECT thread_id,source_rollout_path FROM agents WHERE source_kind=? AND thread_id LIKE 'cursor:%'",
            (SOURCE_APP,),
        ).fetchall()
        projects_prefix = str(projects) + os.sep
        for thread_id, source_path in stored_agents:
            from_transcript = str(source_path or "").startswith(projects_prefix)
            from_editor = str(source_path or "") == str(state_path)
            remove = (from_transcript and transcripts_complete and thread_id not in current_thread_ids) or (
                from_editor and editor_complete and thread_id not in editor_only_threads
                and thread_id not in current_thread_ids
            )
            if remove:
                conn.execute("DELETE FROM agents WHERE thread_id=?", (thread_id,))
        conn.execute(
            "DELETE FROM sessions WHERE source_app=? AND id LIKE 'cursor:%' "
            "AND NOT EXISTS(SELECT 1 FROM agents WHERE agents.session_id=sessions.id)",
            (SOURCE_APP,),
        )
        if transcripts_complete:
            _prune_fingerprints(conn, home, set(fingerprints))
        if editor_complete:
            _prune_composer_marks(conn, set(composers))
        if editor_available:
            _store_composer_marks(
                conn,
                {
                    composer_id: composers[composer_id].updated_mark
                    for composer_id in marked_composers
                    if composers[composer_id].updated_mark is not None
                },
                state_path, scan_now.isoformat(),
            )
        _store_fingerprints(
            conn,
            {
                str(path): fingerprints[str(path)]
                for root in complete_units for path in units[root].paths
                if fingerprints[str(path)] is not None
            },
            scan_now.isoformat(),
        )
        live_roots = {
            row[0] for row in conn.execute(
                "SELECT id FROM sessions WHERE source_app=? AND id LIKE 'cursor:%'", (SOURCE_APP,)
            )
        }
        _refresh_session_state(conn, live_roots, window, scan_now)
        summary.scanned_sessions = len(units) + sum(
            1 for composer_id in composers if composer_id not in transcript_ids and not composers[composer_id].is_draft
        )
        summary.root_sessions = len(live_roots)
        summary.subagent_sessions = int(conn.execute(
            "SELECT COUNT(*) FROM agents WHERE source_kind=? AND parent_thread_id IS NOT NULL", (SOURCE_APP,)
        ).fetchone()[0])
    return summary
