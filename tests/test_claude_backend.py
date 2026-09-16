from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spenda.db import SCHEMA, initialize
from spenda.ingestion.claude_auth import message_backend, read_auth_profile


@pytest.mark.parametrize(
    ("message_id", "request_id", "expected"),
    (
        ("msg_vrtx_01AbC", "req_vrtx_01AbC", "vertex"),
        ("msg_01AbC", "req_vrtx_01AbC", "vertex"),
        ("msg_bdrk_01AbC", None, "bedrock"),
        ("msg_01AbC", "req_01AbC", "anthropic"),
        ("msg_01AbC", None, "anthropic"),
        ("b29181d0-5d11-4ba2-a119-000000000000", None, "unknown"),
        (None, None, "unknown"),
    ),
)
def test_message_backend_uses_identifier_prefixes(message_id, request_id, expected):
    assert message_backend(message_id, request_id) == expected


def _home(tmp_path: Path, *, config: dict | None = None, sibling: bool = True, settings: dict | None = None) -> Path:
    home = tmp_path / ".claude"
    home.mkdir(parents=True)
    if config is not None:
        target = tmp_path / ".claude.json" if sibling else home / ".claude.json"
        target.write_text(json.dumps(config), encoding="utf-8")
    if settings is not None:
        (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def test_oauth_profile_from_sibling_config(tmp_path):
    home = _home(tmp_path, config={
        "oauthAccount": {
            "billingType": "stripe_subscription", "organizationType": "claude_max",
            "organizationName": "Example Org", "emailAddress": "private@example.com",
        },
        "customApiKeyResponses": {"approved": ["abcdef"], "rejected": []},
    })

    profile = read_auth_profile(home, environ={})

    assert profile.oauth and profile.anthropic_backend == "anthropic-oauth"
    assert (profile.billing_type, profile.organization_type, profile.organization_name) == (
        "stripe_subscription", "claude_max", "Example Org",
    )
    assert profile.config_path == tmp_path / ".claude.json"
    # A previously approved key is history, not a live key: no ambiguity.
    assert profile.api_key_history and not profile.api_key_hint and not profile.ambiguous
    assert profile.login == "oauth (claude_max, stripe_subscription, Example Org)"
    assert "private@example.com" not in repr(profile)


def test_config_inside_home_wins_over_sibling(tmp_path):
    home = _home(tmp_path, config={"oauthAccount": {"organizationType": "sibling"}})
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationType": "inside"}}), encoding="utf-8")

    assert read_auth_profile(home, environ={}).organization_type == "inside"


def test_api_key_profile_from_settings_and_environment(tmp_path):
    home = _home(tmp_path, config={"customApiKeyResponses": {"approved": ["abcdef"]}})

    history_only = read_auth_profile(home, environ={})
    assert (history_only.oauth, history_only.anthropic_backend, history_only.login) == (
        False, "anthropic", "api-key (previously approved)",
    )

    (home / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-secret"}}), encoding="utf-8")
    live = read_auth_profile(home, environ={})
    assert live.api_key_hint and live.anthropic_backend == "anthropic-api" and live.login == "api-key"
    assert "sk-secret" not in repr(live)

    from_env = read_auth_profile(_home(tmp_path / "other"), environ={"ANTHROPIC_AUTH_TOKEN": "x"})
    assert from_env.api_key_hint and from_env.anthropic_backend == "anthropic-api"


def test_vertex_machine_without_login_stays_unverified(tmp_path):
    home = _home(
        tmp_path, config={"oauthAccount": {}},
        settings={"env": {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "proj"}},
    )

    profile = read_auth_profile(home, environ={})

    assert not profile.oauth and profile.vertex_hint and not profile.bedrock_hint
    assert profile.anthropic_backend == "anthropic" and profile.login == "unknown"

    bedrock = read_auth_profile(home, environ={"CLAUDE_CODE_USE_BEDROCK": "true"})
    assert bedrock.bedrock_hint


def test_login_and_live_key_are_ambiguous_and_override_resolves(tmp_path):
    home = _home(
        tmp_path, config={"oauthAccount": {"organizationType": "claude_max"}},
        settings={"apiKeyHelper": "/usr/local/bin/key-helper"},
    )

    profile = read_auth_profile(home, environ={})
    assert profile.ambiguous and profile.anthropic_backend == "anthropic-api"

    forced_api = read_auth_profile(home, environ={}, override="api")
    forced_subscription = read_auth_profile(_home(tmp_path / "bare"), environ={}, override="subscription")
    assert forced_api.anthropic_backend == "anthropic-api" and not forced_api.ambiguous
    assert forced_subscription.anthropic_backend == "anthropic-oauth"
    assert read_auth_profile(home, environ={}, override="bogus").override == "auto"


def test_malformed_or_missing_config_is_unknown(tmp_path):
    home = _home(tmp_path)
    (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
    (home / "settings.json").write_text("[]", encoding="utf-8")

    profile = read_auth_profile(home, environ={})

    assert profile.config_path is None and profile.anthropic_backend == "anthropic"


def test_credentials_file_is_never_opened(tmp_path, monkeypatch):
    home = _home(tmp_path, config={"oauthAccount": {"organizationType": "claude_max"}})
    credentials = home / ".credentials.json"
    credentials.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret-token"}}), encoding="utf-8")
    original_read = Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path.name == ".credentials.json":
            raise AssertionError(f"credentials were read: {path}")
        return original_read(path, *args, **kwargs)

    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.name == ".credentials.json":
            raise AssertionError(f"credentials were opened: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(Path, "open", guarded_open)

    profile = read_auth_profile(home, environ={})

    assert profile.anthropic_backend == "anthropic-oauth"
    assert "secret-token" not in repr(profile)


def test_schema_v5_database_gains_backend_columns(tmp_path):
    path = tmp_path / "dashboard.sqlite"
    legacy = SCHEMA
    for column in (
        "    root_backend TEXT,\n", "    backend TEXT,\n",
        "    billing_mode TEXT NOT NULL DEFAULT 'metered',\n",
        "    cache_write_1h_input_tokens INTEGER NOT NULL DEFAULT 0,\n",
        "    equivalent_cost_usd TEXT,\n",
        "CREATE INDEX IF NOT EXISTS idx_usage_backend ON usage(backend);\n",
    ):
        assert column in legacy
        legacy = legacy.replace(column, "")
    with sqlite3.connect(path) as conn:
        conn.executescript(legacy)
        conn.execute("INSERT INTO dashboard_meta(key,value) VALUES('schema_version','5')")
        conn.execute(
            "INSERT INTO sessions(id,root_thread_id,source_app,source_home) VALUES('claude:s','claude:s','claude','/h')"
        )
        conn.execute("INSERT INTO agents(thread_id,session_id) VALUES('claude:s','claude:s')")
        conn.execute(
            """INSERT INTO usage(source_record_identity,session_id,thread_id,timestamp,model,provider,
               input_tokens,cached_input_tokens,cache_write_input_tokens,uncached_input_tokens,output_tokens,
               reasoning_output_tokens,total_tokens,source_file,source_event_type,cost_usd)
               VALUES('claude:m','claude:s','claude:s','2026-09-01T00:00:00Z','m','anthropic',
               1,0,0,1,1,0,2,'/h/f','claude_assistant_message','0')"""
        )

    initialize(path)

    with sqlite3.connect(path) as conn:
        usage = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
        sessions = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        agents = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        row = conn.execute(
            "SELECT backend,billing_mode,cache_write_1h_input_tokens,equivalent_cost_usd,"
            "cost_usd,counts_toward_totals FROM usage"
        ).fetchone()
        version = conn.execute("SELECT value FROM dashboard_meta WHERE key='schema_version'").fetchone()[0]
    assert {
        "backend", "billing_mode", "cache_write_1h_input_tokens", "equivalent_cost_usd",
        "counts_toward_totals",
    } <= usage
    assert "root_backend" in sessions and "backend" in agents
    assert row == (None, "metered", 0, None, "0", 1)
    assert version == "7"
