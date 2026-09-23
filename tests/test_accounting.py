from __future__ import annotations

from decimal import Decimal

from spenda.db import database, initialize
from spenda.models import TokenUsage
from spenda.pricing import add_price, calculate_cost, estimate_cost, price_model, seed_prices


def priced_conn(tmp_path):
    path = tmp_path / "price.sqlite"
    initialize(path)
    return path


def test_cached_and_reasoning_semantics():
    usage = TokenUsage(1000, 600, 300, 200, 150, 1200)
    assert usage.uncached_input_tokens == 100
    assert usage.total_tokens == 1200
    assert usage.reasoning_output_tokens <= usage.output_tokens


def test_one_root_one_model_cost(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    with database(path) as conn:
        seed_prices(conn)
        usage = TokenUsage(100_000, 50_000, 40_000, 10_000, 5_000, 110_000)
        cost = calculate_cost(conn, usage, "gpt-5.6-sol", "openai", "2026-09-08T10:00:00Z")
    # 10k*4 + 50k*.4 + 40k*5 + 10k*20, all per million.
    assert cost.total_usd == Decimal("0.46")


def test_reasoning_is_not_charged_twice(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    with database(path) as conn:
        seed_prices(conn)
        a = calculate_cost(conn, TokenUsage(0, 0, 0, 1000, 900, 1000), "gpt-5.6-luna", "openai", "2026-09-08T10:00:00Z")
        b = calculate_cost(conn, TokenUsage(0, 0, 0, 1000, 0, 1000), "gpt-5.6-luna", "openai", "2026-09-08T10:00:00Z")
    assert a.total_usd == b.total_usd == Decimal("0.0012")


def test_long_context_multiplier(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    with database(path) as conn:
        seed_prices(conn)
        usage = TokenUsage(272001, 0, 0, 1000, 0, 273001)
        cost = calculate_cost(conn, usage, "gpt-5.6-sol", "openai", "2026-09-08T10:00:00Z")
    assert cost.uncached_input_usd == Decimal(272001) * Decimal(8) / Decimal(1_000_000)
    assert cost.output_usd == Decimal("0.03")


def test_unknown_model_and_provider(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    with database(path) as conn:
        seed_prices(conn)
        unknown = calculate_cost(conn, TokenUsage(input_tokens=10), "future-model", "openai", "2026-09-08T10:00:00Z")
        azure = calculate_cost(conn, TokenUsage(input_tokens=10), "gpt-5.6-sol", "azure", "2026-09-08T10:00:00Z")
    assert unknown.total_usd is None and azure.total_usd is None


def test_explicit_alias(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    with database(path) as conn:
        seed_prices(conn)
        cost = calculate_cost(conn, TokenUsage(input_tokens=1000), "gpt-5.6", "openai", "2026-09-08T10:00:00Z")
    assert cost.total_usd is not None


def test_historical_price_change(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    with database(path) as conn:
        add_price(conn, model="test", effective_from="2026-01-01T00:00:00Z", input_per_million="1",
                  cached_input_per_million="1", cache_write_per_million="1", output_per_million="1", source="test")
        add_price(conn, model="test", effective_from="2026-06-01T00:00:00Z", input_per_million="2",
                  cached_input_per_million="2", cache_write_per_million="2", output_per_million="2", source="test")
        old = calculate_cost(conn, TokenUsage(input_tokens=1_000_000), "test", "openai", "2026-03-01T00:00:00Z")
        new = calculate_cost(conn, TokenUsage(input_tokens=1_000_000), "test", "openai", "2026-07-01T00:00:00Z")
    assert old.total_usd == Decimal("1")
    assert new.total_usd == Decimal("2")


def test_builtin_anthropic_prices_and_aliases(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    stamp = "2026-09-14T00:00:00Z"
    with database(path) as conn:
        seed_prices(conn)
        # 100k uncached * 5 + 1M cache read * 0.5 + 100k cache write * 6.25 + 10k output * 25, per MTok.
        opus = calculate_cost(
            conn, TokenUsage(1_200_000, 1_000_000, 100_000, 10_000, 0, 1_210_000), "claude-opus-4-8", "anthropic", stamp
        )
        haiku = calculate_cost(
            conn, TokenUsage(2455, 0, 0, 50, 0, 2505), "claude-haiku-4-5-20251001", "anthropic", stamp
        )
        vertex_alias = calculate_cost(
            conn, TokenUsage(1_000_000, 0, 0, 0, 0, 1_000_000), "claude-sonnet-4-5@20250929", "anthropic", stamp
        )
        fable = calculate_cost(
            conn, TokenUsage(1_000_000, 1_000_000, 0, 0, 0, 1_000_000), "claude-fable-5-1", "anthropic", stamp
        )
        openai = calculate_cost(conn, TokenUsage(1_000_000, 0, 0, 0, 0, 1_000_000), "claude-opus-5", "openai", stamp)
    assert opus.total_usd == Decimal("1.875")
    # Matches the haiku entry Claude Code wrote into a real cost-state ($0.002705).
    assert haiku.total_usd == Decimal("0.002705")
    assert vertex_alias.total_usd == Decimal("3")
    assert fable.cached_input_usd == Decimal("0.25")
    assert openai.total_usd is None


def test_estimate_cost_uplifts_one_hour_cache_writes_and_strips_context_suffix(tmp_path):
    path = tmp_path / "db.sqlite"
    initialize(path)
    with database(path) as conn:
        seed_prices(conn)
        usage = TokenUsage(4000, 0, 4000, 0, 0, 4000)
        plain = estimate_cost(conn, usage, "claude-opus-5[1m]", "anthropic", "2026-09-14T00:00:00Z")
        uplifted = estimate_cost(
            conn, usage, "claude-opus-5[1m]", "anthropic", "2026-09-14T00:00:00Z", cache_write_1h_tokens=4000
        )
        unknown = estimate_cost(
            conn, usage, "claude-unknown", "anthropic", "2026-09-14T00:00:00Z", cache_write_1h_tokens=4000
        )
    # 4000 tokens at the 5m rate (6.25) = 0.025; at the 1h rate (10) = 0.04.
    assert plain.total_usd == Decimal("0.025") and plain.note is None
    assert uplifted.cache_write_usd == uplifted.total_usd == Decimal("0.04")
    assert uplifted.note == "1h cache-write uplift applied to 4000 tokens"
    assert unknown.total_usd is None
    assert price_model("claude-opus-5[1m]") == "claude-opus-5" and price_model("claude-opus-5") == "claude-opus-5"
    assert price_model("anthropic.claude-opus-4-6-v1:0") == "claude-opus-4-6"
    assert price_model("us.anthropic.claude-opus-4-6-v1") == "claude-opus-4-6"
    assert price_model("global.anthropic.claude-opus-4-5-20251101-v1:0") == "claude-opus-4-5"
    assert price_model("claude-haiku-4-5@20251001") == "claude-haiku-4-5"
    assert price_model("unrelated-v1") == "unrelated-v1"
