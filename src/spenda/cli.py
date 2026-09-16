from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from . import __version__
from .config import CLAUDE_BILLING_MODES, Settings
from .db import database, initialize
from .ingestion.claude import claude_auth_profile, discover_claude_home
from .ingestion.claude_auth import BACKEND_LABELS, BACKENDS
from .ingestion.codex_state import read_state
from .ingestion.opencode import discover_opencode_database
from .ingestion.scanner import discover_rollouts
from .ingestion.service import ingest_all as ingest
from .pricing import add_price, reprice_usage, seed_prices
from .reports import as_dict, cost_sql, format_cost, format_tokens, iso_date, session_rows

log = logging.getLogger("spenda")
SOURCE_CHOICES = ("all", "codex", "opencode", "claude")
BACKEND_CHOICES = ("all", *BACKENDS)


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-home", help="Codex state directory (default: CODEX_HOME or ~/.codex)")
    parser.add_argument("--opencode-db", help="OpenCode SQLite database (default: OPENCODE_DB or XDG data path)")
    parser.add_argument("--claude-home", help="Claude Code directory (default: CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument(
        "--claude-billing", choices=CLAUDE_BILLING_MODES,
        help="How Anthropic-direct Claude calls are billed (default: SPENDA_CLAUDE_BILLING or auto-detect)",
    )
    parser.add_argument("--database", help="Dashboard-owned SQLite database")
    parser.add_argument("--no-preview", action="store_true", help="Do not persist first-message previews")


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", choices=SOURCE_CHOICES, default="all")
    parser.add_argument("--backend", choices=BACKEND_CHOICES, default="all", help="Claude Code API backend")
    parser.add_argument(
        "--include-subscription", action="store_true",
        help="Add the equivalent API value of claude.ai subscription usage to cost totals",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spenda")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("doctor", "Inspect coding-agent and dashboard data sources"),
        ("sessions", "List normalized task sessions"),
        ("prices", "List historical model prices"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        _add_config(cmd)
    sessions = sub.choices["sessions"]
    sessions.add_argument("--limit", type=int, default=30)
    sessions.add_argument("--sort", choices=("time", "cost", "tokens", "duration", "agents"), default="time")
    _add_filters(sessions)

    ingest_p = sub.add_parser("ingest", help="Ingest new or changed coding-agent state")
    _add_config(ingest_p)
    ingest_p.add_argument("--all", action="store_true", help="Rescan complete files; deduplication remains active")

    watch = sub.add_parser("watch", help="Continuously ingest coding-agent state")
    _add_config(watch)
    watch.add_argument("--interval", type=float, default=10)

    serve = sub.add_parser("serve", help="Serve the local dashboard")
    _add_config(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--interval", type=float, default=10)

    export = sub.add_parser("export", help="Export normalized session accounting")
    _add_config(export)
    export.add_argument("--format", choices=("csv", "json"), required=True)
    export.add_argument("--breakdown", choices=("sessions", "models"), default="sessions")
    _add_filters(export)
    export.add_argument("--output", "-o", default="-")

    rebuild = sub.add_parser("rebuild", help="Reimport derived data while preserving dashboard metadata")
    _add_config(rebuild)
    rebuild.add_argument("--yes", action="store_true", help="Confirm dashboard database deletion")

    price = sub.add_parser("price-add", help="Add a historical price row")
    _add_config(price)
    price.add_argument("model")
    price.add_argument("effective_from")
    price.add_argument("--provider", default="openai")
    price.add_argument("--input", required=True)
    price.add_argument("--cached-input", required=True)
    price.add_argument("--cache-write", required=True)
    price.add_argument("--output-price", required=True)
    price.add_argument("--effective-until")
    price.add_argument("--source", required=True)
    price.add_argument("--notes")

    tag = sub.add_parser("tag", help="Replace tags on a dashboard session")
    _add_config(tag)
    tag.add_argument("session_id")
    tag.add_argument("tags", nargs="*", help="Tag names")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(
        args.codex_home, args.database, not args.no_preview,
        opencode_database=args.opencode_db,
        claude_home=args.claude_home,
        claude_billing=args.claude_billing,
    )


def _prepare(settings: Settings) -> None:
    settings.validate()
    initialize(settings.database)
    with database(settings.database) as conn:
        seed_prices(conn)


def _print_ingest(summary) -> None:
    print(f"Scanned: {summary.scanned_files} source files")
    print(f"Root sessions: {summary.root_sessions}")
    print(f"Subagent sessions: {summary.subagent_sessions}")
    print(f"Usage records added: {summary.usage_records}")
    print(f"Duplicate records ignored: {summary.duplicate_records}")
    print(f"Unknown models: {', '.join(sorted(summary.unknown_models)) or '0'}")
    print(f"Unknown prices: {', '.join(sorted(summary.unknown_prices)) or '0'}")
    print(f"Estimated spend added: ${summary.estimated_spend:.4f}")
    claude = getattr(summary, "claude", summary)
    if getattr(claude, "subscription_value", 0):
        print(f"Subscription equivalent value added: ${claude.subscription_value:.4f}")
    if getattr(claude, "backends", None):
        print("Claude backends: " + ", ".join(
            f"{name}={count}" for name, count in sorted(claude.backends.items())
        ))
    if summary.parser_warnings or summary.malformed_lines:
        print(f"Parser warnings: {summary.parser_warnings}; malformed lines: {summary.malformed_lines}")
    for source, error in getattr(summary, "source_errors", {}).items():
        print(f"{source} ingestion failed: {error}", file=sys.stderr)


def _codex_version() -> str:
    try:
        result = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=5, check=False
        )
        return (result.stdout or result.stderr).strip().splitlines()[-1]
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def _auth_mode(codex_home: Path) -> str:
    try:
        data = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
        value = data.get("auth_mode")
        return value if isinstance(value, str) else "unknown"
    except (OSError, json.JSONDecodeError):
        return "unknown"


def doctor(settings: Settings) -> int:
    _prepare(settings)
    snapshot = read_state(settings.codex_home)
    rollouts = discover_rollouts(settings.codex_home)
    roots = len(snapshot.threads) - len(snapshot.edges)
    models = sorted({str(t.get("model")) for t in snapshot.threads if t.get("model")})
    print(f"Codex home: {settings.codex_home}")
    print(f"Codex version: {_codex_version()}")
    auth_mode = _auth_mode(settings.codex_home)
    print(f"Codex auth mode: {auth_mode}")
    if auth_mode not in {"apikey", "unknown"}:
        print("Auth warning: this MVP estimates API-key usage only; ChatGPT quota is not implemented")
    print(f"State database: {snapshot.state_path or 'not found'}")
    print(f"State schema version: {snapshot.schema_version if snapshot.schema_version is not None else 'unknown'}")
    print(f"Session directory: {settings.codex_home / 'sessions'}")
    print(f"Rollout files: {len(rollouts)}")
    if rollouts:
        print(f"Earliest/latest rollout: {rollouts[0].name} / {rollouts[-1].name}")
    print(f"State DB root/child threads: {roots} / {len(snapshot.edges)}")
    print(f"Models encountered: {', '.join(models) or 'none'}")
    opencode_path = discover_opencode_database(settings)
    print(f"OpenCode database: {opencode_path}")
    if opencode_path.is_file():
        try:
            with closing(sqlite3.connect(
                f"{opencode_path.as_uri()}?mode=ro", uri=True, timeout=0.2
            )) as source:
                source.execute("PRAGMA query_only=ON")
                opencode_sessions = source.execute("SELECT COUNT(*) FROM session").fetchone()[0]
                opencode_version = source.execute("SELECT MAX(version) FROM session").fetchone()[0]
            print(f"OpenCode version: {opencode_version or 'unknown'}")
            print(f"OpenCode sessions: {opencode_sessions}")
        except sqlite3.DatabaseError as exc:
            print(f"OpenCode source warning: {exc}")
    else:
        print("OpenCode source: not found (skipped)")
    claude_home = discover_claude_home(settings)
    print(f"Claude Code directory: {claude_home}")
    projects = claude_home / "projects"
    if projects.is_dir():
        transcripts = sum(1 for _ in projects.rglob("*.jsonl"))
        print(f"Claude Code transcripts: {transcripts}")
    else:
        print("Claude Code source: not found (skipped)")
    profile = claude_auth_profile(settings)
    print(f"Claude auth profile: {profile.config_path or 'not found'}")
    print(f"Claude login: {profile.login}")
    print(
        f"Claude backend hints: vertex={str(profile.vertex_hint).lower()} "
        f"bedrock={str(profile.bedrock_hint).lower()} api-key={str(profile.api_key_hint).lower()}"
    )
    print(
        f"Claude Anthropic-direct calls billed as: {BACKEND_LABELS[profile.anthropic_backend]}"
        f" ({'--claude-billing ' + profile.override if profile.override != 'auto' else 'auto-detected'})"
    )
    if profile.ambiguous:
        print(
            "Auth warning: both a claude.ai login and an API key were found; msg_ calls are recorded as "
            "subscription usage. Pass --claude-billing api if this machine pays per token."
        )
    with database(settings.database) as conn:
        missing = conn.execute(
            "SELECT DISTINCT provider||':'||model FROM usage WHERE cost_usd IS NULL "
            "OR (billing_mode='subscription' AND equivalent_cost_usd IS NULL) ORDER BY 1"
        ).fetchall()
        warnings = conn.execute("SELECT COUNT(*) FROM parser_warnings").fetchone()[0]
        resolved = conn.execute(
            "SELECT SUM(parent_thread_id IS NULL),SUM(parent_thread_id IS NOT NULL) FROM agents"
        ).fetchone()
        orphans = conn.execute("SELECT COUNT(*) FROM agents WHERE orphan=1").fetchone()[0]
        incomplete = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE accounting_status NOT IN ('complete','estimated')"
        ).fetchone()[0]
        estimated = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE accounting_status='estimated'"
        ).fetchone()[0]
        by_backend = conn.execute(
            "SELECT backend,COUNT(*) FROM usage WHERE backend IS NOT NULL GROUP BY backend ORDER BY backend"
        ).fetchall()
    print(f"Dashboard resolved root/child agents: {int(resolved[0] or 0)} / {int(resolved[1] or 0)}")
    print(f"Orphan subagents: {orphans}")
    print(f"Sessions with incomplete accounting: {incomplete}")
    print(f"Claude sessions priced from list prices: {estimated}")
    print("Claude usage by backend: " + (", ".join(f"{row[0]}={row[1]}" for row in by_backend) or "none"))
    print(f"Models without prices: {', '.join(r[0] for r in missing) or 'none'}")
    print(f"Parser warnings: {warnings}")
    print(f"Dashboard database: {settings.database}")
    if snapshot.warning:
        print(f"State warning: {snapshot.warning}")
    return 0


def _session_filter(source: str, backend: str) -> tuple[str, tuple]:
    clauses, params = [], []
    if source != "all":
        clauses.append("s.source_app=?")
        params.append(source)
    if backend != "all":
        clauses.append("EXISTS(SELECT 1 FROM usage ub WHERE ub.session_id=s.id AND ub.backend=?)")
        params.append(backend)
    return " AND ".join(clauses) or "1=1", tuple(params)


def sessions_command(
    settings: Settings, limit: int, sort: str, source: str = "all", backend: str = "all",
    include_subscription: bool = False,
) -> int:
    _prepare(settings)
    with database(settings.database, readonly=True) as conn:
        where, params = _session_filter(source, backend)
        rows = session_rows(
            conn, where=where, params=params, order=sort, limit=limit,
            include_subscription=include_subscription,
        )
    print(
        f"{'Started':16}  {'Source':8} {'Backend':16} {'Task':38}  {'Project':20}  "
        f"{'Models':24} {'Agents':>6} {'Tokens':>10} {'Cost':>16} {'Sub. value':>12}"
    )
    for row in rows:
        title = (row["title"] or "(untitled)")[:38]
        project = (row["repo_name"] or row["cwd"] or "unknown")[-20:]
        models = (row["models_used"] or row["root_model"] or "unknown")[:24]
        backend_label = (row["root_backend"] or "-")[:16]
        cost = format_cost(row["known_cost_usd"], row["unknown_cost_records"])
        if row["accounting_status"] == "estimated":
            cost += "*"
        value = (
            format_cost(row["equivalent_cost_usd"], row["unknown_value_records"])
            if row["equivalent_cost_usd"] is not None or row["unknown_value_records"] else "-"
        )
        print(
            f"{iso_date(row['created_at']):16}  {row['source_app']:8} {backend_label:16} {title:38}  "
            f"{project:20}  {models:24} {row['agent_count']:6d} {format_tokens(row['total_tokens']):>10} "
            f"{cost:>16} {value:>12}"
        )
    if any(row["accounting_status"] == "estimated" for row in rows):
        print("* estimated from built-in list prices (no Claude Code cost-state)")
    return 0


SESSION_EXPORT_FIELDS = (
    "session_id", "source_app", "root_backend", "date", "title", "project", "root_model", "models_used",
    "agent_count", "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "uncached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "cost_usd",
    "known_cost_usd", "unknown_cost_records", "equivalent_cost_usd", "unknown_value_records",
    "accounting_status", "duration_seconds", "tags",
)


def _export_rows(
    conn, breakdown: str, source: str = "all", backend: str = "all", include_subscription: bool = False,
) -> list[dict]:
    cost_column, unknown = cost_sql(include_subscription)

    def exact_cost(where: str, params: tuple, column: str = "cost_usd") -> str | None:
        values = conn.execute(f"SELECT {column} FROM usage u WHERE {where} AND {column} IS NOT NULL", params)
        total = Decimal("0")
        seen = False
        for value, in values:
            total += Decimal(value)
            seen = True
        return format(total, "f") if seen else None

    def exact_total(where: str, params: tuple) -> str | None:
        real = exact_cost(where, params)
        if not include_subscription:
            return real
        value = exact_cost(where, params, "equivalent_cost_usd")
        if real is None and value is None:
            return None
        return format(Decimal(real or "0") + Decimal(value or "0"), "f")

    if breakdown == "models":
        where, params = _session_filter(source, backend)
        rows = conn.execute(
            f"""SELECT u.session_id,s.source_app,u.model,u.provider,u.backend,u.billing_mode,
               COUNT(DISTINCT u.thread_id) agent_count,
               SUM(source_event_type!='claude_cost_state') usage_events,
               SUM(input_tokens) input_tokens,SUM(cached_input_tokens) cached_input_tokens,
               SUM(cache_write_input_tokens) cache_write_input_tokens,SUM(uncached_input_tokens) uncached_input_tokens,
               SUM(output_tokens) output_tokens,SUM(reasoning_output_tokens) reasoning_tokens,
               SUM(total_tokens) total_tokens,SUM({cost_column}) known_cost_usd,
               SUM({unknown}) unknown_cost_records,
               SUM(CAST(u.equivalent_cost_usd AS REAL)) equivalent_cost_usd
               FROM usage u JOIN sessions s ON s.id=u.session_id
               WHERE {where} GROUP BY u.session_id,s.source_app,u.model,u.provider,u.backend,u.billing_mode
               ORDER BY u.session_id,u.model""",
            params,
        ).fetchall()
        output = []
        for row in rows:
            data = dict(row)
            group = "session_id=? AND model=? AND provider=? AND backend IS ? AND billing_mode=?"
            group_params = (
                data["session_id"], data["model"], data["provider"], data["backend"], data["billing_mode"],
            )
            data["known_cost_usd"] = exact_total(group, group_params)
            data["equivalent_cost_usd"] = exact_cost(group, group_params, "equivalent_cost_usd")
            output.append(data)
        return output
    output = []
    session_where, session_params = _session_filter(source, backend)
    for row in session_rows(
        conn, where=session_where, params=session_params, include_subscription=include_subscription
    ):
        data = as_dict(row)
        known_exact = exact_total("session_id=?", (data["id"],))
        output.append({
            "session_id": data["id"], "source_app": data["source_app"], "root_backend": data["root_backend"],
            "date": data["created_at"], "title": data["title"],
            "project": data["repo_name"] or data["cwd"], "root_model": data["root_model"],
            "models_used": data["models_used"], "agent_count": data["agent_count"],
            "input_tokens": data["input_tokens"], "cached_input_tokens": data["cached_input_tokens"],
            "cache_write_input_tokens": data["cache_write_input_tokens"],
            "uncached_input_tokens": data["uncached_input_tokens"], "output_tokens": data["output_tokens"],
            "reasoning_tokens": data["reasoning_tokens"], "total_tokens": data["total_tokens"],
            "cost_usd": None if data["unknown_cost_records"] else known_exact,
            "known_cost_usd": known_exact,
            "unknown_cost_records": data["unknown_cost_records"],
            "equivalent_cost_usd": exact_cost("session_id=?", (data["id"],), "equivalent_cost_usd"),
            "unknown_value_records": data["unknown_value_records"],
            "accounting_status": data["accounting_status"],
            "duration_seconds": data["duration_seconds"],
            "tags": data["tags"],
        })
    return output


def export_command(
    settings: Settings, fmt: str, breakdown: str, output_path: str, source: str = "all",
    backend: str = "all", include_subscription: bool = False,
) -> int:
    _prepare(settings)
    with database(settings.database, readonly=True) as conn:
        rows = _export_rows(conn, breakdown, source, backend, include_subscription)
    handle = sys.stdout if output_path == "-" else open(output_path, "w", newline="", encoding="utf-8")
    try:
        if fmt == "json":
            json.dump(rows, handle, indent=2)
            handle.write("\n")
        else:
            fields = list(rows[0]) if rows else list(SESSION_EXPORT_FIELDS)
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    finally:
        if handle is not sys.stdout:
            handle.close()
    return 0


def rebuild(settings: Settings, confirmed: bool) -> int:
    if not confirmed:
        answer = input(
            f"Clear imported data and rebuild dashboard database {settings.database}? [y/N] "
        ).strip().lower()
        confirmed = answer in {"y", "yes"}
    if not confirmed:
        print("Cancelled.")
        return 1
    try:
        settings.validate()
    except ValueError as exc:
        print(f"Refusing: {exc}", file=sys.stderr)
        return 2
    initialize(settings.database)
    with database(settings.database) as conn:
        saved_tags = conn.execute(
            """SELECT st.session_id,t.name FROM session_tags st
               JOIN tags t ON t.id=st.tag_id"""
        ).fetchall()
        conn.execute("DELETE FROM usage")
        conn.execute("DELETE FROM agents")
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM ingestion_state")
        conn.execute("DELETE FROM source_sync_state")
        conn.execute("DELETE FROM parser_warnings")
    summary = ingest(settings, force_all=True)
    _print_ingest(summary)
    with database(settings.database) as conn:
        for session_id, tag_name in saved_tags:
            conn.execute(
                """INSERT OR IGNORE INTO session_tags(session_id,tag_id)
                   SELECT ?,id FROM tags WHERE name=? AND EXISTS(
                     SELECT 1 FROM sessions WHERE id=?)""",
                (session_id, tag_name, session_id),
            )
    return 1 if summary.source_errors else 0


def tag_command(settings: Settings, session_id: str, names: list[str]) -> int:
    _prepare(settings)
    cleaned = sorted({name.strip() for name in names if name.strip()})
    with database(settings.database) as conn:
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            print("Unknown session", file=sys.stderr)
            return 2
        conn.execute("DELETE FROM session_tags WHERE session_id=?", (session_id,))
        for name in cleaned:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
            conn.execute(
                "INSERT INTO session_tags(session_id,tag_id) SELECT ?,id FROM tags WHERE name=?",
                (session_id, name),
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = _settings(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        return doctor(settings)
    if args.command == "ingest":
        summary = ingest(settings, force_all=args.all)
        _print_ingest(summary)
        return 1 if summary.source_errors else 0
    if args.command == "sessions":
        return sessions_command(
            settings, args.limit, args.sort, args.source, args.backend, args.include_subscription
        )
    if args.command == "prices":
        _prepare(settings)
        with database(settings.database, readonly=True) as conn:
            rows = conn.execute("SELECT * FROM prices ORDER BY model,effective_from").fetchall()
        for row in rows:
            until = row["effective_until"] or "present"
            print(
                f"{row['model']:18} {row['provider']:8} {row['effective_from']}..{until} "
                f"in={row['input_per_million']} cached={row['cached_input_per_million']} "
                f"write={row['cache_write_per_million']} out={row['output_per_million']} USD/MTok"
            )
        return 0
    if args.command == "price-add":
        _prepare(settings)
        with database(settings.database) as conn:
            add_price(
                conn, model=args.model, provider=args.provider, effective_from=args.effective_from,
                effective_until=args.effective_until, input_per_million=args.input,
                cached_input_per_million=args.cached_input, cache_write_per_million=args.cache_write,
                output_per_million=args.output_price, source=args.source, notes=args.notes,
            )
            updated = reprice_usage(conn, provider=args.provider)
        print(f"Repriced usage records: {updated}")
        return 0
    if args.command == "watch":
        try:
            while True:
                _print_ingest(ingest(settings))
                time.sleep(max(1, args.interval))
        except KeyboardInterrupt:
            return 0
    if args.command == "serve":
        import uvicorn

        from .web.app import create_app
        uvicorn.run(create_app(settings, ingest_interval=args.interval), host=args.host, port=args.port)
        return 0
    if args.command == "export":
        return export_command(
            settings, args.format, args.breakdown, args.output, args.source, args.backend,
            args.include_subscription,
        )
    if args.command == "rebuild":
        return rebuild(settings, args.yes)
    if args.command == "tag":
        return tag_command(settings, args.session_id, args.tags)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
