from __future__ import annotations

from decimal import Decimal

from conftest import atomic, make_state, session_meta, thread, turn, write_rollout
from spenda.db import database
from spenda.ingestion import scanner
from spenda.ingestion.rollout import RolloutParser
from spenda.ingestion.scanner import ingest


def settings_applied(tier: str | None, thread_id: str = "root", ordinal: int = 1) -> dict:
    settings = {"model": "gpt-5.6-sol"} | ({"service_tier": tier} if tier else {})
    return {"timestamp": "2026-09-08T10:00:00.500Z", "type": "event_msg", "ordinal": ordinal,
            "payload": {"type": "thread_settings_applied", "thread_id": thread_id, "thread_settings": settings}}


def write_root(settings, records, model="gpt-5.6-sol"):
    path = settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-root.jsonl"
    write_rollout(path, records)
    make_state(settings.codex_home, [thread("root", path, model=model, agent_path="/root", role="root")])
    return path


def rows(settings):
    with database(settings.database, readonly=True) as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT response_id,service_tier,cost_usd,pricing_note FROM usage ORDER BY response_id"
        )]


def test_parser_tracks_tier_per_thread():
    parser = RolloutParser()
    parser.parse(session_meta("root"), "k")
    parser.parse(settings_applied("priority"), "k")
    parser.parse(settings_applied("default", thread_id="other"), "k")
    assert parser.context.service_tier == "priority"
    parser.parse(settings_applied("default"), "k")
    assert parser.context.service_tier == "default"


def test_priority_calls_bill_at_fast_multiplier(dashboard_settings):
    write_root(dashboard_settings, [
        session_meta("root"), settings_applied("default"), turn("t1", ordinal=2), atomic("root", "t1", "a", ordinal=3),
        settings_applied("priority", ordinal=4), turn("t2", ordinal=5), atomic("root", "t2", "b", ordinal=6),
    ])
    ingest(dashboard_settings)
    (_, tier_a, cost_a, _), (_, tier_b, cost_b, note_b) = rows(dashboard_settings)
    assert (tier_a, tier_b) == ("default", "priority")
    assert Decimal(cost_b) == 2 * Decimal(cost_a)
    assert "Fast-mode (priority) 2x" in note_b


def test_model_without_fast_tier_leaves_priority_call_unpriced(dashboard_settings):
    write_root(dashboard_settings, [
        session_meta("root"), settings_applied("priority"), turn("t1", "gpt-5.4-pro", 2),
        atomic("root", "t1", "a", ordinal=3, timestamp="2026-09-08T10:00:02Z"),
    ], model="gpt-5.4-pro")
    ingest(dashboard_settings)
    assert rows(dashboard_settings) == [("a", "priority", None, "no Fast-mode price for gpt-5.4-pro")]


def test_parser_upgrade_backfills_tier_on_stored_calls(dashboard_settings, monkeypatch):
    write_root(dashboard_settings, [
        session_meta("root"), settings_applied("priority"), turn("t1", ordinal=2), atomic("root", "t1", "a", ordinal=3),
    ])
    ingest(dashboard_settings)
    with database(dashboard_settings.database) as conn:
        # Simulate a call stored by the previous parser, which ignored tiers.
        conn.execute("UPDATE usage SET service_tier=NULL")
        conn.execute("UPDATE ingestion_state SET parser_version=2")
        from spenda.pricing import reprice_usage
        reprice_usage(conn)
        standard = Decimal(conn.execute("SELECT cost_usd FROM usage").fetchone()[0])
    assert scanner.PARSER_VERSION == 3
    ingest(dashboard_settings)
    ((_, tier, cost, _),) = rows(dashboard_settings)
    assert tier == "priority" and Decimal(cost) == 2 * standard


def test_call_without_fast_price_is_not_repriced_every_pass(dashboard_settings, monkeypatch):
    write_root(dashboard_settings, [
        session_meta("root"), settings_applied("priority"), turn("t1", "gpt-5.4-pro", 2),
        atomic("root", "t1", "a", ordinal=3, timestamp="2026-09-08T10:00:02Z"),
    ], model="gpt-5.4-pro")
    ingest(dashboard_settings)
    from spenda import price_sources
    from spenda.price_sources import fill_missing_prices

    def reprice(*_args, **_kwargs):
        raise AssertionError("unchanged call repriced")

    monkeypatch.setattr(price_sources, "reprice_usage", reprice)
    with database(dashboard_settings.database) as conn:
        assert fill_missing_prices(conn, force=True, fetch=lambda: {}) == []
