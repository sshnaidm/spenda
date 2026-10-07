from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..config import Settings
from ..db import database
from ..price_sources import fetch_enabled, fill_missing_prices, still_unpriced
from .claude import ClaudeIngestSummary, discover_claude_home, ingest_claude
from .cursor import CursorIngestSummary, cursor_sources_present, ingest_cursor
from .opencode import OpenCodeIngestSummary, ingest_opencode
from .scanner import IngestSummary
from .scanner import ingest as ingest_codex

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CombinedIngestSummary:
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
    recorded_spend: float = 0.0
    codex: IngestSummary | None = None
    opencode: OpenCodeIngestSummary | None = None
    claude: ClaudeIngestSummary | None = None
    cursor: CursorIngestSummary | None = None
    source_errors: dict[str, str] = field(default_factory=dict)
    fetched_prices: list[str] = field(default_factory=list)


def ingest_all(settings: Settings, *, force_all: bool = False) -> CombinedIngestSummary:
    """Refresh every configured local source into the shared normalized ledger."""

    settings.validate()
    errors: dict[str, str] = {}

    def attempt(source: str, operation):
        try:
            return operation()
        except Exception as exc:
            errors[source] = f"{type(exc).__name__}: {exc}"
            log.error("%s ingestion failed: %s", source, errors[source])
            return None

    codex = attempt("codex", lambda: ingest_codex(settings, force_all=force_all))
    opencode = None
    if settings.opencode_database.is_file():
        opencode = attempt("opencode", lambda: ingest_opencode(settings, force_all=force_all))

    claude = None
    if discover_claude_home(settings).is_dir():
        claude = attempt("claude", lambda: ingest_claude(settings, force_all=force_all))

    cursor = None
    if cursor_sources_present(settings):
        cursor = attempt("cursor", lambda: ingest_cursor(settings, force_all=force_all))

    fetched: list[str] = []
    resolved: set[str] = set()
    repriced_spend = 0.0
    if settings.database.exists():
        try:
            with database(settings.database) as conn:
                total = "SELECT COALESCE(SUM(CAST(cost_usd AS REAL)),0) FROM usage"
                before = conn.execute(total).fetchone()[0]
                fetched = fill_missing_prices(conn, fetch_remote=fetch_enabled())
                # A fetched standard price can leave Fast-mode calls unpriced.
                resolved = set(fetched) - still_unpriced(conn, fetched)
                # Calls priced after their adapter ran were counted as $0 in its summary.
                repriced_spend = conn.execute(total).fetchone()[0] - before
        except Exception as exc:
            # Prices are refreshed on a later pass; an outage is not an ingestion failure.
            log.warning("models.dev price lookup failed: %s: %s", type(exc).__name__, exc)

    summaries = [item for item in (codex, opencode, claude, cursor) if item is not None]
    return CombinedIngestSummary(
        scanned_files=sum(item.scanned_files for item in summaries),
        root_sessions=sum(item.root_sessions for item in summaries),
        subagent_sessions=sum(item.subagent_sessions for item in summaries),
        usage_records=sum(item.usage_records for item in summaries),
        duplicate_records=sum(item.duplicate_records for item in summaries),
        malformed_lines=sum(item.malformed_lines for item in summaries),
        parser_warnings=sum(item.parser_warnings for item in summaries),
        unknown_models=set().union(*(item.unknown_models for item in summaries)),
        unknown_prices=set().union(*(item.unknown_prices for item in summaries)) - resolved,
        estimated_spend=sum(item.estimated_spend for item in summaries) + repriced_spend,
        recorded_spend=sum(getattr(item, "recorded_spend", 0.0) for item in summaries),
        codex=codex,
        opencode=opencode,
        claude=claude,
        cursor=cursor,
        source_errors=errors,
        fetched_prices=fetched,
    )
