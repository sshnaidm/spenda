from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import spenda.ingestion.claude as claude_module
from spenda.db import database
from spenda.ingestion.claude import discover_claude_home, ingest_claude
from spenda.pricing import add_price, reprice_usage
from spenda.reports import session_detail


@dataclass
class _Settings:
    database: Path
    claude_home: Path
    running_window_seconds: int = 0
    claude_billing: str = "auto"

    def validate(self):
        return self


def _line(*, kind: str, session: str, timestamp: str, **extra) -> str:
    value = {"type": kind, "sessionId": session, "timestamp": timestamp, **extra}
    return json.dumps(value, separators=(",", ":"))


def _assistant(
    *, session: str, message_id: str, timestamp: str, input_tokens: int, output_tokens: int,
    git_branch: str | None = None, effort: str | None = None, content: object | None = None,
    model: str = "claude-test", request_id: str | None = None, cache_read: int = 2,
    cache_write: int = 3, cache_write_1h: int | None = None, speed: str | None = None,
) -> str:
    metadata = {}
    if git_branch is not None:
        metadata["gitBranch"] = git_branch
    if effort is not None:
        metadata["effort"] = effort
    if request_id is not None:
        metadata["requestId"] = request_id
    usage = {
        "input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write, "output_tokens": output_tokens,
        "output_tokens_details": {"thinking_tokens": 3},
    }
    if cache_write_1h is not None:
        usage["cache_creation"] = {
            "ephemeral_1h_input_tokens": cache_write_1h,
            "ephemeral_5m_input_tokens": cache_write - cache_write_1h,
        }
    if speed is not None:
        usage["speed"] = speed
    return _line(
        kind="assistant", session=session, timestamp=timestamp, cwd="/work/repo", version="2.1.263",
        **metadata,
        message={
            "id": message_id, "model": model, "role": "assistant",
            # The text must never be copied into dashboard fields.
            "content": "private assistant response" if content is None else content,
            "usage": usage,
        },
    )


def _user(*, session: str, timestamp: str, uuid: str = "prompt-1", origin: str = "human") -> str:
    return _line(
        kind="user", session=session, timestamp=timestamp, uuid=uuid,
        origin={"kind": origin}, promptSource="typed" if origin == "human" else "system",
        message={"role": "user", "content": "private user prompt"},
    )


def _oauth_home(home: Path) -> None:
    """Give the Claude home a claude.ai subscription login profile."""

    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"billingType": "stripe_subscription", "organizationType": "claude_max"},
    }), encoding="utf-8")


def _write_root(home: Path, session: str, *lines: str) -> Path:
    root = home / "projects" / "-work-repo" / f"{session}.jsonl"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def _usage_row(settings: _Settings, identity: str) -> tuple:
    """Return backend, billing, cost, equivalent value, priced flag, 1h tokens, note."""

    with database(settings.database, readonly=True) as conn:
        row = conn.execute(
            "SELECT backend,billing_mode,cost_usd,equivalent_cost_usd,price_id IS NOT NULL,"
            "cache_write_1h_input_tokens,pricing_note FROM usage WHERE source_record_identity=?",
            (identity,),
        ).fetchone()
    money = tuple(None if value is None else Decimal(value) for value in row[2:4])
    return (row[0], row[1], *money, row[4], row[5], row[6])


def _fixture(home: Path, *, cost_state: str = "complete") -> tuple[Path, Path]:
    root = home / "projects" / "-work-repo" / "root.jsonl"
    child = home / "projects" / "-work-repo" / "root" / "subagents" / "agent-child.jsonl"
    child.parent.mkdir(parents=True)
    root.parent.mkdir(parents=True, exist_ok=True)
    records = [
            _user(session="root", timestamp="2026-09-01T09:59:59Z"),
            _assistant(
                session="root", message_id="message-root", timestamp="2026-09-01T10:00:00Z",
                input_tokens=1, output_tokens=1, git_branch="main", effort="high",
            ),
            # Streaming snapshots reuse the same message ID.  The final row is
            # the only accounting record that should remain.
            _assistant(
                session="root", message_id="message-root", timestamp="2026-09-01T10:00:01Z",
                input_tokens=4, output_tokens=5, git_branch="main", effort="high",
            ),
            _line(kind="ai-title", session="root", timestamp="2026-09-01T10:00:01Z", aiTitle="Generated safe title"),
    ]
    if cost_state != "missing":
        records.append(
            _line(
                kind="cost-state", session="root", timestamp="2026-09-01T10:00:02Z",
                startTime=1788256802000, totalCostUSD=1.5,
                modelUsage={"claude-test": {
                    "costUSD": 1.5, "inputTokens": 10, "cacheReadInputTokens": 4,
                    "cacheCreationInputTokens": 6, "outputTokens": 12, "thinkingTokens": 6,
                }},
                hasUnknownModelCost=cost_state == "unknown",
            )
        )
    root.write_text("\n".join(records) + "\n", encoding="utf-8")
    child.write_text(
        # Claude copies shared history into subagent transcripts. This is the
        # same API response as the root's final streaming snapshot.
        _assistant(
            session="root", message_id="message-root", timestamp="2026-09-01T10:00:01Z",
            input_tokens=4, output_tokens=5,
        ) + "\n" + _assistant(
            session="root", message_id="message-child", timestamp="2026-09-01T10:00:03Z",
            input_tokens=6, output_tokens=7, effort="low",
        ) + "\n",
        encoding="utf-8",
    )
    return root, child


def _touch(path: Path) -> None:
    """Advance mtime deterministically, independent of filesystem granularity."""

    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def _forbid_open(monkeypatch, *paths: Path) -> None:
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path in paths:
            raise AssertionError(f"unchanged transcript was reopened: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(claude_module.Path, "open", guarded_open)


def test_claude_ingestion_keeps_only_accounting_and_latest_message(tmp_path):
    home = tmp_path / "claude"
    root, child = _fixture(home)
    source_bytes = {root: root.read_bytes(), child: child.read_bytes()}
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    assert discover_claude_home(settings) == home.resolve()
    assert (summary.root_sessions, summary.subagent_sessions, summary.usage_records) == (1, 1, 3)
    assert summary.recorded_spend == pytest.approx(1.5)
    assert summary.estimated_spend == 0
    # Every call is covered by the root cost-state, so no price is needed.
    assert summary.unknown_prices == set()
    with database(settings.database, readonly=True) as conn:
        paths = dict(conn.execute("SELECT thread_id,agent_path FROM agents"))
        usage = conn.execute(
            "SELECT input_tokens,cached_input_tokens,cache_write_input_tokens,uncached_input_tokens,"
            "output_tokens,reasoning_output_tokens,total_tokens,cost_usd FROM usage "
            "WHERE source_record_identity='claude:root:root:message-root'"
        ).fetchone()
        cost = conn.execute(
            "SELECT cost_usd,total_tokens,timestamp,source_file FROM usage WHERE source_event_type='claude_cost_state'"
        ).fetchone()
        session = conn.execute(
            "SELECT turn_count,first_user_message_preview,source_app,source_version,git_branch,root_reasoning_effort "
            "FROM sessions"
        ).fetchone()
        detail = session_detail(conn, "claude:root")
        cost_label = conn.execute(
            "SELECT turn_id,response_id,call_label FROM usage WHERE source_event_type='claude_cost_state'"
        ).fetchone()
        counted = conn.execute(
            "SELECT source_event_type,counts_toward_totals FROM usage ORDER BY source_event_type"
        ).fetchall()

    assert paths == {
        "claude:root": "/root",
        "claude:root:agent:agent-child": "/root/agent-child",
    }
    assert tuple(usage) == (9, 2, 3, 4, 5, 3, 14, "0")
    assert tuple(cost) == ("1.5", 32, "2026-09-01T10:00:02Z", str(home / "projects" / "-work-repo" / "root.jsonl"))
    assert tuple(session) == (1, None, "claude", "2.1.263", "main", "high")

    with database(settings.database, readonly=True) as conn:
        efforts = dict(conn.execute("SELECT thread_id,reasoning_effort FROM agents"))
    assert efforts == {"claude:root": "high", "claude:root:agent:agent-child": "low"}
    assert detail["usage_events"] == 2
    assert (
        detail["input_tokens"], detail["cached_input_tokens"], detail["cache_write_input_tokens"],
        detail["uncached_input_tokens"], detail["output_tokens"], detail["reasoning_tokens"],
        detail["total_tokens"],
    ) == (20, 4, 6, 10, 12, 6, 32)
    assert [tuple(row) for row in counted] == [
        ("claude_assistant_message", 0),
        ("claude_assistant_message", 0),
        ("claude_cost_state", 1),
    ]
    assert tuple(cost_label) == (None, None, "Cumulative cost state")
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes
    with sqlite3.connect(settings.database) as conn:
        logical_dump = "\n".join(conn.iterdump())
    assert "private assistant response" not in logical_dump

    with database(settings.database) as conn:
        before = conn.execute(
            "SELECT source_event_type,cost_usd,pricing_note "
            "FROM usage WHERE source_event_type GLOB 'claude_*' ORDER BY id"
        ).fetchall()
        assert reprice_usage(conn, provider="anthropic") == 0
        after = conn.execute(
            "SELECT source_event_type,cost_usd,pricing_note "
            "FROM usage WHERE source_event_type GLOB 'claude_*' ORDER BY id"
        ).fetchall()
    assert before == after

    second = ingest_claude(settings)
    assert (second.usage_records, second.duplicate_records, second.unchanged_files) == (0, 0, 2)
    forced = ingest_claude(settings, force_all=True)
    assert (forced.usage_records, forced.duplicate_records, forced.unchanged_files) == (0, 3, 0)


def test_claude_derives_fixed_action_labels_without_persisting_content(tmp_path):
    home = tmp_path / "claude"
    root = home / "projects" / "-work-repo" / "root.jsonl"
    root.parent.mkdir(parents=True)
    tool_snapshot = _assistant(
        session="root", message_id="tool-message", timestamp="2026-09-01T10:00:00Z",
        input_tokens=1, output_tokens=1,
        content=[
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "tool_use", "name": "Edit", "input": {
                "file_path": "/private/file", "new_string": "private replacement"
            }},
        ],
    )
    # Streaming may replace the content blocks while retaining the message ID;
    # keep the strongest safe label while using the latest accounting values.
    final_snapshot = _assistant(
        session="root", message_id="tool-message", timestamp="2026-09-01T10:00:01Z",
        input_tokens=2, output_tokens=2,
        content=[{"type": "text", "text": "private final response"}],
    )
    text_message = _assistant(
        session="root", message_id="text-message", timestamp="2026-09-01T10:00:02Z",
        input_tokens=3, output_tokens=3,
        content=[{"type": "text", "text": "another private response"}],
    )
    root.write_text("\n".join((tool_snapshot, final_snapshot, text_message)) + "\n", encoding="utf-8")
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        labels = dict(conn.execute(
            "SELECT source_record_identity,call_label FROM usage "
            "WHERE source_event_type='claude_assistant_message' ORDER BY source_record_identity"
        ))
        latest_tokens = conn.execute(
            "SELECT input_tokens,output_tokens FROM usage "
            "WHERE source_record_identity='claude:root:root:tool-message'"
        ).fetchone()
    assert labels == {
        "claude:root:root:text-message": "Assistant response",
        "claude:root:root:tool-message": "Apply file change",
    }
    assert tuple(latest_tokens) == (7, 2)
    with sqlite3.connect(settings.database) as conn:
        logical_dump = "\n".join(conn.iterdump())
    for private_value in (
        "private reasoning", "/private/file", "private replacement",
        "private final response", "another private response",
    ):
        assert private_value not in logical_dump


def test_claude_counts_only_structured_human_prompts(tmp_path):
    home = tmp_path / "claude"
    _write_root(
        home, "root",
        _user(session="root", timestamp="2026-09-01T10:00:00Z", uuid="prompt-1"),
        _user(session="root", timestamp="2026-09-01T10:00:01Z", uuid="prompt-2"),
        _user(
            session="root", timestamp="2026-09-01T10:00:02Z",
            uuid="notification", origin="task-notification",
        ),
        _assistant(
            session="root", message_id="message-root", timestamp="2026-09-01T10:00:03Z",
            input_tokens=1, output_tokens=1,
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        row = conn.execute(
            "SELECT turn_count,first_user_message_preview FROM sessions WHERE id='claude:root'"
        ).fetchone()
        dump = "\n".join(conn.iterdump())
    assert tuple(row) == (2, None)
    assert "private user prompt" not in dump


def test_claude_skips_non_utf8_line_and_imports_surrounding_records(tmp_path):
    home = tmp_path / "claude"
    root = home / "projects" / "-work-repo" / "root.jsonl"
    root.parent.mkdir(parents=True)
    first = _assistant(
        session="root", message_id="first", timestamp="2026-09-01T10:00:00Z",
        input_tokens=1, output_tokens=1,
    ).encode()
    second = _assistant(
        session="root", message_id="second", timestamp="2026-09-01T10:00:01Z",
        input_tokens=2, output_tokens=2,
    ).encode()
    root.write_bytes(first + b"\n\xff\n" + second + b"\n")
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    assert summary.malformed_lines == 1
    with database(settings.database, readonly=True) as conn:
        identities = {
            row[0] for row in conn.execute(
                "SELECT source_record_identity FROM usage "
                "WHERE source_event_type='claude_assistant_message'"
            )
        }
    assert identities == {"claude:root:root:first", "claude:root:root:second"}


@pytest.mark.parametrize(
    ("cost_state", "note"),
    (
        ("missing", "no cumulative cost-state"),
        ("unknown", "unknown model cost"),
    ),
)
def test_claude_marks_missing_or_unknown_cost_state_partial(tmp_path, cost_state, note):
    home = tmp_path / "claude"
    _fixture(home, cost_state=cost_state)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        calls = conn.execute(
            "SELECT cost_usd FROM usage WHERE source_event_type='claude_assistant_message' ORDER BY id"
        ).fetchall()
        session = conn.execute("SELECT accounting_status,accounting_note,title FROM sessions").fetchone()
    assert [row[0] for row in calls] == [None, None]
    assert session[0] == "partial" and note in session[1]
    assert session[2] == "Generated safe title"
    assert summary.unknown_prices == {"anthropic:claude-test"}


def test_claude_reconciles_removed_subagent_and_preserves_codex(tmp_path):
    home = tmp_path / "claude"
    _, child = _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)
    with database(settings.database) as conn:
        conn.execute(
            "INSERT INTO sessions(id,root_thread_id,source_app,source_home) VALUES(?,?,?,?)",
            ("codex:kept", "codex:kept", "codex", "/tmp/codex"),
        )
        conn.execute(
            "INSERT INTO agents(thread_id,session_id,agent_role,source_kind) VALUES(?,?,?,?)",
            ("codex:kept", "codex:kept", "root", "cli"),
        )

    child.unlink()
    summary = ingest_claude(settings)

    assert (summary.root_sessions, summary.subagent_sessions) == (1, 0)
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM agents WHERE source_kind='claude'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_event_type='claude_assistant_message'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT turn_count FROM sessions WHERE id='claude:root'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='codex:kept'").fetchone()[0] == 1


def test_complete_root_cost_state_covers_subagent_usage(tmp_path):
    """Claude Code's cumulative cost-state already includes subagent calls."""

    home = tmp_path / "claude"
    _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        costs = dict(conn.execute(
            "SELECT thread_id,cost_usd FROM usage WHERE source_event_type='claude_assistant_message'"
        ))
        notes = {row[0] for row in conn.execute(
            "SELECT pricing_note FROM usage WHERE source_event_type='claude_assistant_message'"
        )}
        session = conn.execute(
            "SELECT accounting_status,accounting_note FROM sessions WHERE id='claude:root'"
        ).fetchone()
    assert costs == {"claude:root": "0", "claude:root:agent:agent-child": "0"}
    assert notes == {"cost represented by cumulative Claude Code cost-state"}
    assert tuple(session) == ("complete", None)
    assert summary.unknown_prices == set()


def test_missing_projects_preserves_claude_rows_but_readable_empty_projects_reconcile(tmp_path):
    home = tmp_path / "claude"
    _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)

    projects = home / "projects"
    projects.rename(home / "projects-unavailable")
    unavailable = ingest_claude(settings)
    assert unavailable.parser_warnings == 1
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE source_app='claude'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity LIKE 'claude:%'"
        ).fetchone()[0] == 3

    projects.mkdir()
    empty = ingest_claude(settings)
    assert empty.parser_warnings == 0
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE source_app='claude'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity LIKE 'claude:%'"
        ).fetchone()[0] == 0


def test_read_failure_skips_reconciliation_and_keeps_existing_claude_rows(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    root, _ = _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)
    with database(settings.database, readonly=True) as conn:
        before_session = conn.execute(
            "SELECT title,cwd,created_at,updated_at,status,accounting_status,accounting_note "
            "FROM sessions WHERE id='claude:root'"
        ).fetchone()
        before_agent = conn.execute(
            "SELECT agent_path,created_at,updated_at,model,reasoning_effort "
            "FROM agents WHERE thread_id='claude:root'"
        ).fetchone()

    original_open = Path.open

    def failed_open(path, *args, **kwargs):
        if path == root:
            raise OSError("simulated transcript read failure")
        return original_open(path, *args, **kwargs)

    # An unchanged transcript is never opened, so mark it as modified first.
    _touch(root)
    monkeypatch.setattr(claude_module.Path, "open", failed_open)
    summary = ingest_claude(settings)
    assert summary.parser_warnings == 1
    with database(settings.database, readonly=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity LIKE 'claude:%'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity='claude:root:root:message-root'"
        ).fetchone()[0] == 1
        after_session = conn.execute(
            "SELECT title,cwd,created_at,updated_at,status,accounting_status,accounting_note "
            "FROM sessions WHERE id='claude:root'"
        ).fetchone()
        after_agent = conn.execute(
            "SELECT agent_path,created_at,updated_at,model,reasoning_effort "
            "FROM agents WHERE thread_id='claude:root'"
        ).fetchone()
    assert after_session == before_session
    assert after_agent == before_agent


def test_initial_partial_scan_cannot_mark_root_cost_coverage_complete(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    _, child = _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    original_open = Path.open

    def failed_open(path, *args, **kwargs):
        if path == child:
            raise OSError("simulated child read failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(claude_module.Path, "open", failed_open)
    summary = ingest_claude(settings)

    assert summary.parser_warnings == 1
    with database(settings.database, readonly=True) as conn:
        session = conn.execute(
            "SELECT accounting_status,accounting_note FROM sessions WHERE id='claude:root'"
        ).fetchone()
    assert tuple(session) == (
        "partial", "Claude Code scan has not established complete cost coverage"
    )


def test_claude_liveness_uses_latest_subagent_timestamp(tmp_path):
    home = tmp_path / "claude"
    _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home, running_window_seconds=5)

    ingest_claude(settings, now=datetime(2026, 9, 1, 10, 0, 5, tzinfo=UTC))
    with database(settings.database, readonly=True) as conn:
        running = conn.execute(
            "SELECT status,updated_at,finished_at FROM sessions WHERE id='claude:root'"
        ).fetchone()
    assert tuple(running) == ("running", "2026-09-01T10:00:03Z", None)

    ingest_claude(settings, now=datetime(2026, 9, 1, 10, 0, 9, tzinfo=UTC))
    with database(settings.database, readonly=True) as conn:
        completed = conn.execute(
            "SELECT status,updated_at,finished_at FROM sessions WHERE id='claude:root'"
        ).fetchone()
    assert tuple(completed) == (
        "completed", "2026-09-01T10:00:03Z", "2026-09-01T10:00:03Z"
    )


def test_claude_cost_state_replaces_removed_models_and_preserves_exact_total(tmp_path):
    home = tmp_path / "claude"
    root = home / "projects" / "-work-repo" / "root.jsonl"
    root.parent.mkdir(parents=True)
    assistant = _assistant(
        session="root", message_id="message-root", timestamp="2026-09-01T10:00:00Z",
        input_tokens=1, output_tokens=1,
    )
    initial = _line(
        kind="cost-state", session="root", timestamp="2026-09-01T10:00:01Z",
        totalCostUSD=3, modelUsage={"claude-a": {"costUSD": 1}, "claude-b": {"costUSD": 2}},
        hasUnknownModelCost=False,
    )
    root.write_text(f"{assistant}\n{initial}\n", encoding="utf-8")
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)

    replacement = _line(
        kind="cost-state", session="root", timestamp="2026-09-01T10:00:02Z",
        totalCostUSD=4, modelUsage={"claude-a": {"costUSD": 4}}, hasUnknownModelCost=False,
    )
    root.write_text(f"{assistant}\n{replacement}\n", encoding="utf-8")
    ingest_claude(settings)
    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT model,cost_usd FROM usage WHERE source_event_type='claude_cost_state' ORDER BY model"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("claude-a", "4")]

    inconsistent = _line(
        kind="cost-state", session="root", timestamp="2026-09-01T10:00:03Z",
        totalCostUSD=1, modelUsage={"claude-a": {"costUSD": 0.7}, "claude-b": {"costUSD": 0.7}},
        hasUnknownModelCost=False,
    )
    root.write_text(f"{assistant}\n{inconsistent}\n", encoding="utf-8")
    ingest_claude(settings)
    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT model,cost_usd FROM usage WHERE source_event_type='claude_cost_state' ORDER BY model"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("claude-code-cumulative-total", "1")]


def test_claude_cost_snapshots_preserve_historical_daily_changes(tmp_path):
    home = tmp_path / "claude"
    root = home / "projects" / "-work-repo" / "root.jsonl"
    root.parent.mkdir(parents=True)
    snapshots = (
        _line(
            kind="cost-state", session="root", timestamp="2026-09-01T10:00:00Z",
            totalCostUSD=10, modelUsage={"claude-test": {"costUSD": 10}},
            hasUnknownModelCost=False,
        ),
        _line(
            kind="cost-state", session="root", timestamp="2026-09-02T10:00:00Z",
            totalCostUSD=15, modelUsage={"claude-test": {"costUSD": 15}},
            hasUnknownModelCost=False,
        ),
    )
    root.write_text("\n".join(snapshots) + "\n", encoding="utf-8")
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        daily = conn.execute(
            "SELECT date(timestamp),SUM(CAST(cost_usd AS REAL)) FROM usage "
            "WHERE source_event_type='claude_cost_state' GROUP BY date(timestamp) ORDER BY 1"
        ).fetchall()
    assert [tuple(row) for row in daily] == [
        ("2026-09-01", 10.0),
        ("2026-09-02", 5.0),
    ]


def test_bedrock_cost_state_and_message_models_share_one_canonical_id(tmp_path):
    home = tmp_path / "claude"
    _write_root(
        home, "root",
        _assistant(
            session="root", message_id="msg_bdrk_01", timestamp="2026-09-01T10:00:00Z",
            model="claude-opus-4-6", input_tokens=1, output_tokens=1,
        ),
        _line(
            kind="cost-state", session="root", timestamp="2026-09-01T10:00:01Z",
            totalCostUSD=2,
            modelUsage={"us.anthropic.claude-opus-4-6-v1": {
                "costUSD": 2, "inputTokens": 10, "cacheReadInputTokens": 4,
                "cacheCreationInputTokens": 6, "outputTokens": 12, "thinkingTokens": 3,
            }},
            hasUnknownModelCost=False,
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT model,COUNT(*),SUM(counts_toward_totals*total_tokens),"
            "SUM(CAST(cost_usd AS REAL)) FROM usage GROUP BY model"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("claude-opus-4-6", 2, 32, 2.0)]
    assert summary.recorded_spend == pytest.approx(2)
    assert summary.estimated_spend == 0


def test_zero_cost_state_is_retained_as_authoritative_evidence(tmp_path):
    home = tmp_path / "claude"
    _write_root(
        home, "root",
        _assistant(
            session="root", message_id="msg_bdrk_01", timestamp="2026-09-01T10:00:00Z",
            model="claude-opus-4-6", input_tokens=1, output_tokens=1,
        ),
        _assistant(
            session="root", message_id="msg_vrtx_01", timestamp="2026-09-01T10:00:01Z",
            model="claude-opus-4-6", input_tokens=2, output_tokens=1,
        ),
        _line(
            kind="cost-state", session="root", timestamp="2026-09-01T10:00:02Z",
            totalCostUSD=0, modelUsage={}, hasUnknownModelCost=False,
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        state = conn.execute(
            "SELECT model,backend,cost_usd,counts_toward_totals FROM usage "
            "WHERE source_event_type='claude_cost_state'"
        ).fetchone()
        session = conn.execute(
            "SELECT root_backend,accounting_status FROM sessions WHERE id='claude:root'"
        ).fetchone()
    assert tuple(state) == ("claude-opus-4-6", "mixed", "0", 0)
    assert tuple(session) == ("mixed", "complete")
    assert summary.recorded_spend == 0
    assert summary.estimated_spend == 0


def test_complete_root_reconciles_costs_when_another_transcript_is_malformed(tmp_path):
    home = tmp_path / "claude"
    root, child = _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)

    replacement = _line(
        kind="cost-state", session="root", timestamp="2026-09-02T10:00:00Z",
        totalCostUSD=4, modelUsage={"claude-new": {"costUSD": 4}},
        hasUnknownModelCost=False,
    )
    root.write_text(replacement + "\n", encoding="utf-8")
    child.write_text("{malformed\n", encoding="utf-8")

    ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        costs = conn.execute(
            "SELECT model,cost_usd FROM usage WHERE source_event_type='claude_cost_state'"
        ).fetchall()
    assert [tuple(row) for row in costs] == [("claude-new", "4")]


def test_malformed_line_keeps_valid_assistant_records(tmp_path):
    home = tmp_path / "claude"
    root = home / "projects" / "-work-repo" / "root.jsonl"
    root.parent.mkdir(parents=True)
    root.write_text(
        _assistant(
            session="root", message_id="first", timestamp="2026-09-01T10:00:00Z",
            input_tokens=1, output_tokens=1,
        )
        + "\n{malformed\n"
        + _assistant(
            session="root", message_id="second", timestamp="2026-09-01T10:01:00Z",
            input_tokens=2, output_tokens=2,
        )
        + "\n",
        encoding="utf-8",
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        calls = conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_event_type='claude_assistant_message'"
        ).fetchone()[0]
    assert summary.malformed_lines == 1
    assert calls == 2


def test_unreadable_project_does_not_erase_claude_history(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    root, _ = _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)
    original_scandir = claude_module.os.scandir

    def unreadable_project(path):
        if Path(path) == root.parent:
            raise PermissionError("simulated unreadable project")
        return original_scandir(path)

    monkeypatch.setattr(claude_module.os, "scandir", unreadable_project)
    summary = ingest_claude(settings)

    with database(settings.database, readonly=True) as conn:
        sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source_app='claude'"
        ).fetchone()[0]
    assert summary.parser_warnings == 1
    assert sessions == 1


def test_unchanged_transcripts_are_skipped_until_a_file_changes(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    root, child = _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    first = ingest_claude(settings)
    assert (first.unchanged_files, first.usage_records) == (0, 3)

    _forbid_open(monkeypatch, root, child)
    second = ingest_claude(settings)
    assert (second.scanned_files, second.unchanged_files) == (2, 2)
    assert (second.usage_records, second.duplicate_records) == (0, 0)
    with database(settings.database, readonly=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity LIKE 'claude:%'"
        ).fetchone()[0] == 3
        assert conn.execute("SELECT accounting_status FROM sessions WHERE id='claude:root'").fetchone()[0] == "complete"

    # A change to any transcript in the unit rereads the whole unit.
    monkeypatch.undo()
    with child.open("a", encoding="utf-8") as handle:
        handle.write(_assistant(
            session="root", message_id="message-child-2", timestamp="2026-09-01T10:00:04Z",
            input_tokens=8, output_tokens=9,
        ) + "\n")
    third = ingest_claude(settings)
    assert (third.unchanged_files, third.usage_records, third.duplicate_records) == (0, 1, 3)
    with database(settings.database, readonly=True) as conn:
        assert conn.execute(
                "SELECT COUNT(*) FROM usage WHERE source_record_identity='claude:root:root:message-child-2'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT updated_at FROM sessions WHERE id='claude:root'"
        ).fetchone()[0] == "2026-09-01T10:00:04Z"

    # A same-size rewrite still changes mtime, and force_all ignores fingerprints.
    _touch(root)
    assert ingest_claude(settings).unchanged_files == 0
    assert ingest_claude(settings).unchanged_files == 2
    assert ingest_claude(settings, force_all=True).unchanged_files == 0


def test_reconciliation_leaves_skipped_unit_alone_while_changed_unit_is_replaced(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    root, child = _fixture(home)
    other = home / "projects" / "-work-repo" / "other.jsonl"
    other.write_text(
        _assistant(
            session="other", message_id="message-other", timestamp="2026-09-02T10:00:00Z",
            input_tokens=1, output_tokens=1,
        ) + "\n",
        encoding="utf-8",
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)

    other.write_text(
        _assistant(
            session="other", message_id="message-replaced", timestamp="2026-09-02T10:00:01Z",
            input_tokens=1, output_tokens=1,
        ) + "\n",
        encoding="utf-8",
    )
    _touch(other)
    _forbid_open(monkeypatch, root, child)
    summary = ingest_claude(settings)

    assert summary.unchanged_files == 2
    with database(settings.database, readonly=True) as conn:
        identities = sorted(row[0] for row in conn.execute(
            "SELECT source_record_identity FROM usage WHERE source_record_identity LIKE 'claude:%'"
        ))
    assert identities == [
        "claude:cost:root:4:claude-test",
        "claude:other:root:message-replaced",
        "claude:root:root:message-child",
        "claude:root:root:message-root",
    ]


def test_partial_unit_is_not_fingerprinted_and_is_reread(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    root, child = _fixture(home)
    child.write_text("{malformed\n", encoding="utf-8")
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    assert ingest_claude(settings).malformed_lines == 1
    assert ingest_claude(settings).unchanged_files == 0

    child.write_text(
        _assistant(
            session="root", message_id="message-child", timestamp="2026-09-01T10:00:03Z",
            input_tokens=6, output_tokens=7,
        ) + "\n",
        encoding="utf-8",
    )
    assert ingest_claude(settings).malformed_lines == 0
    assert ingest_claude(settings).unchanged_files == 2


def test_removed_transcript_fingerprints_are_pruned_after_complete_scan(tmp_path):
    home = tmp_path / "claude"
    root, child = _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)
    child.unlink()
    child.parent.rmdir()
    child.parent.parent.rmdir()

    # The root file is untouched, but the unit lost a member and is reread so
    # its coverage no longer cites subagent usage.
    summary = ingest_claude(settings)

    assert summary.unchanged_files == 0
    with database(settings.database, readonly=True) as conn:
        stored = [row[0] for row in conn.execute(
            "SELECT source_path FROM ingestion_state WHERE source_key LIKE 'claude:%' ORDER BY 1"
        )]
        assert conn.execute("SELECT COUNT(*) FROM agents WHERE source_kind='claude'").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity LIKE 'claude:%'"
        ).fetchone()[0] == 2
        assert conn.execute("SELECT accounting_status FROM sessions WHERE id='claude:root'").fetchone()[0] == "complete"
    assert stored == [str(root)]
    assert ingest_claude(settings).unchanged_files == 1


def test_unchanged_history_pass_opens_no_transcript_files(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    for index in range(20):
        session = f"session-{index}"
        (project / f"{session}.jsonl").write_text(
            _assistant(
                session=session, message_id="m", timestamp="2026-09-01T10:00:00Z",
                input_tokens=1, output_tokens=1,
            ) + "\n",
            encoding="utf-8",
        )
        child = project / session / "subagents" / f"agent-{index}.jsonl"
        child.parent.mkdir(parents=True)
        child.write_text(
            _assistant(
                session=session, message_id="c", timestamp="2026-09-01T10:00:01Z",
                input_tokens=1, output_tokens=1,
            ) + "\n",
            encoding="utf-8",
        )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    opened: list[Path] = []
    original_open = Path.open

    def counting_open(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(claude_module.Path, "open", counting_open)

    first = ingest_claude(settings)
    assert (first.scanned_files, first.unchanged_files, len(opened)) == (40, 0, 40)

    opened.clear()
    second = ingest_claude(settings)
    assert (second.scanned_files, second.unchanged_files, len(opened)) == (40, 40, 0)
    with database(settings.database, readonly=True) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM usage WHERE source_record_identity LIKE 'claude:%'"
        ).fetchone()[0] == 40
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE source_app='claude'").fetchone()[0] == 20

    # Touching one root rereads exactly that unit: its root and its subagent.
    opened.clear()
    _touch(project / "session-7.jsonl")
    third = ingest_claude(settings)
    assert (third.unchanged_files, sorted(p.name for p in opened)) == (38, ["agent-7.jsonl", "session-7.jsonl"])


def test_vertex_unit_without_cost_state_stays_unpriced_in_strict_mode(tmp_path):
    home = tmp_path / "claude"
    _write_root(
        home, "vertex-root",
        _assistant(
            session="vertex-root", message_id="msg_vrtx_01", request_id="req_vrtx_01",
            timestamp="2026-09-01T10:00:00Z", model="claude-opus-4-8", input_tokens=1000,
            output_tokens=500, cache_read=2000, cache_write=3000, cache_write_1h=1000,
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    backend, billing, cost, equivalent, priced, write_1h, note = _usage_row(
        settings, "claude:vertex-root:root:msg_vrtx_01"
    )
    assert (backend, billing, cost, equivalent, priced, write_1h) == (
        "vertex", "metered", None, None, 0, 1000,
    )
    assert note.startswith("strict accounting: vertex backend/region price")
    with database(settings.database, readonly=True) as conn:
        session = conn.execute(
            "SELECT root_backend,accounting_status,accounting_note FROM sessions WHERE id='claude:vertex-root'"
        ).fetchone()
        agent = conn.execute("SELECT backend FROM agents WHERE thread_id='claude:vertex-root'").fetchone()
    assert tuple(session) == (
        "vertex", "partial",
        "Claude Code transcript has no cumulative cost-state",
    )
    assert agent[0] == "vertex"
    assert summary.unknown_prices == {
        "vertex:claude-opus-4-8:backend-price-unavailable"
    }
    assert summary.backends == {"vertex": 1}
    assert summary.estimated_records == 0 and summary.estimated_spend == 0
    assert summary.subscription_value == 0


def test_subscription_unit_keeps_equivalent_value_with_zero_real_cost(tmp_path):
    home = tmp_path / "claude"
    _oauth_home(home)
    _write_root(
        home, "oauth-root",
        _assistant(
            session="oauth-root", message_id="msg_01", request_id="req_01",
            timestamp="2026-09-01T10:00:00Z", model="claude-opus-5", input_tokens=1_000_000,
            output_tokens=0, cache_read=0, cache_write=0,
        ),
        _line(
            kind="cost-state", session="oauth-root", timestamp="2026-09-01T10:00:02Z",
            totalCostUSD=7.5, modelUsage={"claude-opus-5[1m]": {"costUSD": 7.5}},
            hasUnknownModelCost=False,
        ),
    )
    _write_root(
        home, "oauth-open",
        _assistant(
            session="oauth-open", message_id="msg_02", timestamp="2026-09-02T10:00:00Z",
            model="claude-opus-5", input_tokens=1_000_000, output_tokens=0, cache_read=0, cache_write=0,
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    covered = _usage_row(settings, "claude:oauth-root:root:msg_01")
    assert covered[:5] == ("anthropic-oauth", "subscription", Decimal("0"), Decimal("0"), 0)
    estimated = _usage_row(settings, "claude:oauth-open:root:msg_02")
    assert estimated[:5] == ("anthropic-oauth", "subscription", Decimal("0"), Decimal("5"), 1)
    assert estimated[6].startswith("subscription usage: real cost $0")
    with database(settings.database, readonly=True) as conn:
        cost_rows = conn.execute(
            "SELECT model,backend,billing_mode,cost_usd,equivalent_cost_usd,pricing_note FROM usage "
            "WHERE source_event_type='claude_cost_state'"
        ).fetchall()
        statuses = dict(conn.execute("SELECT id,accounting_status FROM sessions"))
        totals = conn.execute(
            "SELECT SUM(CAST(cost_usd AS REAL)),SUM(CAST(equivalent_cost_usd AS REAL)) FROM usage"
        ).fetchone()
    assert [tuple(row) for row in cost_rows] == [(
        "claude-opus-5", "anthropic-oauth", "subscription", "0", "7.5",
        "Change between Claude Code cumulative cost-state snapshots (subscription equivalent value)",
    )]
    assert statuses == {"claude:oauth-root": "complete", "claude:oauth-open": "estimated"}
    assert tuple(totals) == (0.0, 12.5)
    assert summary.subscription_value == pytest.approx(12.5) and summary.estimated_spend == 0
    assert summary.recorded_spend == 0


def test_billing_override_and_profile_change_relabel_unchanged_units(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    _write_root(
        home, "root",
        _assistant(
            session="root", message_id="msg_01", timestamp="2026-09-01T10:00:00Z",
            model="claude-opus-5", input_tokens=1_000_000, output_tokens=0, cache_read=0, cache_write=0,
        ),
    )
    vertex = _write_root(
        home, "vertex-root",
        _assistant(
            session="vertex-root", message_id="msg_vrtx_01", timestamp="2026-09-01T10:00:00Z",
            model="claude-opus-5", input_tokens=1, output_tokens=1,
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    ingest_claude(settings)
    assert _usage_row(settings, "claude:root:root:msg_01")[:4] == (
        "anthropic", "metered", None, None,
    )

    # Logging in changes how msg_ calls are billed; the unchanged transcript
    # with Anthropic-direct rows must be reread so its rows are relabeled,
    # while the Vertex unit is unaffected and is not reopened.
    _oauth_home(home)
    _forbid_open(monkeypatch, vertex)
    summary = ingest_claude(settings)
    assert summary.unchanged_files == 1
    monkeypatch.undo()
    assert _usage_row(settings, "claude:root:root:msg_01")[:4] == (
        "anthropic-oauth", "subscription", Decimal("0"), Decimal("5"),
    )
    assert ingest_claude(settings).unchanged_files == 2

    forced = _Settings(tmp_path / "dashboard.sqlite", home)
    forced.claude_billing = "api"
    ingest_claude(forced)
    assert _usage_row(settings, "claude:root:root:msg_01")[:4] == ("anthropic-api", "metered", Decimal("5"), None)


def test_mixed_backend_unit_and_fast_mode_rows(tmp_path):
    home = tmp_path / "claude"
    root = _write_root(
        home, "root",
        _assistant(
            session="root", message_id="msg_vrtx_01", timestamp="2026-09-01T10:00:00Z",
            model="claude-opus-5", input_tokens=1, output_tokens=1,
        ),
        _assistant(
            session="root", message_id="msg_bdrk_02", timestamp="2026-09-01T10:00:01Z",
            model="claude-opus-5", input_tokens=1, output_tokens=1, speed="fast",
        ),
        _assistant(
            session="root", message_id="msg_03", timestamp="2026-09-01T10:00:02Z",
            model="claude-future", input_tokens=1, output_tokens=1,
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)

    summary = ingest_claude(settings)

    assert _usage_row(settings, "claude:root:root:msg_vrtx_01")[:2] == ("vertex", "metered")
    fast = _usage_row(settings, "claude:root:root:msg_bdrk_02")
    assert fast[:5] == ("bedrock", "metered", None, None, 0)
    assert fast[6] == "fast-mode request; standard list price not applicable"
    unknown = _usage_row(settings, "claude:root:root:msg_03")
    assert unknown[:5] == ("anthropic", "metered", None, None, 0)
    assert unknown[6].startswith("no built-in Anthropic price for claude-future")
    assert summary.unknown_prices == {
        "anthropic:claude-opus-5:fast", "anthropic:claude-future",
        "vertex:claude-opus-5:backend-price-unavailable",
    }
    with database(settings.database, readonly=True) as conn:
        session = conn.execute(
            "SELECT root_backend,accounting_status,accounting_note FROM sessions WHERE id='claude:root'"
        ).fetchone()
    assert tuple(session) == (
        "mixed", "partial",
        "Claude Code transcript has no cumulative cost-state",
    )

    cost_state = _line(
        kind="cost-state", session="root", timestamp="2026-09-01T10:00:03Z",
        totalCostUSD=2, modelUsage={"claude-opus-5": {"costUSD": 2}}, hasUnknownModelCost=False,
    )
    with root.open("a", encoding="utf-8") as handle:
        handle.write(cost_state + "\n")
    ingest_claude(settings)
    with database(settings.database, readonly=True) as conn:
        cost_row = conn.execute(
            "SELECT backend,billing_mode,cost_usd,pricing_note FROM usage WHERE source_event_type='claude_cost_state'"
        ).fetchone()
        calls = {row[0]: row[1] for row in conn.execute(
            "SELECT source_record_identity,cost_usd FROM usage WHERE source_event_type='claude_assistant_message'"
        )}
        status = conn.execute("SELECT accounting_status FROM sessions WHERE id='claude:root'").fetchone()[0]
    assert tuple(cost_row) == (
        "mixed", "metered", "2",
        "Change between Claude Code cumulative cost-state snapshots (mixed backends; cost-state cannot be split)",
    )
    # Once the cost-state exists every call, including the fast and unknown
    # ones, is covered by it instead of being estimated.
    assert set(calls.values()) == {"0"} and status == "complete"


def test_reprice_touches_only_estimated_claude_rows(tmp_path):
    home = tmp_path / "claude"
    _oauth_home(home)
    _fixture(home)
    _write_root(
        home, "vertex-root",
        _assistant(
            session="vertex-root", message_id="msg_vrtx_01", timestamp="2026-09-01T10:00:00Z",
            model="claude-future", input_tokens=1_000_000, output_tokens=0, cache_read=0, cache_write=0,
        ),
    )
    _write_root(
        home, "oauth-root",
        _assistant(
            session="oauth-root", message_id="msg_01", timestamp="2026-09-01T10:00:00Z",
            model="claude-future", input_tokens=1_000_000, output_tokens=0, cache_read=0, cache_write=0,
        ),
        _assistant(
            session="oauth-root", message_id="msg_02", timestamp="2026-09-01T10:00:01Z",
            model="claude-future", input_tokens=1_000_000, output_tokens=0, cache_read=0, cache_write=0,
            speed="fast",
        ),
    )
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)

    with database(settings.database) as conn:
        covered_before = conn.execute(
            "SELECT cost_usd,equivalent_cost_usd,pricing_note FROM usage WHERE session_id='claude:root' ORDER BY id"
        ).fetchall()
        add_price(
            conn, model="claude-future", provider="anthropic", effective_from="2026-01-01T00:00:00Z",
            input_per_million="2", cached_input_per_million="1", cache_write_per_million="1",
            output_per_million="1", source="test",
        )
        assert reprice_usage(conn, provider="anthropic") == 2
        covered_after = conn.execute(
            "SELECT cost_usd,equivalent_cost_usd,pricing_note FROM usage WHERE session_id='claude:root' ORDER BY id"
        ).fetchall()
    assert covered_before == covered_after
    assert _usage_row(settings, "claude:vertex-root:root:msg_vrtx_01")[:5] == (
        "vertex", "metered", Decimal("2"), None, 1,
    )
    assert _usage_row(settings, "claude:oauth-root:root:msg_01")[:5] == (
        "anthropic-oauth", "subscription", Decimal("0"), Decimal("2"), 1,
    )
    # Fast-mode rows bill at a premium the table does not carry; they stay unpriced.
    fast = _usage_row(settings, "claude:oauth-root:root:msg_02")
    assert fast[:5] == ("anthropic-oauth", "subscription", Decimal("0"), None, 0)
    assert fast[6] == "fast-mode request; standard list price not applicable"


def test_parser_version_bump_rereads_previously_fingerprinted_units(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    _fixture(home)
    settings = _Settings(tmp_path / "dashboard.sqlite", home)
    ingest_claude(settings)
    assert ingest_claude(settings).unchanged_files == 2

    monkeypatch.setattr(claude_module, "PARSER_VERSION", claude_module.PARSER_VERSION + 1)
    assert ingest_claude(settings).unchanged_files == 0
    assert ingest_claude(settings).unchanged_files == 2
