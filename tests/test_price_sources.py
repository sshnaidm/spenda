from __future__ import annotations

from decimal import Decimal

from conftest import atomic, make_state, session_meta, thread, turn, write_rollout
from spenda.db import database
from spenda.ingestion.scanner import ingest
from spenda.ingestion.service import ingest_all
from spenda.price_sources import fill_missing_prices, price_row
from spenda.pricing import add_price, reprice_usage

CATALOGUE = {
    "openai": {"models": {
        "gpt-9-nova": {"cost": {
            "input": 2, "output": 10, "cache_read": 0.2, "cache_write": 2.5,
            "tiers": [{"input": 4, "output": 15, "cache_read": 0.4, "tier": {"type": "context", "size": 272000}}],
        }},
        "gpt-9-plain": {"cost": {"input": 1.75, "output": 14}},
    }},
}


def setup_model(settings, model, response_id="resp"):
    path = settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-root.jsonl"
    write_rollout(path, [session_meta("root"), turn("turn", model), atomic("root", "turn", response_id)])
    make_state(settings.codex_home, [thread("root", path, model=model, agent_path="/root", role="root")])


def usage_cost(settings):
    with database(settings.database, readonly=True) as conn:
        return tuple(conn.execute("SELECT price_id,cost_usd FROM usage").fetchone())


def test_price_row_maps_tiers_and_defaults():
    row = price_row(CATALOGUE, "openai", "gpt-9-nova", "2026-09-08T00:00:00Z")
    assert (row["input"], row["cached"], row["write"], row["output"]) == ("2", "0.2", "2.5", "10")
    assert (row["threshold"], row["long_in"], row["long_out"]) == (272000, "2", "1.5")
    plain = price_row(CATALOGUE, "openai", "gpt-9-plain", "2026-09-08T00:00:00Z")
    # No cached or write rate listed: no discount, writes at the input rate.
    assert (plain["cached"], plain["write"], plain["threshold"]) == ("1.75", "1.75", None)
    assert price_row(CATALOGUE, "openai", "missing", "2026-09-08T00:00:00Z") is None


def test_unpriced_codex_usage_is_priced_from_catalogue(dashboard_settings):
    setup_model(dashboard_settings, "gpt-9-nova")
    ingest(dashboard_settings)
    assert usage_cost(dashboard_settings) == (None, None)
    with database(dashboard_settings.database) as conn:
        assert fill_missing_prices(conn, fetch=lambda: CATALOGUE) == ["openai:gpt-9-nova"]
        price = conn.execute("SELECT effective_from,source FROM prices WHERE model='gpt-9-nova'").fetchone()
    assert price[0] == "2026-09-08T10:00:02Z" and price[1].endswith("#openai/gpt-9-nova")
    # usage_values(): 100 uncached, 500 cached, 400 written, 100 output tokens.
    expected = (100 * Decimal(2) + 500 * Decimal("0.2") + 400 * Decimal("2.5") + 100 * Decimal(10)) / 1_000_000
    assert Decimal(usage_cost(dashboard_settings)[1]) == expected


def test_catalogue_is_not_fetched_again_within_interval(dashboard_settings):
    setup_model(dashboard_settings, "gpt-9-unlisted")
    ingest(dashboard_settings)
    calls = []

    def fetch():
        calls.append(1)
        return CATALOGUE

    with database(dashboard_settings.database) as conn:
        assert fill_missing_prices(conn, fetch=fetch) == []
        assert fill_missing_prices(conn, fetch=fetch) == []
        assert fill_missing_prices(conn, force=True, fetch=fetch) == []
    assert len(calls) == 2


def test_known_model_never_triggers_fetch(dashboard_settings):
    setup_model(dashboard_settings, "gpt-5.6-sol")
    ingest(dashboard_settings)

    def fetch():
        raise AssertionError("priced usage must not fetch")

    with database(dashboard_settings.database) as conn:
        assert fill_missing_prices(conn, force=True, fetch=fetch) == []


def test_seeded_builtin_price_reprices_earlier_usage(dashboard_settings):
    setup_model(dashboard_settings, "gpt-5.6-sol")
    ingest(dashboard_settings)
    with database(dashboard_settings.database) as conn:
        # Simulate usage ingested before this model's built-in price existed.
        conn.execute("UPDATE usage SET price_id=NULL,cost_usd=NULL")
    ingest_all(dashboard_settings)
    assert usage_cost(dashboard_settings)[1] is not None


def test_ingest_all_stays_offline_when_disabled(dashboard_settings, monkeypatch):
    setup_model(dashboard_settings, "gpt-9-nova")

    def fetch(*_args, **_kwargs):
        raise AssertionError("network fetch with SPENDA_PRICE_FETCH=0")

    monkeypatch.setattr("spenda.price_sources.fetch_models_dev", fetch)
    summary = ingest_all(dashboard_settings)
    assert summary.unknown_prices == {"openai:gpt-9-nova"}
    assert usage_cost(dashboard_settings) == (None, None)


def test_price_row_reads_fast_mode_multiplier():
    catalogue = {"openai": {"models": {"gpt-9-fast": {"cost": {"input": 5, "output": 30}, "experimental": {
        "modes": {"fast": {"cost": {"input": 12.5, "output": 75}}}}}}}}
    assert price_row(catalogue, "openai", "gpt-9-fast", "2026-09-08T00:00:00Z")["priority"] == "2.5"
    assert price_row(CATALOGUE, "openai", "gpt-9-nova", "2026-09-08T00:00:00Z")["priority"] is None


def test_newly_seen_model_is_looked_up_without_waiting(dashboard_settings):
    setup_model(dashboard_settings, "gpt-9-unlisted")
    ingest(dashboard_settings)
    with database(dashboard_settings.database) as conn:
        assert fill_missing_prices(conn, fetch=lambda: CATALOGUE) == []
    (dashboard_settings.codex_home / "state_1.sqlite").unlink()
    setup_model(dashboard_settings, "gpt-9-nova", "resp-2")
    ingest(dashboard_settings, force_all=True)
    with database(dashboard_settings.database) as conn:
        assert fill_missing_prices(conn, fetch=lambda: CATALOGUE) == ["openai:gpt-9-nova"]


def test_failed_lookup_is_throttled_and_keeps_repricing(dashboard_settings):
    setup_model(dashboard_settings, "gpt-9-nova")
    ingest(dashboard_settings)
    calls = []

    def offline():
        calls.append(1)
        raise OSError("offline")

    for _ in range(2):
        with database(dashboard_settings.database) as conn:
            assert fill_missing_prices(conn, fetch=offline) == []
    assert len(calls) == 1


def test_fetched_price_does_not_override_earlier_manual_interval(dashboard_settings):
    path = dashboard_settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-root.jsonl"
    write_rollout(path, [
        session_meta("root"), turn("turn", "gpt-9-nova"),
        atomic("root", "turn", "morning", timestamp="2026-09-08T09:00:00Z"),
        atomic("root", "turn", "evening", ordinal=3, timestamp="2026-09-08T18:00:00Z"),
    ])
    make_state(dashboard_settings.codex_home, [thread("root", path, model="gpt-9-nova", agent_path="/root")])
    ingest(dashboard_settings)
    with database(dashboard_settings.database) as conn:
        add_price(
            conn, model="gpt-9-nova", effective_from="2026-09-01T00:00:00Z", effective_until="2026-09-08T12:00:00Z",
            input_per_million="1", cached_input_per_million="0.1", cache_write_per_million="1",
            output_per_million="5", source="manual",
        )
        reprice_usage(conn, model="gpt-9-nova")
        morning = conn.execute("SELECT cost_usd FROM usage WHERE response_id='morning'").fetchone()[0]
        assert fill_missing_prices(conn, fetch=lambda: CATALOGUE) == ["openai:gpt-9-nova"]
        rows = dict(conn.execute("SELECT response_id,cost_usd FROM usage").fetchall())
        start = conn.execute("SELECT effective_from FROM prices WHERE source LIKE '%models.dev%'").fetchone()[0]
    assert rows["morning"] == morning and rows["evening"] is not None
    assert start == "2026-09-08T18:00:00Z"


def test_ingest_summary_counts_spend_priced_after_adapters(dashboard_settings, monkeypatch):
    setup_model(dashboard_settings, "gpt-9-nova")
    monkeypatch.setenv("SPENDA_PRICE_FETCH", "1")
    monkeypatch.setattr("spenda.price_sources.fetch_models_dev", lambda: CATALOGUE)
    summary = ingest_all(dashboard_settings)
    assert summary.fetched_prices == ["openai:gpt-9-nova"]
    assert summary.estimated_spend == float(usage_cost(dashboard_settings)[1]) > 0


def test_fetched_price_covers_later_calls_in_its_first_second(dashboard_settings):
    path = dashboard_settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-root.jsonl"
    write_rollout(path, [
        session_meta("root"), turn("turn", "gpt-9-nova"),
        atomic("root", "turn", "first", timestamp="2026-09-08T10:00:02Z"),
        atomic("root", "turn", "later", ordinal=3, timestamp="2026-09-08T10:00:02.500Z"),
    ])
    make_state(dashboard_settings.codex_home, [thread("root", path, model="gpt-9-nova", agent_path="/root")])
    ingest(dashboard_settings)
    with database(dashboard_settings.database) as conn:
        assert fill_missing_prices(conn, fetch=lambda: CATALOGUE) == ["openai:gpt-9-nova"]
        costs = dict(conn.execute("SELECT response_id,cost_usd FROM usage").fetchall())
    assert costs["first"] is not None and costs["later"] is not None


def test_unpriced_fast_calls_stay_in_unknown_prices_after_fetch(dashboard_settings, monkeypatch):
    from test_service_tier import settings_applied

    path = dashboard_settings.codex_home / "sessions" / "2026" / "09" / "08" / "rollout-root.jsonl"
    write_rollout(path, [
        session_meta("root"), settings_applied("priority"), turn("turn", "gpt-9-nova", 2),
        atomic("root", "turn", "fast", ordinal=3),
    ])
    make_state(dashboard_settings.codex_home, [thread("root", path, model="gpt-9-nova", agent_path="/root")])
    monkeypatch.setenv("SPENDA_PRICE_FETCH", "1")
    # The catalogue lists gpt-9-nova without a Fast-mode price.
    monkeypatch.setattr("spenda.price_sources.fetch_models_dev", lambda: CATALOGUE)
    summary = ingest_all(dashboard_settings)
    assert summary.fetched_prices == ["openai:gpt-9-nova"]
    assert usage_cost(dashboard_settings)[1] is None
    assert "openai:gpt-9-nova" in summary.unknown_prices
