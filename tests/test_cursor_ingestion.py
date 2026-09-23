from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import spenda.ingestion.cursor as cursor_module
from spenda.config import Settings
from spenda.db import database
from spenda.ingestion.cursor import ingest_cursor, resolve_slug
from spenda.pricing import add_price, reprice_usage

PRIVATE = (
    "private user prompt", "private assistant text", "private-command", "/private/file",
    "private thinking", "private answer", "private draft", "private tool arguments", "SECRET-TOKEN",
    "private blob content",
)


def _line(record: dict) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def _assistant(*blocks: dict | str) -> str:
    content = blocks[0] if len(blocks) == 1 and isinstance(blocks[0], str) else list(blocks)
    return _line({"role": "assistant", "message": {"content": content}})


def _user(text: str = "private user prompt") -> str:
    return _line({"role": "user", "message": {"content": [{"type": "text", "text": text}]}})


def _tool(name: str, **arguments) -> dict:
    return {"type": "tool_use", "name": name, "input": arguments or {"argument": "private tool arguments"}}


def _write_transcript(home: Path, slug: str, root: str, lines: list[str], agent: str | None = None) -> Path:
    directory = home / "projects" / slug / "agent-transcripts" / root
    path = directory / f"{root}.jsonl" if agent is None else directory / "subagents" / f"{agent}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _write_cli_store(home: Path, workspace: str, session: str, meta: dict) -> Path:
    path = home / "chats" / workspace / session / "store.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB); CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        )
        # Cursor stores the metadata document as hex-encoded JSON text.
        conn.execute("INSERT INTO meta VALUES('0',?)", (json.dumps(meta).encode("utf-8").hex(),))
        conn.execute("INSERT INTO blobs VALUES('root',?)", (b"private blob content",))
    return path


def _bubble(composer: str, bubble: str, created_at: str, *, kind: int = 2, **fields) -> tuple[str, bytes]:
    record = {"_v": 3, "bubbleId": bubble, "type": kind, "createdAt": created_at,
              "tokenCount": {"inputTokens": 0, "outputTokens": 0}, "text": "", **fields}
    return f"bubbleId:{composer}:{bubble}", json.dumps(record).encode("utf-8")


def _composer(composer: str, **fields) -> tuple[str, bytes]:
    record = {
        "_v": 18, "composerId": composer, "text": "private draft", "richText": "private draft",
        "createdAt": 1789818525900, "lastUpdatedAt": 1789819801172, "isDraft": False,
        "modelConfig": {
            "modelName": "editor-model", "maxMode": False,
            "selectedModels": [{"modelId": "editor-model", "parameters": [{"id": "effort", "value": "high"}]}],
        },
        "workspaceIdentifier": {"id": "ws", "uri": {"fsPath": "/work/editor-repo", "scheme": "file"}},
        "subagentComposerIds": [], "subComposerIds": [], "name": "Synthetic editor task", **fields,
    }
    return f"composerData:{composer}", json.dumps(record).encode("utf-8")


def _editor_rows() -> list[tuple[str, bytes]]:
    thinking = {"thinking": {"text": "private thinking"}}
    return [
        _composer("ide-1"),
        _composer("ide-old", name="Editor-only task", createdAt=1780000000000, lastUpdatedAt=1780000100000),
        _composer("empty-state-draft", isDraft=True, name="Draft"),
        # ide-1: two model responses; the first spans thinking, text, and a tool call.
        _bubble("ide-1", "u1", "2026-09-19T11:48:45.985Z", kind=1, text="private user prompt",
                modelInfo={"modelName": "editor-model"}),
        _bubble("ide-1", "b1", "2026-09-19T11:48:50.013Z", tokenCount={"inputTokens": 10, "outputTokens": 0},
                **thinking),
        _bubble("ide-1", "b2", "2026-09-19T11:48:50.016Z", tokenCount={"inputTokens": 0, "outputTokens": 5},
                text="private answer"),
        _bubble("ide-1", "b3", "2026-09-19T11:48:50.024Z",
                toolFormerData={"name": "run_terminal_command_v2", "params": "private tool arguments"}),
        _bubble("ide-1", "b4", "2026-09-19T11:48:51.822Z", **thinking),
        _bubble("ide-1", "b5", "2026-09-19T11:48:51.825Z", tokenCount={"inputTokens": 7, "outputTokens": 3},
                toolFormerData={"name": "read_file_v2", "params": "private tool arguments"}),
        # ide-old has no transcript: text followed by a tool call is one response.
        _bubble("ide-old", "u1", "2026-06-01T10:00:00.000Z", kind=1, text="private user prompt",
                modelInfo={"modelName": "legacy-model"}),
        _bubble("ide-old", "b1", "2026-06-01T10:00:01.000Z", tokenCount={"inputTokens": 100, "outputTokens": 20},
                text="private answer"),
        _bubble("ide-old", "b2", "2026-06-01T10:00:02.000Z",
                toolFormerData={"name": "edit_file_v2", "params": "private tool arguments"}),
    ]


def _write_editor_state(user_dir: Path, rows: list[tuple[str, bytes]] | None = None) -> Path:
    path = user_dir / "globalStorage" / "state.vscdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE ItemTable(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);"
            "CREATE TABLE cursorDiskKV(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);"
        )
        conn.execute("INSERT INTO ItemTable VALUES('cursorAuth/accessToken',?)", (b"SECRET-TOKEN",))
        conn.executemany("INSERT INTO cursorDiskKV VALUES(?,?)", rows if rows is not None else _editor_rows())
    return path


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "cursor"
    user_dir = tmp_path / "cursor-user"
    _write_transcript(home, "work-repo", "root-1", [
        _user(),
        _assistant({"type": "text", "text": "private assistant text"}, _tool("Shell", command="private-command")),
        _assistant(_tool("ReadFile", path="/private/file")),
        _line({"type": "turn_ended", "status": "success"}),
        _assistant("private assistant text"),
    ])
    _write_transcript(home, "work-repo", "root-1", [_user(), _assistant(_tool("Grep"))], agent="agent-a")
    _write_cli_store(home, "workspace-hash", "root-1", {
        "agentId": "root-1", "name": "Synthetic Cursor task", "mode": "default",
        "createdAt": 1789000000000, "lastUsedModel": "cursor-model", "latestRootBlobId": "root",
    })
    _write_transcript(home, "work-editor-repo", "ide-1", [
        _user(),
        _assistant({"type": "text", "text": "private answer"}, _tool("Shell")),
        _assistant(_tool("ReadFile")),
    ])
    _write_editor_state(user_dir)
    return home, user_dir


def _settings(tmp_path: Path, home: Path, user_dir: Path, **overrides) -> Settings:
    return Settings(
        tmp_path / "codex", tmp_path / "dashboard.sqlite", running_window_seconds=0,
        opencode_database=tmp_path / "missing-opencode.sqlite", claude_home=tmp_path / "missing-claude",
        cursor_home=home, cursor_user_dir=user_dir, **overrides,
    )


def _dump(settings: Settings) -> str:
    with sqlite3.connect(settings.database) as conn:
        return "\n".join(conn.iterdump())


def test_cursor_transcripts_and_cli_store_become_sessions_without_content(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)

    summary = ingest_cursor(settings)

    assert (summary.scanned_files, summary.root_sessions, summary.subagent_sessions) == (3, 3, 1)
    assert summary.usage_records == 3 + 1 + 2 + 1
    with database(settings.database, readonly=True) as conn:
        session = conn.execute(
            "SELECT title,repo_name,cwd,root_model,root_provider,created_at,accounting_status,turn_count,"
            "source_app FROM sessions WHERE id='cursor:root-1'"
        ).fetchone()
        rows = conn.execute(
            "SELECT source_record_identity,thread_id,call_label,model,timestamp,cost_usd FROM usage "
            "WHERE session_id='cursor:root-1' ORDER BY thread_id,source_ordinal"
        ).fetchall()
        paths = dict(conn.execute("SELECT thread_id,agent_path FROM agents WHERE session_id='cursor:root-1'"))
    assert tuple(session) == (
        "Synthetic Cursor task", "work-repo", None, "cursor-model", "cursor", "2026-09-10T00:26:40Z", "partial", 4,
        "cursor",
    )
    assert [tuple(row) for row in rows] == [
        ("cursor:root-1:root:1", "cursor:root-1", "Run command", "cursor-model", "2026-09-10T00:26:40Z", None),
        ("cursor:root-1:root:2", "cursor:root-1", "Read files", "cursor-model", "2026-09-10T00:26:40Z", None),
        ("cursor:root-1:root:4", "cursor:root-1", "Assistant response", "cursor-model", "2026-09-10T00:26:40Z", None),
        ("cursor:root-1:agent-a:1", "cursor:root-1:agent:agent-a", "Search files", "unknown-model",
         "2026-09-10T00:26:40Z", None),
    ]
    assert paths == {"cursor:root-1": "/root", "cursor:root-1:agent:agent-a": "/root/agent-a"}
    dump = _dump(settings)
    assert not [item for item in PRIVATE if item in dump]


def test_editor_bubbles_supply_timestamps_tokens_and_metadata_for_transcript_sessions(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)

    summary = ingest_cursor(settings)

    assert summary.unknown_prices == {"cursor:editor-model", "cursor:legacy-model"}
    with database(settings.database, readonly=True) as conn:
        session = conn.execute(
            "SELECT title,cwd,repo_name,root_model,root_reasoning_effort,created_at,accounting_status,updated_at "
            "FROM sessions WHERE id='cursor:ide-1'"
        ).fetchone()
        rows = conn.execute(
            "SELECT timestamp,model,input_tokens,output_tokens,total_tokens,call_label,cost_usd FROM usage "
            "WHERE session_id='cursor:ide-1' ORDER BY source_ordinal"
        ).fetchall()
    assert tuple(session)[:7] == (
        "Synthetic editor task", "/work/editor-repo", "editor-repo", "editor-model", "high",
        "2026-09-19T11:48:45.900000Z", "complete",
    )
    # The transcript was just written, so the file activity is later than the
    # editor's own lastUpdatedAt; the session keeps the latest of the two.
    assert session["updated_at"] > "2026-09-19T12:10:01.172000Z"
    assert [tuple(row) for row in rows] == [
        ("2026-09-19T11:48:50.013000Z", "editor-model", 10, 5, 15, "Run command", None),
        ("2026-09-19T11:48:51.822000Z", "editor-model", 7, 3, 10, "Read files", None),
    ]

    with database(settings.database) as conn:
        add_price(
            conn, model="editor-model", provider="cursor", effective_from="2026-01-01T00:00:00Z",
            input_per_million="1", cached_input_per_million="1", cache_write_per_million="1",
            output_per_million="2", source="test",
        )
        repriced = reprice_usage(conn, provider="cursor")
        costs = [row[0] for row in conn.execute(
            "SELECT cost_usd FROM usage WHERE session_id='cursor:ide-1' ORDER BY source_ordinal"
        )]
    assert repriced >= 2
    assert [float(cost) for cost in costs] == [0.00002, 0.000013]


def test_editor_only_composer_is_imported_from_bubbles_and_drafts_are_skipped(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)

    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        session = conn.execute(
            "SELECT title,root_model,created_at,turn_count,accounting_status FROM sessions WHERE id='cursor:ide-old'"
        ).fetchone()
        rows = conn.execute(
            "SELECT source_record_identity,timestamp,model,input_tokens,output_tokens,call_label,source_file "
            "FROM usage WHERE session_id='cursor:ide-old'"
        ).fetchall()
        drafts = conn.execute("SELECT COUNT(*) FROM sessions WHERE id LIKE '%draft%'").fetchone()[0]
    assert tuple(session) == ("Editor-only task", "editor-model", "2026-05-28T20:26:40Z", 1, "complete")
    assert [tuple(row) for row in rows] == [(
        "cursor:ide-old:root:bubble:b1", "2026-06-01T10:00:01Z", "legacy-model", 100, 20, "Apply file change",
        str(user_dir / "globalStorage" / "state.vscdb"),
    )]
    assert drafts == 0
    assert "SECRET-TOKEN" not in _dump(settings)


def test_bubble_count_mismatch_falls_back_to_session_timestamp_with_warning(tmp_path):
    home, user_dir = _fixture(tmp_path)
    _write_transcript(home, "work-editor-repo", "ide-1", [
        _user(), _assistant(_tool("Shell")), _assistant(_tool("ReadFile")), _assistant("private assistant text"),
    ])
    settings = _settings(tmp_path, home, user_dir)

    summary = ingest_cursor(settings)

    assert summary.parser_warnings == 1
    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT timestamp,total_tokens FROM usage WHERE session_id='cursor:ide-1' ORDER BY source_ordinal"
        ).fetchall()
        warning = conn.execute("SELECT source_key,code FROM parser_warnings").fetchone()
        status = conn.execute("SELECT accounting_status FROM sessions WHERE id='cursor:ide-1'").fetchone()[0]
    assert [tuple(row) for row in rows] == [("2026-09-19T11:48:45.900000Z", 0)] * 3
    assert tuple(warning) == ("cursor:ide-1", "cursor_bubble_alignment")
    assert status == "partial"


def test_unchanged_transcripts_are_skipped_until_a_file_changes(tmp_path, monkeypatch):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    reads: list[Path] = []
    original = cursor_module._read_transcript

    def counting_read(transcript, summary):
        reads.append(transcript.path)
        return original(transcript, summary)

    monkeypatch.setattr(cursor_module, "_read_transcript", counting_read)

    second = ingest_cursor(settings)

    assert (second.usage_records, second.duplicate_records, second.unchanged_files) == (0, 0, 3)
    assert reads == []

    root = home / "projects" / "work-repo" / "agent-transcripts" / "root-1" / "root-1.jsonl"
    with root.open("a", encoding="utf-8") as handle:
        handle.write(_assistant(_tool("Write", path="/private/file")))
    third = ingest_cursor(settings)

    assert [path.name for path in reads] == ["root-1.jsonl", "agent-a.jsonl"]
    assert (third.usage_records, third.duplicate_records) == (1, 4)
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT turn_count FROM sessions WHERE id='cursor:root-1'").fetchone()[0] == 5

    forced = ingest_cursor(settings, force_all=True)
    assert (forced.usage_records, forced.duplicate_records) == (0, 4 + 1 + 2 + 1)


def test_removed_transcripts_reconcile_without_touching_other_sources(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    with database(settings.database) as conn:
        conn.execute(
            "INSERT INTO sessions(id,root_thread_id,source_app,source_home) VALUES(?,?,?,?)",
            ("codex-kept", "codex-kept", "codex", "/tmp/codex"),
        )
        conn.execute(
            "INSERT INTO agents(thread_id,session_id,agent_role,source_kind) VALUES(?,?,?,?)",
            ("codex-kept", "codex-kept", "root", "cli"),
        )

    (home / "projects" / "work-repo" / "agent-transcripts" / "root-1" / "subagents" / "agent-a.jsonl").unlink()
    summary = ingest_cursor(settings)

    assert (summary.root_sessions, summary.subagent_sessions) == (3, 0)
    with database(settings.database, readonly=True) as conn:
        subagent_rows = conn.execute(
            "SELECT COUNT(*) FROM usage WHERE thread_id LIKE 'cursor:root-1:agent:%'"
        ).fetchone()[0]
        assert subagent_rows == 0
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='codex-kept'").fetchone()[0] == 1

    # The editor still holds ide-1, so it survives as an editor-only session
    # whose rows now come from bubbles instead of the removed transcript.
    shutil.rmtree(home / "projects" / "work-editor-repo")
    summary = ingest_cursor(settings)

    assert summary.root_sessions == 3
    with database(settings.database, readonly=True) as conn:
        identities = sorted(row[0] for row in conn.execute(
            "SELECT source_record_identity FROM usage WHERE session_id='cursor:ide-1'"
        ))
        fingerprinted = sorted(Path(row[0]).name for row in conn.execute(
            "SELECT source_path FROM ingestion_state WHERE source_key LIKE 'cursor:%' "
            "AND source_key NOT LIKE 'cursor:composer:%'"
        ))
        assert fingerprinted == ["root-1.jsonl", "store.db", "store.db-wal"]
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='codex-kept'").fetchone()[0] == 1
    assert identities == ["cursor:ide-1:root:bubble:b1", "cursor:ide-1:root:bubble:b4"]

    _write_editor_state(user_dir, rows=[])
    summary = ingest_cursor(settings)

    assert summary.root_sessions == 1
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id='codex-kept'").fetchone()[0] == 1


def test_missing_projects_directory_keeps_imported_history(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    shutil.rmtree(home / "projects")
    summary = ingest_cursor(settings)

    assert summary.root_sessions == 3
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage WHERE session_id='cursor:root-1'").fetchone()[0] == 4


def test_editor_state_removal_reconciles_only_editor_only_sessions(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    _write_editor_state(user_dir, rows=[_composer("ide-1")])
    summary = ingest_cursor(settings)

    assert summary.root_sessions == 2
    with database(settings.database, readonly=True) as conn:
        remaining = sorted(row[0] for row in conn.execute("SELECT id FROM sessions WHERE source_app='cursor'"))
    assert remaining == ["cursor:ide-1", "cursor:root-1"]


def test_resolve_slug_walks_local_directories(tmp_path):
    (tmp_path / "home" / "user" / "sources" / "ai").mkdir(parents=True)
    (tmp_path / "home" / "user" / "sources" / "ai-shell").mkdir()
    (tmp_path / "home" / "user" / "proj.code-workspace").write_text("{}", encoding="utf-8")

    sources = tmp_path / "home" / "user" / "sources"
    assert resolve_slug("home-user-sources-ai-shell", root=tmp_path) == sources / "ai-shell"
    assert resolve_slug("home-user-sources-ai", root=tmp_path) == sources / "ai"
    assert resolve_slug("home-user-proj-code-workspace", root=tmp_path) == sources.parent / "proj.code-workspace"
    assert resolve_slug("home-user-missing", root=tmp_path) is None
    assert resolve_slug("empty-window", root=tmp_path) is None


def test_malformed_line_keeps_valid_records_and_is_reread_next_pass(tmp_path):
    home, user_dir = _fixture(tmp_path)
    root = home / "projects" / "work-repo" / "agent-transcripts" / "root-1" / "root-1.jsonl"
    root.write_bytes(root.read_bytes() + b"{not json\n" + _assistant(_tool("Shell")).encode("utf-8"))
    settings = _settings(tmp_path, home, user_dir)

    summary = ingest_cursor(settings)

    assert summary.malformed_lines == 1
    with database(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage WHERE thread_id='cursor:root-1'").fetchone()[0] == 4
        fingerprints = conn.execute(
            "SELECT COUNT(*) FROM ingestion_state WHERE source_path=?", (str(root),)
        ).fetchone()[0]
    assert fingerprints == 0


def test_transcripts_without_assistant_messages_are_not_sessions(tmp_path):
    home, user_dir = _fixture(tmp_path)
    _write_transcript(home, "empty-window", "failed-1", [
        _line({"type": "turn_ended", "status": "error", "error": "private error text"}),
    ])
    _write_transcript(home, "work-repo", "pending-1", [_user()])
    settings = _settings(tmp_path, home, user_dir)

    summary = ingest_cursor(settings)

    assert summary.root_sessions == 3
    with database(settings.database, readonly=True) as conn:
        ids = sorted(row[0] for row in conn.execute("SELECT id FROM sessions WHERE source_app='cursor'"))
    assert ids == ["cursor:ide-1", "cursor:ide-old", "cursor:root-1"]

    with (home / "projects" / "work-repo" / "agent-transcripts" / "pending-1" / "pending-1.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write(_assistant(_tool("Shell")))
    summary = ingest_cursor(settings)

    assert summary.root_sessions == 4
    assert "private error text" not in _dump(settings)


def test_editor_changes_refresh_a_unit_whose_transcript_is_unchanged(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    rows = [
        _composer("ide-1", name="Renamed editor task", lastUpdatedAt=1789819900000)
        if key == "composerData:ide-1" else (key, value)
        for key, value in _editor_rows()
    ]
    rows = [
        _bubble("ide-1", "b1", "2026-09-19T11:48:50.013Z", tokenCount={"inputTokens": 50, "outputTokens": 0},
                thinking={"text": "private thinking"})
        if key == "bubbleId:ide-1:b1" else (key, value)
        for key, value in rows
    ]
    _write_editor_state(user_dir, rows=rows)
    summary = ingest_cursor(settings)

    assert summary.unchanged_files == 2
    with database(settings.database, readonly=True) as conn:
        title = conn.execute("SELECT title FROM sessions WHERE id='cursor:ide-1'").fetchone()[0]
        tokens = conn.execute(
            "SELECT input_tokens FROM usage WHERE session_id='cursor:ide-1' ORDER BY source_ordinal"
        ).fetchall()
    assert title == "Renamed editor task"
    assert [row[0] for row in tokens] == [50, 7]


def test_editor_state_that_appears_later_fills_in_tokens_and_titles(tmp_path):
    home, user_dir = _fixture(tmp_path)
    state = user_dir / "globalStorage" / "state.vscdb"
    state.unlink()
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    with database(settings.database, readonly=True) as conn:
        before = conn.execute(
            "SELECT title,(SELECT SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-1') "
            "FROM sessions WHERE id='cursor:ide-1'"
        ).fetchone()
    assert tuple(before) == ("Cursor session", 0)

    _write_editor_state(user_dir)
    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        after = conn.execute(
            "SELECT title,(SELECT SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-1') "
            "FROM sessions WHERE id='cursor:ide-1'"
        ).fetchone()
    assert tuple(after) == ("Synthetic editor task", 25)


def test_tool_call_ids_split_responses_for_models_without_thinking(tmp_path):
    home, user_dir = _fixture(tmp_path)
    rows = _editor_rows() + [
        _composer("ide-flat", name="Flat model task"),
        _bubble("ide-flat", "u1", "2026-09-20T09:00:00.000Z", kind=1, text="private user prompt",
                modelInfo={"modelName": "flat-model"}),
        _bubble("ide-flat", "b1", "2026-09-20T09:00:01.000Z", text="private answer"),
        _bubble("ide-flat", "b2", "2026-09-20T09:00:02.000Z",
                toolFormerData={"name": "read_file_v2", "toolCallId": "call-aaaa-0\nfc_x_0", "modelCallId": ""}),
        _bubble("ide-flat", "b3", "2026-09-20T09:00:02.100Z",
                toolFormerData={"name": "read_file_v2", "toolCallId": "call-aaaa-1\nfc_x_1", "modelCallId": ""}),
        _bubble("ide-flat", "b4", "2026-09-20T09:00:05.000Z",
                toolFormerData={"name": "edit_file_v2", "toolCallId": "call-bbbb-2"}),
        _bubble("ide-flat", "b5", "2026-09-20T09:00:09.000Z", text="private answer"),
    ]
    _write_editor_state(user_dir, rows=rows)
    _write_transcript(home, "work-editor-repo", "ide-flat", [
        _user(),
        _assistant({"type": "text", "text": "private answer"}, _tool("ReadFile"), _tool("ReadFile")),
        _assistant(_tool("StrReplace")),
        _assistant("private answer"),
    ])
    settings = _settings(tmp_path, home, user_dir)

    summary = ingest_cursor(settings)

    assert summary.parser_warnings == 0
    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT timestamp,call_label FROM usage WHERE session_id='cursor:ide-flat' ORDER BY source_ordinal"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("2026-09-20T09:00:01Z", "Read files"),
        ("2026-09-20T09:00:05Z", "Apply file change"),
        ("2026-09-20T09:00:09Z", "Assistant response"),
    ]


def test_session_with_any_uncounted_response_stays_partial(tmp_path):
    home, user_dir = _fixture(tmp_path)
    _write_transcript(home, "work-editor-repo", "ide-1", [_user(), _assistant(_tool("Grep"))], agent="agent-b")
    settings = _settings(tmp_path, home, user_dir)

    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        status = conn.execute(
            "SELECT accounting_status,accounting_note FROM sessions WHERE id='cursor:ide-1'"
        ).fetchone()
    assert tuple(status) == ("partial", "Cursor keeps no token or cost accounting in its local history")


def test_alignment_warning_is_recorded_once_per_transcript(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    transcript = home / "projects" / "work-editor-repo" / "agent-transcripts" / "ide-1" / "ide-1.jsonl"
    transcript.write_text("".join([_user(), _assistant(_tool("Shell")), _assistant(_tool("ReadFile")),
                                   _assistant("private assistant text")]), encoding="utf-8")
    ingest_cursor(settings)
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_assistant("private assistant text"))
    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        warnings = conn.execute(
            "SELECT COUNT(*) FROM parser_warnings WHERE code='cursor_bubble_alignment'"
        ).fetchone()[0]
    assert warnings == 1


def test_discover_cursor_home_uses_settings(tmp_path):
    from spenda.ingestion.cursor import discover_cursor_home

    settings = _settings(tmp_path, tmp_path / "cursor", tmp_path / "cursor-user")
    assert discover_cursor_home(settings) == (tmp_path / "cursor").resolve()


def test_transcript_replaces_editor_bubble_rows_for_the_same_thread(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    _write_transcript(home, "work-editor-repo", "ide-old", [_user(), _assistant(_tool("StrReplace"))])
    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT source_record_identity,total_tokens FROM usage WHERE session_id='cursor:ide-old'"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("cursor:ide-old:root:1", 120)]


def test_undecodable_composer_document_does_not_delete_its_session(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    with sqlite3.connect(user_dir / "globalStorage" / "state.vscdb") as conn:
        conn.execute("UPDATE cursorDiskKV SET value=? WHERE key='composerData:ide-old'", (b"{broken",))
    summary = ingest_cursor(settings)

    assert summary.parser_warnings == 1
    with database(settings.database, readonly=True) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM usage WHERE session_id='cursor:ide-old'").fetchone()[0]
        codes = [row[0] for row in conn.execute("SELECT code FROM parser_warnings")]
    assert rows == 1
    assert codes == ["cursor_composer_unreadable"]


def test_editor_outage_does_not_fingerprint_units_read_without_editor_data(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    state = user_dir / "globalStorage" / "state.vscdb"
    healthy = state.read_bytes()
    state.write_bytes(b"not a database")
    transcript = home / "projects" / "work-editor-repo" / "agent-transcripts" / "ide-1" / "ide-1.jsonl"
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_assistant(_tool("Shell")))
    ingest_cursor(settings)
    with database(settings.database, readonly=True) as conn:
        during = conn.execute("SELECT SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-1'").fetchone()[0]
    assert during == 0

    # The editor recovers with a matching third response but the same lastUpdatedAt.
    state.write_bytes(healthy)
    with sqlite3.connect(state) as conn:
        conn.executemany("INSERT INTO cursorDiskKV VALUES(?,?)", [
            _bubble("ide-1", "b6", "2026-09-19T11:48:59.000Z", thinking={"text": "private thinking"},
                    tokenCount={"inputTokens": 4, "outputTokens": 1}),
            _bubble("ide-1", "b7", "2026-09-19T11:48:59.500Z",
                    toolFormerData={"name": "run_terminal_command_v2", "toolCallId": "call-cccc-9"}),
        ])
    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        after = conn.execute("SELECT SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-1'").fetchone()[0]
    assert after == 30


def test_repricing_never_prices_placeholder_rows_without_tokens(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    with database(settings.database) as conn:
        add_price(
            conn, model="cursor-model", provider="cursor", effective_from="2020-01-01T00:00:00Z",
            input_per_million="1", cached_input_per_million="1", cache_write_per_million="1",
            output_per_million="1", source="test",
        )
        reprice_usage(conn, provider="cursor")
        rows = conn.execute(
            "SELECT DISTINCT cost_usd,pricing_note FROM usage WHERE session_id='cursor:root-1'"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(None, "Cursor keeps no token or cost accounting in its local history")]


def test_cli_store_changes_refresh_an_unchanged_transcript_unit(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    _write_cli_store(home, "workspace-hash", "root-1", {
        "agentId": "root-1", "name": "Renamed CLI task", "mode": "default",
        "createdAt": 1789000000000, "lastUsedModel": "newer-model",
    })
    summary = ingest_cursor(settings)

    assert summary.unchanged_files == 1
    with database(settings.database, readonly=True) as conn:
        title, model = conn.execute("SELECT title,root_model FROM sessions WHERE id='cursor:root-1'").fetchone()
    assert (title, model) == ("Renamed CLI task", "newer-model")


def test_nested_editor_only_composers_keep_their_immediate_parent(tmp_path):
    home, user_dir = _fixture(tmp_path)
    rows = [
        _composer("ide-old", name="Editor-only task", createdAt=1780000000000, lastUpdatedAt=1780000100000,
                  subagentComposerIds=["child"])
        if key == "composerData:ide-old" else (key, value)
        for key, value in _editor_rows()
    ] + [
        _composer("child", subagentComposerIds=["grandchild"]),
        _composer("grandchild"),
        _bubble("child", "b1", "2026-09-21T10:00:01.000Z", text="private answer"),
        _bubble("grandchild", "b1", "2026-09-21T10:00:02.000Z", text="private answer"),
    ]
    _write_editor_state(user_dir, rows=rows)
    settings = _settings(tmp_path, home, user_dir)

    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        agents = conn.execute(
            "SELECT thread_id,parent_thread_id,agent_path FROM agents WHERE session_id='cursor:ide-old' "
            "ORDER BY agent_path"
        ).fetchall()
    assert [tuple(row) for row in agents] == [
        ("cursor:ide-old", None, "/root"),
        ("cursor:ide-old:agent:child", "cursor:ide-old", "/root/child"),
        ("cursor:ide-old:agent:grandchild", "cursor:ide-old:agent:child", "/root/child/grandchild"),
    ]


def _grow_editor_session(user_dir: Path) -> None:
    with sqlite3.connect(user_dir / "globalStorage" / "state.vscdb") as conn:
        conn.executemany("INSERT INTO cursorDiskKV VALUES(?,?)", [
            _bubble("ide-1", "b6", "2026-09-19T11:48:59.000Z", thinking={"text": "private thinking"},
                    tokenCount={"inputTokens": 4, "outputTokens": 1}),
            _bubble("ide-1", "b7", "2026-09-19T11:48:59.500Z",
                    toolFormerData={"name": "run_terminal_command_v2", "toolCallId": "call-cccc-9"}),
        ])


def test_absent_editor_database_is_an_outage_once_editor_history_exists(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    state = user_dir / "globalStorage" / "state.vscdb"
    healthy = state.read_bytes()
    state.unlink()
    transcript = home / "projects" / "work-editor-repo" / "agent-transcripts" / "ide-1" / "ide-1.jsonl"
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_assistant(_tool("Shell")))
    ingest_cursor(settings)

    state.write_bytes(healthy)
    _grow_editor_session(user_dir)
    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        tokens = conn.execute("SELECT SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-1'").fetchone()[0]
    assert tokens == 30


def test_forced_reimport_without_editor_database_stops_treating_absence_as_outage(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    (user_dir / "globalStorage" / "state.vscdb").unlink()

    ingest_cursor(settings, force_all=True)
    summary = ingest_cursor(settings)

    assert summary.unchanged_files == 3
    with database(settings.database, readonly=True) as conn:
        marks = conn.execute(
            "SELECT COUNT(*) FROM ingestion_state WHERE source_key LIKE 'cursor:composer:%'"
        ).fetchone()[0]
    assert marks == 0


def test_incomplete_transcript_keeps_editor_rows_until_it_is_complete(tmp_path):
    home, user_dir = _fixture(tmp_path)
    rows = _editor_rows() + [
        _bubble("ide-old", "u2", "2026-06-01T10:01:00.000Z", kind=1, text="private user prompt"),
        _bubble("ide-old", "b3", "2026-06-01T10:01:01.000Z", text="private answer",
                tokenCount={"inputTokens": 50, "outputTokens": 10}),
    ]
    _write_editor_state(user_dir, rows=rows)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    transcript = _write_transcript(home, "work-editor-repo", "ide-old", [_user(), _assistant(_tool("StrReplace"))])
    transcript.write_bytes(transcript.read_bytes() + b'{"role":"assistant","message":{"content":[{"type":"te')
    ingest_cursor(settings)
    with database(settings.database, readonly=True) as conn:
        partial = conn.execute(
            "SELECT COUNT(*),SUM(total_tokens),COUNT(DISTINCT source_file) FROM usage "
            "WHERE session_id='cursor:ide-old'"
        ).fetchone()
    # Only the editor rows exist: the partial transcript is neither imported
    # beside them nor allowed to evict them.
    assert tuple(partial) == (2, 180, 1)

    transcript.write_text("".join([_user(), _assistant(_tool("StrReplace")), _assistant("private answer")]))
    ingest_cursor(settings)
    with database(settings.database, readonly=True) as conn:
        final = conn.execute(
            "SELECT COUNT(*),SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-old'"
        ).fetchone()
    assert tuple(final) == (2, 180)


def test_malformed_bubble_document_is_a_read_failure_that_keeps_rows(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    with sqlite3.connect(user_dir / "globalStorage" / "state.vscdb") as conn:
        conn.execute("UPDATE cursorDiskKV SET value=? WHERE key='bubbleId:ide-old:b1'", (b"{broken",))

    summary = ingest_cursor(settings, force_all=True)

    assert summary.parser_warnings == 1
    with database(settings.database, readonly=True) as conn:
        rows = conn.execute(
            "SELECT COUNT(*),SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-old'"
        ).fetchone()
        codes = [row[0] for row in conn.execute("SELECT code FROM parser_warnings")]
    assert tuple(rows) == (1, 120)
    assert codes == ["cursor_bubble_unreadable"]


def test_unreadable_parent_composer_does_not_import_its_child_as_a_new_root(tmp_path):
    home, user_dir = _fixture(tmp_path)
    rows = [
        _composer("ide-old", name="Editor-only task", createdAt=1780000000000, lastUpdatedAt=1780000100000,
                  subagentComposerIds=["child"])
        if key == "composerData:ide-old" else (key, value)
        for key, value in _editor_rows()
    ] + [
        _composer("child"),
        _bubble("child", "b1", "2026-09-21T10:00:01.000Z", text="private answer",
                tokenCount={"inputTokens": 100, "outputTokens": 20}),
    ]
    _write_editor_state(user_dir, rows=rows)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)
    with database(settings.database, readonly=True) as conn:
        before = tuple(conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM usage").fetchone())

    with sqlite3.connect(user_dir / "globalStorage" / "state.vscdb") as conn:
        conn.execute("UPDATE cursorDiskKV SET value=? WHERE key='composerData:ide-old'", (b"{broken",))
    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        after = tuple(conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM usage").fetchone())
        sessions = sorted(row[0] for row in conn.execute("SELECT id FROM sessions WHERE source_app='cursor'"))
    assert after == before
    assert sessions == ["cursor:ide-1", "cursor:ide-old", "cursor:root-1"]


def test_cli_store_changes_committed_to_the_wal_are_picked_up(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    store = home / "chats" / "workspace-hash" / "root-1" / "store.db"
    live = sqlite3.connect(store)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.commit()
        ingest_cursor(settings)

        renamed = {
            "agentId": "root-1", "name": "Renamed in WAL", "mode": "default",
            "createdAt": 1789000000000, "lastUsedModel": "wal-model",
        }
        live.execute("UPDATE meta SET value=?", (json.dumps(renamed).encode("utf-8").hex(),))
        live.commit()
        assert store.with_name("store.db-wal").stat().st_size > 0
        ingest_cursor(settings)
    finally:
        live.close()

    with database(settings.database, readonly=True) as conn:
        title, model = conn.execute("SELECT title,root_model FROM sessions WHERE id='cursor:root-1'").fetchone()
    assert (title, model) == ("Renamed in WAL", "wal-model")


def test_partial_transcript_with_one_aligned_message_does_not_double_count(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    transcript = _write_transcript(home, "work-editor-repo", "ide-old", [_user(), _assistant(_tool("StrReplace"))])
    transcript.write_bytes(transcript.read_bytes() + b'{"role":"assistant","message":{"content":[{"type":"te')
    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        rows = conn.execute("SELECT COUNT(*),SUM(total_tokens) FROM usage WHERE session_id='cursor:ide-old'").fetchone()
    assert tuple(rows) == (1, 120)


def test_locked_cli_store_is_retried_on_the_next_pass(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    store = home / "chats" / "workspace-hash" / "root-1" / "store.db"
    locker = sqlite3.connect(store, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        summary = ingest_cursor(settings)
    finally:
        locker.execute("ROLLBACK")
        locker.close()

    assert summary.parser_warnings == 1
    with database(settings.database, readonly=True) as conn:
        locked = conn.execute("SELECT title,root_model FROM sessions WHERE id='cursor:root-1'").fetchone()
        codes = [row[0] for row in conn.execute("SELECT code FROM parser_warnings")]
    assert tuple(locked) == ("Cursor session", None)
    assert codes == ["cursor_cli_store_unreadable"]

    summary = ingest_cursor(settings)

    assert summary.unchanged_files == 1
    with database(settings.database, readonly=True) as conn:
        recovered = conn.execute("SELECT title,root_model FROM sessions WHERE id='cursor:root-1'").fetchone()
    assert tuple(recovered) == ("Synthetic Cursor task", "cursor-model")


def test_subagent_transcripts_keep_editor_nesting(tmp_path):
    home, user_dir = _fixture(tmp_path)
    rows = [
        _composer("ide-1", subagentComposerIds=["child"]) if key == "composerData:ide-1" else (key, value)
        for key, value in _editor_rows()
    ] + [_composer("child", subagentComposerIds=["grandchild"]), _composer("grandchild")]
    _write_editor_state(user_dir, rows=rows)
    _write_transcript(home, "work-editor-repo", "ide-1", [_user(), _assistant(_tool("Grep"))], agent="child")
    _write_transcript(home, "work-editor-repo", "ide-1", [_user(), _assistant(_tool("Grep"))], agent="grandchild")
    settings = _settings(tmp_path, home, user_dir)

    ingest_cursor(settings)

    with database(settings.database, readonly=True) as conn:
        agents = conn.execute(
            "SELECT thread_id,parent_thread_id,agent_path FROM agents WHERE session_id='cursor:ide-1' "
            "ORDER BY agent_path"
        ).fetchall()
    assert [tuple(row) for row in agents] == [
        ("cursor:ide-1", None, "/root"),
        ("cursor:ide-1:agent:child", "cursor:ide-1", "/root/child"),
        ("cursor:ide-1:agent:grandchild", "cursor:ide-1:agent:child", "/root/child/grandchild"),
    ]


def test_unreadable_projects_folder_keeps_transcript_sourced_sessions(tmp_path):
    home, user_dir = _fixture(tmp_path)
    settings = _settings(tmp_path, home, user_dir)
    ingest_cursor(settings)

    def ide_state():
        with database(settings.database, readonly=True) as conn:
            source = conn.execute(
                "SELECT source_rollout_path FROM agents WHERE thread_id='cursor:ide-1'"
            ).fetchone()[0]
            rows = sorted(row[0] for row in conn.execute(
                "SELECT source_record_identity FROM usage WHERE session_id='cursor:ide-1'"
            ))
        return Path(source).name, rows

    before = ide_state()
    assert before == ("ide-1.jsonl", ["cursor:ide-1:root:1", "cursor:ide-1:root:2"])

    hidden = home / "projects-hidden"
    (home / "projects").rename(hidden)
    ingest_cursor(settings)
    assert ide_state() == before

    hidden.rename(home / "projects")
    ingest_cursor(settings)
    assert ide_state() == before
