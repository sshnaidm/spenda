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
from ..pricing import (
    BILLING_UNRESOLVED,
    CLAUDE_COVERED_NOTE,
    CLAUDE_LATE_CALLS_NOTE,
    CLAUDE_LIST_PRICED_BACKENDS,
    CLAUDE_NO_COST_STATE_NOTE,
    CLAUDE_WITHHELD_NOTE,
    FAST_MODE_NOTE,
    claude_estimate_note,
    claude_estimate_status,
    claude_no_price_note,
    estimate_cost,
    price_model,
    seed_prices,
    subscription_split,
)
from .action_labels import prefer_action_label, safe_action_label
from .claude_auth import (
    BACKEND_ANTHROPIC,
    BACKEND_ANTHROPIC_API,
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
_COST_STATE_COMPLETE = "complete"
_COST_STATE_PARTIAL = "partial"
_PARTIAL_COST_STATE_NOTE = (
    "Claude Code cost-state reports unknown model cost; cumulative session cost is partial"
)
_COVERED_NOTE = CLAUDE_COVERED_NOTE
_WITHHELD_NOTE = CLAUDE_WITHHELD_NOTE
_UNVERIFIED_NOTE = (
    "Anthropic billing is unverified (no subscription login or API key found); pass --claude-billing to classify"
)
# Why a cost-state total cannot be split into real spend and subscription value.
_UNRESOLVED_INCOMPLETE = "has incomplete backend coverage"
_UNRESOLVED_UNVERIFIED = "includes Anthropic calls whose billing is unverified"
_UNRESOLVED_MIXED = "spans subscription and metered backends"
# Bump when the values derived from a transcript change, so recorded
# fingerprints from an older parser stop suppressing a reread.
PARSER_VERSION = 7


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
    recorded_spend: float = 0.0
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
    human_turns: int = 0
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


_COST_TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "uncached_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


@dataclass(frozen=True, slots=True)
class _CostTokens:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    uncached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    def minus(self, previous: _CostTokens) -> _CostTokens:
        return _CostTokens(*(getattr(self, name) - getattr(previous, name) for name in _COST_TOKEN_FIELDS))

    def plus(self, other: _CostTokens) -> _CostTokens:
        return _CostTokens(*(getattr(self, name) + getattr(other, name) for name in _COST_TOKEN_FIELDS))

    def any(self) -> bool:
        return any(getattr(self, name) for name in _COST_TOKEN_FIELDS)


@dataclass(slots=True)
class _CostState:
    model_costs: dict[str, Decimal]
    model_tokens: dict[str, _CostTokens]
    has_token_totals: bool
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


def _prune_fingerprints(conn, current_paths: set[str], projects: Path) -> None:
    stored = {
        row[0] for row in conn.execute(
            "SELECT source_path FROM ingestion_state WHERE source_key LIKE 'claude:%'"
        )
        if _group_of(row[0], projects) is not None
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


def _instant(value: str | None) -> datetime | None:
    """Parse a stored timestamp for ordering; string order breaks on fractions."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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


def _is_human_prompt(record: dict[str, Any]) -> bool:
    """Identify a real root prompt from structured envelope metadata only."""

    if record.get("isSidechain") is True:
        return False
    origin = record.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    if record.get("promptSource") == "system" or record.get("queueSkipAttachments") is True:
        return False
    message = record.get("message")
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
    )


def _cost_tokens(detail: dict[str, Any]) -> _CostTokens:
    uncached = _safe_int(detail.get("inputTokens"))
    cached = _safe_int(detail.get("cacheReadInputTokens"))
    cache_write = _safe_int(detail.get("cacheCreationInputTokens"))
    output = _safe_int(detail.get("outputTokens"))
    reasoning = _safe_int(detail.get("thinkingTokens"))
    input_tokens = uncached + cached + cache_write
    return _CostTokens(
        input_tokens, cached, cache_write, uncached, output, reasoning, input_tokens + output
    )


def _read_transcript(
    transcript: _Transcript,
    summary: ClaudeIngestSummary,
) -> tuple[dict[str, _AssistantRecord], list[_CostState], bool, bool]:
    """Return valid rows, all cost snapshots, completeness, and readability."""

    assistant: dict[str, _AssistantRecord] = {}
    cost_states: list[_CostState] = []
    human_prompts: set[str] = set()
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
                if not line.endswith(b"\n"):
                    # Claude Code is still writing this final line.  Its bytes
                    # are part of the recorded fingerprint, so the transcript is
                    # reread once the line is finished.
                    continue
                summary.malformed_lines += 1
                complete = False
                continue
            if not isinstance(record, dict):
                continue
            _update_metadata(transcript, record)
            kind = record.get("type")
            if kind == "ai-title" and not transcript.is_subagent:
                transcript.title = _safe_title(record.get("aiTitle")) or transcript.title
            elif kind == "user" and not transcript.is_subagent and _is_human_prompt(record):
                prompt_id = record.get("uuid")
                human_prompts.add(prompt_id if isinstance(prompt_id, str) else f"line:{ordinal}")
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
                model = price_model(model)
                transcript.root_model = transcript.root_model or model
                # Claude copies shared history into every spawned subagent
                # transcript. API message ids are session-global, so use a
                # fixed root namespace rather than the transcript/agent id.
                identity = _ns(f"{transcript.root_external_id}:root:{message_id}")
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
                model_tokens: dict[str, _CostTokens] = {}
                has_token_totals = bool(model_usage)
                for source_model, detail in model_usage.items():
                    if not isinstance(source_model, str) or not isinstance(detail, dict):
                        has_token_totals = False
                        continue
                    model = price_model(source_model)
                    cost = _safe_cost(detail.get("costUSD"))
                    if cost is not None:
                        model_costs[model] = model_costs.get(model, Decimal("0")) + cost
                    token_keys = (
                        "inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "outputTokens",
                    )
                    if all(key in detail for key in token_keys):
                        model_tokens[model] = model_tokens.get(model, _CostTokens()).plus(
                            _cost_tokens(detail)
                        )
                    else:
                        has_token_totals = False
                # Each line is a complete cumulative snapshot. Keep snapshots
                # separate so ingestion can derive dated changes later.
                cost_states.append(_CostState(
                    model_costs=model_costs,
                    model_tokens=model_tokens,
                    has_token_totals=has_token_totals,
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
    transcript.human_turns = len(human_prompts)
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
               root_provider=?,root_backend=?,turn_count=? WHERE id=?""",
            (
                transcript.title or "Claude Code session", transcript.cwd, transcript.cwd,
                _project_name(transcript.cwd), transcript.git_branch, transcript.created_at,
                transcript.updated_at, transcript.root_model, transcript.reasoning_effort,
                PROVIDER, root_backend, transcript.human_turns, root_id,
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
    record: _AssistantRecord, *, covered_by_cost_state: bool,
    covered_by_token_state: bool, cost: CostResult | None, withheld: bool = False,
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
        note = FAST_MODE_NOTE
    elif cost is not None and cost.price_id is not None:
        price_id = cost.price_id
        components = tuple(
            str(value) for value in
            (cost.uncached_input_usd, cost.cached_input_usd, cost.cache_write_usd, cost.output_usd)
        )
        cost_usd, equivalent = subscription_split(str(cost.total_usd), subscription)
        note = claude_estimate_note(subscription, cost.note, record.backend)
    else:
        cost_usd, equivalent = ("0", None) if subscription else (None, None)
        if withheld:
            note = _WITHHELD_NOTE
        elif record.backend == BACKEND_ANTHROPIC:
            note = _UNVERIFIED_NOTE
        else:
            note = claude_no_price_note(record.model)
    return (
        record.identity, record.session_id, record.thread_id, record.identity, record.identity,
        record.timestamp, record.model, PROVIDER, record.backend, billing,
        tokens.input_tokens, tokens.cached_input_tokens, tokens.cache_write_input_tokens,
        record.cache_write_1h, tokens.uncached_input_tokens, tokens.output_tokens,
        tokens.reasoning_output_tokens, tokens.total_tokens, int(not covered_by_token_state),
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
           reasoning_output_tokens,total_tokens,counts_toward_totals,source_file,source_ordinal,
           source_event_type,call_label,
           price_id,uncached_input_usd,cached_input_usd,cache_write_usd,output_usd,cost_usd,
           equivalent_cost_usd,pricing_note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_record_identity) DO UPDATE SET
             session_id=excluded.session_id,thread_id=excluded.thread_id,turn_id=excluded.turn_id,
             response_id=excluded.response_id,timestamp=excluded.timestamp,model=excluded.model,
             provider=excluded.provider,backend=excluded.backend,billing_mode=excluded.billing_mode,
             input_tokens=excluded.input_tokens,cached_input_tokens=excluded.cached_input_tokens,
             cache_write_input_tokens=excluded.cache_write_input_tokens,
             cache_write_1h_input_tokens=excluded.cache_write_1h_input_tokens,
             uncached_input_tokens=excluded.uncached_input_tokens,output_tokens=excluded.output_tokens,
             reasoning_output_tokens=excluded.reasoning_output_tokens,total_tokens=excluded.total_tokens,
             counts_toward_totals=excluded.counts_toward_totals,
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
    root: str, states: list[_CostState], fallback_model: str = "claude-code-cumulative-total"
) -> list[tuple[str, str, Decimal, _CostTokens, _CostState]]:
    """Convert cumulative snapshots into dated changes without rewriting history."""

    previous_costs: dict[str, Decimal] = {}
    previous_tokens: dict[str, _CostTokens] = {}
    changes: list[tuple[str, str, Decimal, _CostTokens, _CostState]] = []
    for state in states:
        current_costs = _normalized_costs(state)
        current_tokens = state.model_tokens
        models = set(previous_costs) | set(current_costs) | set(previous_tokens) | set(current_tokens)
        state_change_count = 0
        for model in sorted(models):
            cost_change = current_costs.get(model, Decimal("0")) - previous_costs.get(
                model, Decimal("0")
            )
            token_change = current_tokens.get(model, _CostTokens()).minus(
                previous_tokens.get(model, _CostTokens())
            )
            if cost_change or token_change.any():
                identity = _ns(f"cost:{root}:{state.ordinal}:{model}")
                changes.append((identity, model, cost_change, token_change, state))
                state_change_count += 1
        # A zero-dollar initial cost-state is still authoritative source
        # evidence. Retain a marker row so strict backend filtering and the
        # session detail do not mistake it for a session with no cost-state.
        if not changes and not state_change_count:
            model = sorted(models)[0] if models else fallback_model
            identity = _ns(f"cost:{root}:{state.ordinal}:{model}")
            changes.append((identity, model, Decimal("0"), _CostTokens(), state))
        previous_costs = current_costs
        previous_tokens = current_tokens
    return changes


def _upsert_cost(
    conn, *, identity: str, root_id: str, model: str, source_path: Path,
    cost: Decimal, tokens: _CostTokens, timestamp: str | None, ordinal: int,
    backend: str | None, counts_toward_totals: bool, unresolved_reason: str | None = None,
) -> bool:
    exists = conn.execute("SELECT 1 FROM usage WHERE source_record_identity=?", (identity,)).fetchone()
    # Incomplete backend coverage or unverified Anthropic billing can hide
    # subscription calls even when every known call is metered. Like a known
    # subscription/metered mix, that leaves both real spend and equivalent
    # value unknown.
    billing = BILLING_UNRESOLVED if unresolved_reason else _billing_mode(backend)
    cost_usd, equivalent = (
        (None, None)
        if unresolved_reason
        else subscription_split(format(cost, "f"), billing == "subscription")
    )
    note = "Change between Claude Code cumulative cost-state snapshots"
    if unresolved_reason:
        note += f" (source total ${format(cost, 'f')} {unresolved_reason}; real spend cannot be separated)"
    elif billing == "subscription":
        note += " (subscription equivalent value)"
    elif backend == BACKEND_MIXED:
        note += " (mixed backends; cost-state cannot be split)"
    conn.execute(
        """INSERT INTO usage(source_record_identity,session_id,thread_id,turn_id,response_id,
           timestamp,model,provider,backend,billing_mode,input_tokens,cached_input_tokens,
           cache_write_input_tokens,cache_write_1h_input_tokens,uncached_input_tokens,output_tokens,
           reasoning_output_tokens,total_tokens,counts_toward_totals,source_file,source_ordinal,
           source_event_type,call_label,price_id,
           uncached_input_usd,cached_input_usd,cache_write_usd,output_usd,cost_usd,equivalent_cost_usd,
           pricing_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_record_identity) DO UPDATE SET timestamp=excluded.timestamp,model=excluded.model,
             backend=excluded.backend,billing_mode=excluded.billing_mode,
             turn_id=NULL,response_id=NULL,call_label=excluded.call_label,
             source_file=excluded.source_file,source_ordinal=excluded.source_ordinal,cost_usd=excluded.cost_usd,
             input_tokens=excluded.input_tokens,cached_input_tokens=excluded.cached_input_tokens,
             cache_write_input_tokens=excluded.cache_write_input_tokens,
             uncached_input_tokens=excluded.uncached_input_tokens,output_tokens=excluded.output_tokens,
             reasoning_output_tokens=excluded.reasoning_output_tokens,total_tokens=excluded.total_tokens,
             counts_toward_totals=excluded.counts_toward_totals,
             equivalent_cost_usd=excluded.equivalent_cost_usd,
             pricing_note=excluded.pricing_note""",
        (
            identity, root_id, root_id, None, None,
            timestamp or datetime.now(UTC).isoformat(), price_model(model), PROVIDER, backend, billing,
            tokens.input_tokens, tokens.cached_input_tokens, tokens.cache_write_input_tokens, 0,
            tokens.uncached_input_tokens, tokens.output_tokens, tokens.reasoning_output_tokens,
            tokens.total_tokens, int(counts_toward_totals),
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
    conn, *, source_home: Path, transcript_ids: set[str], usage_ids: set[str], keep_files: set[str]
) -> None:
    scope = "session_id IN (SELECT id FROM sessions WHERE source_app='claude' AND source_home=?)"
    params = (str(source_home),)
    _remove_stale_usage(
        conn, event=_ASSISTANT_EVENT,
        scope=scope, params=params,
        identities={item for item in usage_ids if not item.startswith(_ns("cost:"))},
        keep_files=keep_files,
    )
    _remove_stale_usage(
        conn, event=_COST_EVENT,
        scope=scope, params=params,
        identities={item for item in usage_ids if item.startswith(_ns("cost:"))},
        keep_files=keep_files,
    )

    stored_transcripts = {
        row[0] for row in conn.execute(
            "SELECT thread_id FROM agents WHERE source_kind='claude' AND thread_id LIKE 'claude:%' "
            f"AND {scope}", params,
        )
    }
    conn.executemany(
        "DELETE FROM agents WHERE source_kind='claude' AND thread_id=?",
        ((identity,) for identity in stored_transcripts - transcript_ids),
    )
    conn.execute(
        "DELETE FROM sessions WHERE source_app='claude' AND id LIKE 'claude:%' AND source_home=? "
        "AND NOT EXISTS(SELECT 1 FROM agents WHERE agents.session_id=sessions.id)", params,
    )


def _refresh_coverage(conn, coverage: dict[str, tuple[str, str]]) -> None:
    """Record how each Claude session's dollars are accounted for."""

    for root_id, (status, note) in coverage.items():
        conn.execute(
            "UPDATE sessions SET accounting_status=?,accounting_note=? WHERE id=?",
            (status, note or None, root_id),
        )


def _unresolved_reason(complete: bool, backends: set[str]) -> str | None:
    """Why a unit's cost-state cannot be split into real spend and subscription value."""

    if not complete:
        return _UNRESOLVED_INCOMPLETE
    if BACKEND_ANTHROPIC in backends:
        return _UNRESOLVED_UNVERIFIED
    if BACKEND_ANTHROPIC_OAUTH in backends and len(backends) > 1:
        return _UNRESOLVED_MIXED
    return None


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
        # A changed login profile relabels every ``msg_`` call, so units with
        # Anthropic-direct rows must be reread once even when unchanged on
        # disk; Vertex and Bedrock units are unaffected and stay skipped.
        relabel_roots: set[str] = set()
        if _read_meta(conn, _META_AUTH_BACKEND) != profile.anthropic_backend and not force_all:
            relabel_roots = {
                row[0] for row in conn.execute(
                    "SELECT DISTINCT session_id FROM usage WHERE source_event_type=? AND backend IN (?,?,?)",
                    (_ASSISTANT_EVENT, BACKEND_ANTHROPIC, BACKEND_ANTHROPIC_OAUTH, BACKEND_ANTHROPIC_API),
                )
            }
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
            if _ns(root) not in relabel_roots
            and stored_members.get(root) == {str(item.path) for item in items}
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
                for identity, record in rows.items():
                    previous = assistants.get(identity)
                    if previous is None:
                        assistants[identity] = record
                    else:
                        label = prefer_action_label(previous.call_label, record.call_label)
                        # Prefer the root transcript as the audit source when
                        # a copied response is present in both root and child.
                        if previous.thread_id != previous.session_id and record.thread_id == record.session_id:
                            assistants[identity] = record
                            previous = record
                        previous.call_label = label or previous.call_label
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
        unit_backend_sets: dict[str, set[str]] = {}
        for transcript in readable_transcripts:
            unit_backend_sets.setdefault(transcript.root_external_id, set()).update(transcript.backends)
        # A failed transcript read is not evidence that its previously seen
        # backend disappeared. Keep those backends until a complete unit scan
        # can establish the new membership and billing split.
        for root, complete in group_complete.items():
            if not complete:
                unit_backend_sets.setdefault(root, set()).update(
                    row[0] for row in conn.execute(
                        "SELECT DISTINCT backend FROM agents WHERE session_id=? "
                        "AND source_kind='claude' AND backend IS NOT NULL",
                        (_ns(root),),
                    )
                )
        unit_backends = {root: _unit_backend(backends) for root, backends in unit_backend_sets.items()}
        unresolved_reasons = {
            root: reason for root, backends in unit_backend_sets.items()
            if (reason := _unresolved_reason(group_complete[root], backends))
        }
        unresolved_billing_roots = set(unresolved_reasons)
        unresolved_billing_root_ids = {_ns(root) for root in unresolved_billing_roots}
        unit_root_models = {
            transcript.root_external_id: transcript.root_model
            for transcript in readable_transcripts
            if not transcript.is_subagent and transcript.root_model
        }

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
        root_token_coverage: dict[str, bool] = {}
        # A cumulative snapshot covers only the calls recorded up to its own
        # timestamp.  Calls after the latest snapshot (a resumed session, or
        # a child still running) are outside every snapshot and are priced
        # like calls of a unit without a cost-state.
        coverage_cutoff: dict[str, datetime] = {}
        estimable_roots: set[str] = set()
        # Snapshot rows survive a root that could not be read completely.  The
        # completeness recorded with them decides whether readable subagent
        # calls stay covered or unpriced until the root is readable again.
        stored_cost_states = {
            row[0]: (row[1], bool(row[2])) for row in conn.execute(
                "SELECT s.id,s.cost_state_status,MAX(u.counts_toward_totals) FROM sessions s "
                "JOIN usage u ON u.session_id=s.id AND u.source_event_type=? "
                "WHERE s.cost_state_status IS NOT NULL GROUP BY s.id",
                (_COST_EVENT,),
            )
        }
        stored_cutoffs: dict[str, datetime] = {}
        for session_id, stamp in conn.execute(
            "SELECT session_id,timestamp FROM usage WHERE source_event_type=?", (_COST_EVENT,)
        ):
            instant = _instant(stamp)
            if instant is not None and (session_id not in stored_cutoffs or instant > stored_cutoffs[session_id]):
                stored_cutoffs[session_id] = instant
        # Subagent transcripts whose root file is gone form an orphan unit.
        # Only a complete directory scan proves the root is gone rather than
        # unreadable; the stored snapshots then describe nothing on disk.
        removed_roots = {root for root in readable_roots if root not in root_files and discovery_complete}
        for root in removed_roots:
            conn.execute(
                "DELETE FROM usage WHERE session_id=? AND source_event_type=?", (_ns(root), _COST_EVENT)
            )
            conn.execute("UPDATE sessions SET cost_state_status=NULL WHERE id=?", (_ns(root),))
        for root in readable_roots:
            root_id = _ns(root)
            states = cost_states.get(root, [])
            state = states[-1] if states else None
            # A root that was present but not read completely keeps its stored
            # snapshots, whose coverage replaces whatever part was readable.
            retained = (
                stored_cost_states.get(root_id)
                if root_id not in complete_transcript_ids and root not in removed_roots else None
            )
            if retained is not None:
                stored_status, token_covered = retained
                root_token_coverage[root_id] = token_covered
                if root_id in stored_cutoffs:
                    coverage_cutoff[root_id] = stored_cutoffs[root_id]
                if stored_status == _COST_STATE_COMPLETE:
                    root_cost_coverage[root_id] = True
                    coverage_notes[root_id] = ""
                else:
                    coverage_notes[root_id] = _PARTIAL_COST_STATE_NOTE
                continue
            if state is None:
                coverage_notes[root_id] = CLAUDE_NO_COST_STATE_NOTE
                estimable_roots.add(root_id)
                continue
            cutoff = _instant(state.timestamp)
            if cutoff is not None:
                coverage_cutoff[root_id] = cutoff
            root_token_coverage[root_id] = root_id in complete_transcript_ids and state.has_token_totals
            if state.complete:
                root_cost_coverage[root_id] = root_id in complete_transcript_ids
                reason = unresolved_reasons.get(root)
                coverage_notes[root_id] = (
                    f"Claude Code cost-state {reason}; real spend cannot be separated" if reason else ""
                )
            else:
                coverage_notes[root_id] = _PARTIAL_COST_STATE_NOTE
        priced_by_root: dict[str, list[int]] = {}
        late_by_root: dict[str, list[int]] = {}
        for record in assistants.values():
            cutoff = coverage_cutoff.get(record.session_id)
            recorded = _instant(record.timestamp)
            late = cutoff is not None and recorded is not None and recorded > cutoff
            covered = root_cost_coverage.get(record.session_id, False) and not late
            cost = None
            # Orphaned subagent transcripts have no root and no cost-state, so
            # their rows are estimated like any unit without a cost-state.
            estimable = late or record.session_id in estimable_roots or record.session_id not in coverage_notes
            # Calls no cost-state covers are estimated from Anthropic list
            # prices so totals include every call.  Vertex AI and Bedrock list
            # the same base prices; their regional premiums are not modeled.
            # Calls with unverified Anthropic billing stay unpriced, since they
            # may be subscription usage.
            list_priced = record.backend in CLAUDE_LIST_PRICED_BACKENDS
            if not covered and estimable and list_priced and not record.fast:
                cost = estimate_cost(
                    conn, _token_usage(record), record.model, PROVIDER, record.timestamp,
                    cache_write_1h_tokens=record.cache_write_1h,
                )
            priced = covered or (cost is not None and cost.price_id is not None)
            if not priced:
                if record.fast:
                    missing_price = f"{PROVIDER}:{record.model}:fast"
                else:
                    missing_price = f"{PROVIDER}:{record.model}"
                summary.unknown_prices.add(missing_price)
            counts = priced_by_root.setdefault(record.session_id, [0, 0])
            counts[0] += int(priced and not covered)
            counts[1] += 1
            if late:
                late_counts = late_by_root.setdefault(record.session_id, [0, 0])
                late_counts[0] += int(priced)
                late_counts[1] += 1
            summary.backends[record.backend] = summary.backends.get(record.backend, 0) + 1
            values = _usage_values(
                record, covered_by_cost_state=covered,
                covered_by_token_state=root_token_coverage.get(record.session_id, False) and not late,
                cost=cost, withheld=not covered and not estimable,
            )
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
            if root_id in unresolved_billing_root_ids:
                coverage[root_id] = ("partial", note)
            elif root_cost_coverage.get(root_id):
                late_counts = late_by_root.get(root_id)
                coverage[root_id] = (
                    claude_estimate_status(CLAUDE_LATE_CALLS_NOTE, *late_counts) if late_counts
                    else ("complete", "")
                )
            elif root_id in estimable_roots:
                coverage[root_id] = claude_estimate_status(note, *priced_by_root.get(root_id, [0, 0]))
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
            last = states[-1] if states else None
            conn.execute(
                "UPDATE sessions SET cost_state_status=? WHERE id=?",
                (
                    None if last is None
                    else _COST_STATE_COMPLETE if last.complete else _COST_STATE_PARTIAL,
                    root_id,
                ),
            )
            root_cost_ids = cost_ids_by_root.setdefault(root, set())
            for identity, model, cost, tokens, state in _cost_changes(
                root, states, unit_root_models.get(root, "claude-code-cumulative-total")
            ):
                current_usage_ids.add(identity)
                root_cost_ids.add(identity)
                existed = _upsert_cost(
                    conn, identity=identity, root_id=root_id, model=model,
                    source_path=state.source_path, cost=cost, tokens=tokens,
                    timestamp=state.timestamp, ordinal=state.ordinal,
                    backend=unit_backends.get(root),
                    counts_toward_totals=root_token_coverage.get(root_id, False),
                    unresolved_reason=unresolved_reasons.get(root),
                )
                if existed:
                    summary.duplicate_records += 1
                elif root in unresolved_billing_roots:
                    summary.usage_records += 1
                elif _billing_mode(unit_backends.get(root)) == "subscription":
                    summary.usage_records += 1
                    summary.subscription_value += float(cost)
                else:
                    summary.usage_records += 1
                    summary.recorded_spend += float(cost)
        if scan_complete:
            _reconcile(
                conn, source_home=home, transcript_ids=current_transcript_ids, usage_ids=current_usage_ids,
                keep_files=skipped_files,
            )
            _prune_fingerprints(conn, set(fingerprints), projects)
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
        # Full coverage needs a complete unit. A newly written unresolved
        # snapshot must also mark a previously complete session partial;
        # retained snapshots from unreadable roots keep their stored status.
        _refresh_coverage(conn, {
            _ns(root): coverage[_ns(root)]
            for root in readable_roots if _ns(root) in coverage and (
                group_complete[root] or (root in unresolved_billing_roots and cost_ids_by_root.get(root))
            )
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
        # A unit that was not read completely loses its fingerprints so the
        # next pass rereads it even when nothing changed on disk.  That alone
        # retries a unit that missed a relabel, so the marker always advances
        # and the other units are not reread again.
        conn.executemany(
            "DELETE FROM ingestion_state WHERE source_key=?",
            (
                (_ns(str(item.path)),)
                for root, complete in group_complete.items() if not complete
                for item in groups[root]
            ),
        )
        _write_meta(conn, _META_AUTH_BACKEND, profile.anthropic_backend)
        summary.scanned_sessions = len(transcripts)
        summary.root_sessions = int(conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_app='claude' AND id LIKE 'claude:%'"
        ).fetchone()[0])
        summary.subagent_sessions = int(conn.execute(
            "SELECT COUNT(*) FROM agents WHERE source_kind='claude' AND parent_thread_id IS NOT NULL"
        ).fetchone()[0])
    return summary
