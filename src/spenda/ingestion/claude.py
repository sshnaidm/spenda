"""Privacy-preserving, read-only ingestion of Claude Code transcripts.

Only transcript envelope metadata, assistant accounting fields, content-block
types, and tool names are retained. Prompt text, assistant content bodies, tool
arguments/results, attachments, credentials, and every other message payload
are deliberately ignored and never persisted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import database, initialize
from ..models import CostResult, TokenUsage
from ..pricing import estimate_cost, price_model, seed_prices, subscription_split
from .action_labels import prefer_action_label, safe_action_label
from .claude_auth import (
    BACKEND_ANTHROPIC,
    BACKEND_ANTHROPIC_OAUTH,
    BACKEND_MIXED,
    ClaudeAuthProfile,
    message_backend,
    read_auth_profile,
)

SOURCE_APP = "claude"
PROVIDER = "anthropic"
_PREFIX = "claude:"
_ASSISTANT_EVENT = "claude_assistant_message"
_COST_EVENT = "claude_cost_state"
_META_AUTH_BACKEND = "claude_auth_backend"
_COVERED_NOTE = "cost represented by cumulative Claude Code cost-state"
# Bump when the values derived from a transcript change, so recorded
# fingerprints from an older parser stop suppressing a reread.
PARSER_VERSION = 2


@dataclass(slots=True)
class ClaudeIngestSummary:
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
    estimated_records: int = 0
    subscription_value: float = 0.0
    backends: dict[str, int] = field(default_factory=dict)
    auth_profile: ClaudeAuthProfile | None = None


@dataclass(slots=True)
class _Transcript:
    path: Path
    root_external_id: str
    agent_external_id: str | None
    cwd: str | None = None
    version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    root_model: str | None = None
    git_branch: str | None = None
    reasoning_effort: str | None = None
    title: str | None = None
    backends: set[str] = field(default_factory=set)

    @property
    def is_subagent(self) -> bool:
        return self.agent_external_id is not None

    @property
    def thread_id(self) -> str:
        if self.agent_external_id:
            return _ns(f"{self.root_external_id}:agent:{self.agent_external_id}")
        return _ns(self.root_external_id)


@dataclass(slots=True)
class _AssistantRecord:
    identity: str
    session_id: str
    thread_id: str
    timestamp: str
    model: str
    usage: dict[str, Any]
    source_path: Path
    ordinal: int
    call_label: str
    backend: str
    cache_write_1h: int = 0
    fast: bool = False


@dataclass(slots=True)
class _CostState:
    model_costs: dict[str, Decimal]
    total_cost: Decimal
    complete: bool
    timestamp: str | None
    ordinal: int
    source_path: Path


@dataclass(frozen=True, slots=True)
class _Fingerprint:
    """Cheap identity of a transcript's on-disk state; never its content."""

    inode: int
    size: int
    mtime_ns: int


def _fingerprint(path: Path) -> _Fingerprint | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _Fingerprint(stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _stored_fingerprints(conn) -> dict[str, _Fingerprint]:
    """Return fingerprints recorded after each transcript's last complete import."""

    return {
        row[0]: _Fingerprint(int(row[1] or 0), int(row[2]), int(row[3]))
        for row in conn.execute(
            "SELECT source_path,inode,size,mtime_ns FROM ingestion_state "
            "WHERE source_key LIKE 'claude:%' AND parser_version=?",
            (PARSER_VERSION,),
        )
    }


def _store_fingerprints(conn, fingerprints: dict[str, _Fingerprint], scanned_at: str) -> None:
    conn.executemany(
        """INSERT INTO ingestion_state(source_key,source_path,inode,last_offset,mtime_ns,size,
             parser_version,last_successful_ingestion)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(source_key) DO UPDATE SET
             source_path=excluded.source_path,inode=excluded.inode,last_offset=excluded.last_offset,
             mtime_ns=excluded.mtime_ns,size=excluded.size,parser_version=excluded.parser_version,
             last_successful_ingestion=excluded.last_successful_ingestion""",
        (
            (_ns(path), path, item.inode, item.size, item.mtime_ns, item.size, PARSER_VERSION, scanned_at)
            for path, item in fingerprints.items()
        ),
    )


def _group_of(path: str, projects: Path) -> str | None:
    """Return the root session a recorded transcript path belongs to, if any."""

    try:
        transcript = _transcript_for(Path(path), projects)
    except ValueError:
        # Recorded under a different Claude home; it can never match here.
        return None
    return transcript.root_external_id if transcript is not None else None


def _prune_fingerprints(conn, current_paths: set[str]) -> None:
    stored = {
        row[0] for row in conn.execute(
            "SELECT source_path FROM ingestion_state WHERE source_key LIKE 'claude:%'"
        )
    }
    conn.executemany(
        "DELETE FROM ingestion_state WHERE source_key=?",
        ((_ns(path),) for path in stored - current_paths),
    )


def discover_claude_home(settings: Settings) -> Path:
    """Return Claude Code's home directory from the shared settings object."""

    configured = getattr(settings, "claude_home", None)
    return Path(configured or Path.home() / ".claude").expanduser().resolve()


def claude_auth_profile(settings: Settings) -> ClaudeAuthProfile:
    """Read the non-secret login profile of the configured Claude home."""

    return read_auth_profile(
        discover_claude_home(settings), override=getattr(settings, "claude_billing", "auto")
    )


def _unit_backend(backends: set[str]) -> str | None:
    """Collapse the backends seen in a transcript or unit into one label."""

    if not backends:
        return None
    return next(iter(backends)) if len(backends) == 1 else BACKEND_MIXED


def _read_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM dashboard_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _write_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO dashboard_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _ns(value: str) -> str:
    return value if value.startswith(_PREFIX) else f"{_PREFIX}{value}"


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_cost(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return max(Decimal("0"), parsed)
    except (InvalidOperation, ValueError):
        return None


def _timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        except ValueError:
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


def _latest(left: str | None, right: str | None) -> str | None:
    return max((item for item in (left, right) if item), default=None)


def _project_name(cwd: str | None) -> str | None:
    return Path(cwd).name if cwd else None


def _safe_title(value: Any) -> str | None:
    """Accept only the generated scalar title, never transcript content."""

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:200] or None


def _safe_scalar(value: Any, limit: int = 100) -> str | None:
    """Keep bounded scalar metadata, never an arbitrary transcript payload."""

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] or None


def _transcript_paths(
    home: Path, summary: ClaudeIngestSummary
) -> tuple[list[Path], bool] | None:
    projects = home / "projects"
    if not projects.is_dir():
        # An absent/unavailable projects directory is not evidence that every
        # previously seen Claude transcript was deleted. Keep the normalized
        # ledger intact until a readable directory scan can establish that.
        summary.parser_warnings += 1
        return None
    found: list[Path] = []
    complete = True

    def unreadable(_error: OSError) -> None:
        nonlocal complete
        complete = False
        summary.parser_warnings += 1

    try:
        for directory, _subdirectories, filenames in os.walk(
            projects, onerror=unreadable, followlinks=False
        ):
            for filename in filenames:
                if not filename.endswith(".jsonl"):
                    continue
                path = Path(directory) / filename
                relative = path.relative_to(projects)
                # Root session: <project>/<session>.jsonl. Subagent transcript:
                # <project>/<root-session>/subagents/agent-*.jsonl.
                if len(relative.parts) == 2 or (
                    len(relative.parts) == 4 and relative.parts[-2] == "subagents"
                ):
                    found.append(path)
    except OSError:
        summary.parser_warnings += 1
        return None
    return sorted(found), complete


def _transcript_for(path: Path, projects: Path) -> _Transcript | None:
    relative = path.relative_to(projects)
    if len(relative.parts) == 2:
        return _Transcript(path, path.stem, None)
    if len(relative.parts) == 4 and relative.parts[-2] == "subagents":
        return _Transcript(path, relative.parts[-3], path.stem)
    return None


def _update_metadata(transcript: _Transcript, record: dict[str, Any]) -> None:
    timestamp = _timestamp(record.get("timestamp"))
    transcript.created_at = transcript.created_at or timestamp
    transcript.updated_at = _latest(transcript.updated_at, timestamp)
    cwd = record.get("cwd")
    if isinstance(cwd, str) and cwd:
        transcript.cwd = transcript.cwd or cwd
    version = record.get("version")
    if isinstance(version, str) and version:
        transcript.version = transcript.version or version
    git_branch = _safe_scalar(record.get("gitBranch"))
    if git_branch:
        transcript.git_branch = transcript.git_branch or git_branch
    effort = _safe_scalar(record.get("effort"))
    if effort:
        transcript.reasoning_effort = transcript.reasoning_effort or effort


def _assistant_action_label(message: dict[str, Any]) -> str:
    """Inspect only block types and tool names; never retain block content."""

    content = message.get("content")
    if isinstance(content, str):
        return "Assistant response"
    blocks = content if isinstance(content, list) else ()
    label = None
    for block in blocks:
        if isinstance(block, dict):
            label = prefer_action_label(
                label, safe_action_label(block.get("type"), block.get("name"))
            )
    return label or "Assistant response"


def _read_transcript(
    transcript: _Transcript,
    summary: ClaudeIngestSummary,
) -> tuple[dict[str, _AssistantRecord], list[_CostState], bool, bool]:
    """Return valid rows, all cost snapshots, completeness, and readability."""

    assistant: dict[str, _AssistantRecord] = {}
    cost_states: list[_CostState] = []
    try:
        lines = transcript.path.open("rb")
    except OSError:
        summary.parser_warnings += 1
        return assistant, cost_states, False, False
    complete = True
    with lines:
        for ordinal, line in enumerate(lines):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                summary.malformed_lines += 1
                complete = False
                continue
            if not isinstance(record, dict):
                continue
            _update_metadata(transcript, record)
            kind = record.get("type")
            if kind == "ai-title" and not transcript.is_subagent:
                transcript.title = _safe_title(record.get("aiTitle")) or transcript.title
            if kind == "assistant":
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                message_id = message.get("id")
                model = message.get("model")
                if not isinstance(usage, dict) or not isinstance(message_id, str) or not message_id:
                    continue
                if not isinstance(model, str) or not model or model == "<synthetic>":
                    continue
                timestamp = _timestamp(record.get("timestamp"))
                if timestamp is None:
                    summary.parser_warnings += 1
                    complete = False
                    continue
                transcript.root_model = transcript.root_model or model
                identity = _ns(f"{transcript.root_external_id}:{transcript.agent_external_id or 'root'}:{message_id}")
                call_label = _assistant_action_label(message)
                previous = assistant.get(identity)
                if previous is not None:
                    call_label = prefer_action_label(previous.call_label, call_label) or call_label
                # The API backend is visible only in the identifier prefixes.
                backend = message_backend(message_id, record.get("requestId"))
                transcript.backends.add(backend)
                cache_creation = usage.get("cache_creation")
                cache_write_1h = (
                    _safe_int(cache_creation.get("ephemeral_1h_input_tokens"))
                    if isinstance(cache_creation, dict) else 0
                )
                # Same message appears several times while streaming.  Keeping
                # the latest transcript line avoids the observed overcounting.
                assistant[identity] = _AssistantRecord(
                    identity, _ns(transcript.root_external_id), transcript.thread_id,
                    timestamp, model, usage, transcript.path, ordinal, call_label,
                    backend, cache_write_1h, usage.get("speed") == "fast",
                )
            elif kind == "cost-state" and not transcript.is_subagent:
                model_usage = record.get("modelUsage")
                total = _safe_cost(record.get("totalCostUSD"))
                if not isinstance(model_usage, dict) or total is None:
                    summary.parser_warnings += 1
                    complete = False
                    continue
                model_costs: dict[str, Decimal] = {}
                for model, detail in model_usage.items():
                    if not isinstance(model, str) or not isinstance(detail, dict):
                        continue
                    cost = _safe_cost(detail.get("costUSD"))
                    if cost is not None:
                        model_costs[model] = cost
                # Each line is a complete cumulative snapshot. Keep snapshots
                # separate so ingestion can derive dated changes later.
                cost_states.append(_CostState(
                    model_costs=model_costs,
                    total_cost=total,
                    complete=record.get("hasUnknownModelCost") is False,
                    # The event timestamp identifies when this cumulative
                    # snapshot was persisted. ``startTime`` is the beginning
                    # of its aggregation window and would put all later cost
                    # on the first day of a long-running session.
                    timestamp=_timestamp(record.get("timestamp")) or transcript.updated_at,
                    ordinal=ordinal,
                    source_path=transcript.path,
                ))
    return assistant, cost_states, complete, True


def _ensure_session(conn, *, root_id: str, source_home: Path, version: str | None) -> None:
    conn.execute(
        """INSERT INTO sessions(
             id,root_thread_id,source_app,source_home,source_version,accounting_status,accounting_note
           ) VALUES(?,?,?,?,?,'partial','Claude Code scan has not established complete cost coverage')
           ON CONFLICT(id) DO UPDATE SET
             source_app=excluded.source_app,source_home=excluded.source_home,
             source_version=COALESCE(excluded.source_version,sessions.source_version)""",
        (root_id, root_id, SOURCE_APP, str(source_home), version),
    )


def _upsert_transcript(
    conn, transcript: _Transcript, source_home: Path, root_seen: bool, *,
    backend: str | None, root_backend: str | None,
) -> None:
    root_id = _ns(transcript.root_external_id)
    _ensure_session(conn, root_id=root_id, source_home=source_home, version=transcript.version)
    if not transcript.is_subagent:
        conn.execute(
            """UPDATE sessions SET title=?,cwd=?,repo_root=?,repo_name=?,git_branch=?,
               created_at=?,updated_at=?,root_model=?,root_reasoning_effort=?,
               root_provider=?,root_backend=? WHERE id=?""",
            (
                transcript.title or "Claude Code session", transcript.cwd, transcript.cwd,
                _project_name(transcript.cwd), transcript.git_branch, transcript.created_at,
                transcript.updated_at, transcript.root_model, transcript.reasoning_effort,
                PROVIDER, root_backend, root_id,
            ),
        )
    conn.execute(
        """INSERT INTO agents(thread_id,session_id,parent_thread_id,agent_role,agent_nickname,
           agent_path,created_at,updated_at,model,model_provider,backend,reasoning_effort,
           source_rollout_path,source_kind,orphan,source_available)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
           ON CONFLICT(thread_id) DO UPDATE SET
             session_id=excluded.session_id,parent_thread_id=excluded.parent_thread_id,
             agent_role=excluded.agent_role,agent_nickname=excluded.agent_nickname,
             agent_path=excluded.agent_path,created_at=excluded.created_at,updated_at=excluded.updated_at,
             model=COALESCE(excluded.model,agents.model),model_provider=excluded.model_provider,
             backend=excluded.backend,
             reasoning_effort=COALESCE(excluded.reasoning_effort,agents.reasoning_effort),
             source_rollout_path=excluded.source_rollout_path,source_kind=excluded.source_kind,
             orphan=excluded.orphan,source_available=1""",
        (
            transcript.thread_id, root_id,
            root_id if transcript.is_subagent else None,
            "subagent" if transcript.is_subagent else "root",
            transcript.agent_external_id,
            f"/root/{transcript.agent_external_id}" if transcript.is_subagent else "/root",
            transcript.created_at, transcript.updated_at, transcript.root_model, PROVIDER, backend,
            transcript.reasoning_effort,
            str(transcript.path), SOURCE_APP, int(transcript.is_subagent and not root_seen),
        ),
    )


def _billing_mode(backend: str | None) -> str:
    return "subscription" if backend == BACKEND_ANTHROPIC_OAUTH else "metered"


def _token_usage(record: _AssistantRecord) -> TokenUsage:
    usage = record.usage
    uncached = _safe_int(usage.get("input_tokens"))
    cached = _safe_int(usage.get("cache_read_input_tokens"))
    cache_write = _safe_int(usage.get("cache_creation_input_tokens"))
    output = _safe_int(usage.get("output_tokens"))
    details = usage.get("output_tokens_details")
    reasoning = _safe_int(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    return TokenUsage(
        uncached + cached + cache_write, cached, cache_write, output, reasoning,
        uncached + cached + cache_write + output,
    )


def _usage_values(
    record: _AssistantRecord, *, covered_by_cost_state: bool, cost: CostResult | None,
) -> tuple[Any, ...]:
    """Build the usage row; dollars come from exactly one of cost-state or estimate."""

    tokens = _token_usage(record)
    billing = _billing_mode(record.backend)
    subscription = billing == "subscription"
    price_id = None
    components: tuple[str | None, ...] = (None, None, None, None)
    if covered_by_cost_state:
        cost_usd, equivalent = "0", ("0" if subscription else None)
        note = _COVERED_NOTE
    elif record.fast:
        cost_usd, equivalent = ("0", None) if subscription else (None, None)
        note = "fast-mode request; standard list price not applicable"
    elif cost is not None and cost.price_id is not None:
        price_id = cost.price_id
        components = tuple(
            str(value) for value in
            (cost.uncached_input_usd, cost.cached_input_usd, cost.cache_write_usd, cost.output_usd)
        )
        cost_usd, equivalent = subscription_split(str(cost.total_usd), subscription)
        note = (
            "subscription usage: real cost $0; equivalent API value estimated from built-in Anthropic list price"
            if subscription
            else "estimated from built-in Anthropic list price; no complete cost-state covers this message"
        )
        if cost.note:
            note = f"{note}; {cost.note}"
    else:
        cost_usd, equivalent = ("0", None) if subscription else (None, None)
        note = (
            f"no built-in Anthropic price for {record.model}; "
            "Claude transcript has no complete cumulative cost-state"
        )
    return (
        record.identity, record.session_id, record.thread_id, record.identity, record.identity,
        record.timestamp, record.model, PROVIDER, record.backend, billing,
        tokens.input_tokens, tokens.cached_input_tokens, tokens.cache_write_input_tokens,
        record.cache_write_1h, tokens.uncached_input_tokens, tokens.output_tokens,
        tokens.reasoning_output_tokens, tokens.total_tokens,
        str(record.source_path), record.ordinal, _ASSISTANT_EVENT, record.call_label,
        price_id, *components, cost_usd, equivalent, note,
    )


def _upsert_usage(conn, values: tuple[Any, ...]) -> bool:
    identity = values[0]
    exists = conn.execute("SELECT 1 FROM usage WHERE source_record_identity=?", (identity,)).fetchone()
    conn.execute(
        """INSERT INTO usage(source_record_identity,session_id,thread_id,turn_id,response_id,
           timestamp,model,provider,backend,billing_mode,input_tokens,cached_input_tokens,
           cache_write_input_tokens,cache_write_1h_input_tokens,uncached_input_tokens,output_tokens,
           reasoning_output_tokens,total_tokens,source_file,source_ordinal,source_event_type,call_label,
           price_id,uncached_input_usd,cached_input_usd,cache_write_usd,output_usd,cost_usd,
           equivalent_cost_usd,pricing_note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_record_identity) DO UPDATE SET
             session_id=excluded.session_id,thread_id=excluded.thread_id,turn_id=excluded.turn_id,
             response_id=excluded.response_id,timestamp=excluded.timestamp,model=excluded.model,
             provider=excluded.provider,backend=excluded.backend,billing_mode=excluded.billing_mode,
             input_tokens=excluded.input_tokens,cached_input_tokens=excluded.cached_input_tokens,
             cache_write_input_tokens=excluded.cache_write_input_tokens,
             cache_write_1h_input_tokens=excluded.cache_write_1h_input_tokens,
             uncached_input_tokens=excluded.uncached_input_tokens,output_tokens=excluded.output_tokens,
             reasoning_output_tokens=excluded.reasoning_output_tokens,total_tokens=excluded.total_tokens,
             source_file=excluded.source_file,source_ordinal=excluded.source_ordinal,
             source_event_type=excluded.source_event_type,call_label=excluded.call_label,
             price_id=excluded.price_id,uncached_input_usd=excluded.uncached_input_usd,
             cached_input_usd=excluded.cached_input_usd,cache_write_usd=excluded.cache_write_usd,
             output_usd=excluded.output_usd,cost_usd=excluded.cost_usd,
             equivalent_cost_usd=excluded.equivalent_cost_usd,pricing_note=excluded.pricing_note""",
        values,
    )
    return exists is not None


def _normalized_costs(state: _CostState) -> dict[str, Decimal]:
    """Return a model split whose sum matches the authoritative source total."""

    model_costs = dict(state.model_costs)
    accounted = sum(model_costs.values(), Decimal("0"))
    if accounted > state.total_cost:
        return {"claude-code-cumulative-total": state.total_cost}
    if state.total_cost > accounted:
        model_costs["claude-code-cumulative-adjustment"] = state.total_cost - accounted
    return model_costs


def _cost_changes(
    root: str, states: list[_CostState]
) -> list[tuple[str, str, Decimal, _CostState]]:
    """Convert cumulative snapshots into dated changes without rewriting history."""

    previous: dict[str, Decimal] = {}
    changes: list[tuple[str, str, Decimal, _CostState]] = []
    for state in states:
        current = _normalized_costs(state)
        for model in sorted(set(previous) | set(current)):
            change = current.get(model, Decimal("0")) - previous.get(model, Decimal("0"))
            if change:
                identity = _ns(f"cost:{root}:{state.ordinal}:{model}")
                changes.append((identity, model, change, state))
        previous = current
    return changes


def _upsert_cost(
    conn, *, identity: str, root_id: str, model: str, source_path: Path,
    cost: Decimal, timestamp: str | None, ordinal: int, backend: str | None,
) -> bool:
    exists = conn.execute("SELECT 1 FROM usage WHERE source_record_identity=?", (identity,)).fetchone()
    billing = _billing_mode(backend)
    cost_usd, equivalent = subscription_split(format(cost, "f"), billing == "subscription")
    note = "Change between Claude Code cumulative cost-state snapshots"
    if billing == "subscription":
        note += " (subscription equivalent value)"
    elif backend == BACKEND_MIXED:
        note += " (mixed backends; cost-state cannot be split)"
    conn.execute(
        """INSERT INTO usage(source_record_identity,session_id,thread_id,turn_id,response_id,
           timestamp,model,provider,backend,billing_mode,input_tokens,cached_input_tokens,
           cache_write_input_tokens,uncached_input_tokens,output_tokens,reasoning_output_tokens,
           total_tokens,source_file,source_ordinal,source_event_type,call_label,price_id,
           uncached_input_usd,cached_input_usd,cache_write_usd,output_usd,cost_usd,equivalent_cost_usd,
           pricing_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_record_identity) DO UPDATE SET timestamp=excluded.timestamp,model=excluded.model,
             backend=excluded.backend,billing_mode=excluded.billing_mode,
             turn_id=NULL,response_id=NULL,call_label=excluded.call_label,
             source_file=excluded.source_file,source_ordinal=excluded.source_ordinal,cost_usd=excluded.cost_usd,
             equivalent_cost_usd=excluded.equivalent_cost_usd,pricing_note=excluded.pricing_note""",
        (
            identity, root_id, root_id, None, None,
            timestamp or datetime.now(UTC).isoformat(), price_model(model), PROVIDER, backend, billing,
            0, 0, 0, 0, 0, 0, 0,
            str(source_path), ordinal, _COST_EVENT, "Cumulative cost state", None, None, None, None, None,
            cost_usd, equivalent, note,
        ),
    )
    return exists is not None


def _remove_stale_usage(
    conn, *, event: str, scope: str, params: tuple[Any, ...], identities: set[str],
    keep_files: frozenset[str] | set[str] = frozenset(),
) -> None:
    # Rows from transcripts that were skipped as unchanged were not reread, so
    # their identities are absent from ``identities`` and must be left alone.
    stored = {
        row[0] for row in conn.execute(
            f"SELECT source_record_identity,source_file FROM usage WHERE source_event_type=? AND {scope}",
            (event, *params),
        )
        if row[1] not in keep_files
    }
    conn.executemany(
        "DELETE FROM usage WHERE source_event_type=? AND source_record_identity=?",
        ((event, identity) for identity in stored - identities),
    )


def _reconcile_transcript_records(
    conn, transcript: _Transcript, assistant_ids: set[str], cost_ids: set[str]
) -> None:
    """Reconcile only a transcript that was read without errors."""

    _remove_stale_usage(
        conn, event=_ASSISTANT_EVENT, scope="source_file=?",
        params=(str(transcript.path),), identities=assistant_ids,
    )
    if not transcript.is_subagent:
        _remove_stale_usage(
            conn, event=_COST_EVENT, scope="session_id=?",
            params=(_ns(transcript.root_external_id),), identities=cost_ids,
        )


def _reconcile(
    conn, *, transcript_ids: set[str], usage_ids: set[str], keep_files: set[str]
) -> None:
    _remove_stale_usage(
        conn, event=_ASSISTANT_EVENT,
        scope="source_record_identity LIKE 'claude:%'", params=(),
        identities={item for item in usage_ids if not item.startswith(_ns("cost:"))},
        keep_files=keep_files,
    )
    _remove_stale_usage(
        conn, event=_COST_EVENT,
        scope="source_record_identity LIKE 'claude:%'", params=(),
        identities={item for item in usage_ids if item.startswith(_ns("cost:"))},
        keep_files=keep_files,
    )

    stored_transcripts = {
        row[0] for row in conn.execute(
            "SELECT thread_id FROM agents WHERE source_kind='claude' AND thread_id LIKE 'claude:%'"
        )
    }
    conn.executemany(
        "DELETE FROM agents WHERE source_kind='claude' AND thread_id=?",
        ((identity,) for identity in stored_transcripts - transcript_ids),
    )
    conn.execute(
        "DELETE FROM sessions WHERE source_app='claude' AND id LIKE 'claude:%' "
        "AND NOT EXISTS(SELECT 1 FROM agents WHERE agents.session_id=sessions.id)"
    )


def _refresh_turn_counts(conn) -> None:
    conn.execute(
        """UPDATE sessions SET turn_count=(SELECT COUNT(*) FROM usage
               WHERE usage.session_id=sessions.id AND usage.source_event_type=?)
           WHERE source_app='claude' AND id LIKE 'claude:%'""",
        (_ASSISTANT_EVENT,),
    )


def _refresh_coverage(conn, coverage: dict[str, tuple[str, str]]) -> None:
    """Record how each Claude session's dollars are accounted for."""

    for root_id, (status, note) in coverage.items():
        conn.execute(
            "UPDATE sessions SET accounting_status=?,accounting_note=? WHERE id=?",
            (status, note or None, root_id),
        )


def _estimate_status(base_note: str, priced: int, total: int) -> tuple[str, str]:
    """Describe a unit whose dollars come from list prices instead of cost-state."""

    if total and priced == total:
        return "estimated", f"{base_note}; priced from built-in Anthropic list prices"
    if priced:
        return "partial", f"{base_note}; some records have no built-in Anthropic price"
    return "partial", base_note


def _session_liveness(updated_at: str | None, running_window_seconds: int, now: datetime) -> tuple[str, str | None]:
    """Return session status from the latest root or child transcript activity."""

    if updated_at and running_window_seconds > 0:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated = None
        if updated is not None and updated >= now - timedelta(seconds=running_window_seconds):
            return "running", None
    return "completed", updated_at


def ingest_claude(
    settings: Settings, force_all: bool = False, *, now: datetime | None = None
) -> ClaudeIngestSummary:
    """Ingest Claude Code transcript accounting without persisting content.

    Claude Code can append later snapshots for the same message ID, so a
    changed transcript is always reread completely and upserted by identity.
    A root session and its subagent transcripts form one unit.  When every
    transcript in a unit still matches the inode, size, and mtime recorded
    after its last complete import, the unit is skipped without opening a
    file; its rows are left untouched by reconciliation and only its liveness
    status is recomputed from the stored activity timestamp.  ``force_all``
    rereads everything.
    """

    settings.validate()
    home = discover_claude_home(settings)
    profile = claude_auth_profile(settings)
    summary = ClaudeIngestSummary(source_home=home, auth_profile=profile)
    projects = home / "projects"
    discovery = _transcript_paths(home, summary)
    if discovery is None:
        return summary
    paths, discovery_complete = discovery
    summary.scanned_files = len(paths)
    transcripts = [_transcript_for(path, projects) for path in paths]
    transcripts = [item for item in transcripts if item is not None]
    root_files = {item.root_external_id for item in transcripts if not item.is_subagent}
    groups: dict[str, list[_Transcript]] = {}
    for transcript in transcripts:
        groups.setdefault(transcript.root_external_id, []).append(transcript)
    # Fingerprints are taken before a transcript is read.  A write that lands
    # between the stat and the read therefore still changes the fingerprint
    # relative to what is recorded, and the transcript is reread next pass.
    fingerprints = {str(item.path): _fingerprint(item.path) for item in transcripts}

    initialize(settings.database)
    scan_now = (now or datetime.now(UTC)).astimezone(UTC)
    running_window = int(getattr(settings, "running_window_seconds", 0))
    with database(settings.database) as conn:
        seed_prices(conn)
        # A changed login profile relabels every ``msg_`` call, so units that
        # would otherwise be skipped as unchanged must be reread once.
        previous_auth = _read_meta(conn, _META_AUTH_BACKEND)
        if previous_auth != profile.anthropic_backend:
            force_all = True
        # Reading the recorded fingerprints does not open a write transaction,
        # so the dashboard database stays unlocked while transcripts are read.
        stored = {} if force_all else _stored_fingerprints(conn)
        # A unit is unchanged only when its membership is unchanged too: a
        # removed subagent transcript leaves the root file untouched but must
        # still trigger a reread so coverage and reconciliation see the loss.
        stored_members: dict[str, set[str]] = {}
        for path in stored:
            root = _group_of(path, projects)
            if root is not None:
                stored_members.setdefault(root, set()).add(path)
        skipped_roots = {
            root for root, items in groups.items()
            if stored_members.get(root) == {str(item.path) for item in items}
            and all(
                fingerprints[str(item.path)] is not None
                and stored[str(item.path)] == fingerprints[str(item.path)]
                for item in items
            )
        }
        skipped_files = {str(item.path) for root in skipped_roots for item in groups[root]}
        summary.unchanged_files = len(skipped_files)

        assistants: dict[str, _AssistantRecord] = {}
        cost_states: dict[str, list[_CostState]] = {}
        readable_transcripts: list[_Transcript] = []
        complete_transcript_ids: set[str] = set()
        # A unit is complete when the directory scan was complete and every one
        # of its transcripts was read without error.  Only then do its derived
        # coverage, liveness, and fingerprints describe the whole unit.
        group_complete: dict[str, bool] = {}
        for root, items in groups.items():
            if root in skipped_roots:
                continue
            complete = discovery_complete
            for transcript in items:
                rows, states, transcript_complete, readable = _read_transcript(transcript, summary)
                complete = complete and transcript_complete
                if not readable:
                    continue
                readable_transcripts.append(transcript)
                if transcript_complete:
                    complete_transcript_ids.add(transcript.thread_id)
                assistants.update(rows)
                if not transcript.is_subagent:
                    cost_states[root] = states
            group_complete[root] = complete
        scan_complete = discovery_complete and all(group_complete.values())

        # Transcripts only know that a call went to Anthropic directly; the
        # login profile says whether that is a subscription or an API key.
        for record in assistants.values():
            if record.backend == BACKEND_ANTHROPIC:
                record.backend = profile.anthropic_backend
        for transcript in readable_transcripts:
            if BACKEND_ANTHROPIC in transcript.backends:
                transcript.backends.discard(BACKEND_ANTHROPIC)
                transcript.backends.add(profile.anthropic_backend)
        unit_backends: dict[str, str | None] = {}
        for transcript in readable_transcripts:
            unit_backends[transcript.root_external_id] = _unit_backend(
                set().union(*(
                    item.backends for item in readable_transcripts
                    if item.root_external_id == transcript.root_external_id
                ))
            )

        current_transcript_ids = {item.thread_id for item in transcripts}
        readable_roots = {item.root_external_id for item in readable_transcripts}
        for root in readable_roots:
            root_item = next((
                item for item in readable_transcripts
                if item.root_external_id == root and not item.is_subagent
            ), None)
            _ensure_session(
                conn, root_id=_ns(root), source_home=home, version=root_item.version if root_item else None
            )
        for transcript in readable_transcripts:
            _upsert_transcript(
                conn, transcript, home, transcript.root_external_id in root_files,
                backend=_unit_backend(transcript.backends),
                root_backend=unit_backends.get(transcript.root_external_id),
            )
        # A child can continue producing messages after the root has become
        # idle.  Liveness belongs to the task/session, so use the latest
        # envelope timestamp across every transcript associated with that root.
        for root, items in groups.items():
            root_id = _ns(root)
            if root in skipped_roots:
                # Nothing on disk changed, but the running window may have
                # elapsed since the stored activity timestamp was written.
                stored_row = conn.execute("SELECT updated_at FROM sessions WHERE id=?", (root_id,)).fetchone()
                if stored_row is None:
                    continue
                status, finished_at = _session_liveness(stored_row[0], running_window, scan_now)
                conn.execute(
                    "UPDATE sessions SET status=?,finished_at=? WHERE id=?", (status, finished_at, root_id)
                )
            elif group_complete[root]:
                latest = max((item.updated_at for item in items if item.updated_at), default=None)
                status, finished_at = _session_liveness(latest, running_window, scan_now)
                conn.execute(
                    "UPDATE sessions SET updated_at=?,status=?,finished_at=? WHERE id=?",
                    (latest, status, finished_at, root_id),
                )
        # A root cost-state is Claude Code's own cumulative total for the whole
        # session, subagent calls included (observed: models that only appear
        # in subagent transcripts are listed in the root's cost-state).  Units
        # without any cost-state are priced from list prices instead; a unit
        # never gets both, so dollars are not counted twice.
        coverage_notes: dict[str, str] = {}
        root_cost_coverage: dict[str, bool] = {}
        estimable_roots: set[str] = set()
        for root in readable_roots:
            root_id = _ns(root)
            states = cost_states.get(root, [])
            state = states[-1] if states else None
            if state is None:
                coverage_notes[root_id] = "Claude Code transcript has no cumulative cost-state"
                estimable_roots.add(root_id)
            elif state.complete:
                root_cost_coverage[root_id] = root_id in complete_transcript_ids
                coverage_notes[root_id] = ""
            else:
                coverage_notes[root_id] = (
                    "Claude Code cost-state reports unknown model cost; cumulative session cost is partial"
                )
        priced_by_root: dict[str, list[int]] = {}
        for record in assistants.values():
            covered = root_cost_coverage.get(record.session_id, False)
            cost = None
            # Orphaned subagent transcripts have no root and no cost-state, so
            # their rows are estimated like any unit without a cost-state.
            estimable = record.session_id in estimable_roots or record.session_id not in coverage_notes
            if not covered and estimable and not record.fast:
                cost = estimate_cost(
                    conn, _token_usage(record), record.model, PROVIDER, record.timestamp,
                    cache_write_1h_tokens=record.cache_write_1h,
                )
            priced = covered or (cost is not None and cost.price_id is not None)
            if not priced:
                summary.unknown_prices.add(
                    f"{PROVIDER}:{record.model}:fast" if record.fast else f"{PROVIDER}:{record.model}"
                )
            counts = priced_by_root.setdefault(record.session_id, [0, 0])
            counts[0] += int(priced and not covered)
            counts[1] += 1
            summary.backends[record.backend] = summary.backends.get(record.backend, 0) + 1
            values = _usage_values(record, covered_by_cost_state=covered, cost=cost)
            if _upsert_usage(conn, values):
                summary.duplicate_records += 1
            else:
                summary.usage_records += 1
                if cost is not None and cost.total_usd is not None:
                    summary.estimated_records += 1
                    if _billing_mode(record.backend) == "subscription":
                        summary.subscription_value += float(cost.total_usd)
                    else:
                        summary.estimated_spend += float(cost.total_usd)
        coverage: dict[str, tuple[str, str]] = {}
        for root_id, note in coverage_notes.items():
            if root_cost_coverage.get(root_id):
                coverage[root_id] = ("complete", "")
            elif root_id in estimable_roots:
                coverage[root_id] = _estimate_status(note, *priced_by_root.get(root_id, [0, 0]))
            else:
                coverage[root_id] = ("partial", note)
        current_usage_ids = set(assistants)
        cost_ids_by_root: dict[str, set[str]] = {}
        for root, states in cost_states.items():
            root_id = _ns(root)
            # Cost snapshots are safe to replace only when the root transcript
            # was completely readable. Valid assistant rows from a partial
            # transcript are still imported below as unpriced usage.
            if root_id not in complete_transcript_ids:
                continue
            root_cost_ids = cost_ids_by_root.setdefault(root, set())
            for identity, model, cost, state in _cost_changes(root, states):
                current_usage_ids.add(identity)
                root_cost_ids.add(identity)
                existed = _upsert_cost(
                    conn, identity=identity, root_id=root_id, model=model,
                    source_path=state.source_path, cost=cost,
                    timestamp=state.timestamp, ordinal=state.ordinal,
                    backend=unit_backends.get(root),
                )
                if existed:
                    summary.duplicate_records += 1
                elif _billing_mode(unit_backends.get(root)) == "subscription":
                    summary.usage_records += 1
                    summary.subscription_value += float(cost)
                else:
                    summary.usage_records += 1
                    summary.estimated_spend += float(cost)
        if scan_complete:
            _reconcile(
                conn, transcript_ids=current_transcript_ids, usage_ids=current_usage_ids,
                keep_files=skipped_files,
            )
            _prune_fingerprints(conn, set(fingerprints))
        else:
            # A failure elsewhere must not block safe replacement within a
            # transcript that was itself read completely.
            for transcript in readable_transcripts:
                if transcript.thread_id not in complete_transcript_ids:
                    continue
                transcript_assistant_ids = {
                    identity for identity, record in assistants.items()
                    if record.source_path == transcript.path
                }
                _reconcile_transcript_records(
                    conn, transcript, transcript_assistant_ids,
                    cost_ids_by_root.get(transcript.root_external_id, set()),
                )
        # Coverage depends on every transcript of a unit, so it is written per
        # complete unit rather than only when the whole scan was clean.
        _refresh_coverage(conn, {
            _ns(root): coverage[_ns(root)]
            for root in readable_roots if group_complete[root] and _ns(root) in coverage
        })
        _store_fingerprints(
            conn,
            {
                str(item.path): fingerprints[str(item.path)]
                for root, complete in group_complete.items() if complete
                for item in groups[root] if fingerprints[str(item.path)] is not None
            },
            scan_now.isoformat(),
        )
        _refresh_turn_counts(conn)
        _write_meta(conn, _META_AUTH_BACKEND, profile.anthropic_backend)
        summary.scanned_sessions = len(transcripts)
        summary.root_sessions = int(conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_app='claude' AND id LIKE 'claude:%'"
        ).fetchone()[0])
        summary.subagent_sessions = int(conn.execute(
            "SELECT COUNT(*) FROM agents WHERE source_kind='claude' AND parent_thread_id IS NOT NULL"
        ).fetchone()[0])
    return summary
