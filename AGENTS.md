# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, and others) working on Spenda. Human contributors: [CONTRIBUTING.md](CONTRIBUTING.md) has the same rules in long form, and they apply to agents too.

Spenda is a local dashboard for the token usage and cost of coding agents (Codex, OpenCode, Claude Code, Cursor). It reads each agent's local history read-only, normalizes it into its own SQLite ledger, and serves reports with FastAPI and Jinja templates. Its whole value is **accurate accounting**: a change that makes a total slightly wrong is worse than no change.

## Commands

```bash
uv sync                                  # set up the environment (Python 3.11+)
uv run pytest -q                         # full test suite; CI does NOT run tests, so run them yourself
scripts/lint.sh                          # ruff + flake8, 120-character lines; CI runs this
scripts/ruff.sh --fix                    # apply safe ruff autofixes
uv run spenda --help                     # CLI: ingest, watch, serve, doctor, sessions, export, prices, ...
```

Run the tests and linters before you call a change done, and include new tests for new behavior and its failure cases.

## Where things are

| Path | What it holds |
|---|---|
| `src/spenda/ingestion/scanner.py`, `rollout.py`, `codex_state.py` | Codex: rollout JSONL parser and state database |
| `src/spenda/ingestion/claude.py`, `claude_auth.py` | Claude Code transcripts, cost-state, API backend and billing detection |
| `src/spenda/ingestion/opencode.py`, `cursor.py` | OpenCode and Cursor adapters |
| `src/spenda/ingestion/service.py` | `ingest_all`: runs every adapter, then fills missing prices |
| `src/spenda/pricing.py` | Built-in effective-dated prices, cost formulas, repricing |
| `src/spenda/price_sources.py` | models.dev fallback for models without a built-in price |
| `src/spenda/db.py` | Schema, `SCHEMA_VERSION`, in-place migrations |
| `src/spenda/reports.py`, `src/spenda/web/` | Aggregations, FastAPI routes, Jinja templates |
| `docs/accounting.md`, `docs/data-sources.md` | The accounting rules and source formats; keep them in sync with code |
| `tests/conftest.py` | Synthetic source fixtures and test isolation |
| `scripts/demo/` | Records `docs/demo.webp` from invented data |

## Accounting rules that are easy to break

- **Source-reported costs win.** OpenCode's per-message cost and Claude Code's cumulative `cost-state` are authoritative and are never repriced (see `repriceable_clauses()` in `pricing.py`). Calls a cost-state covers are stored at `$0` with a "covered" note, so the dollars are counted once, on the cost-state row.
- **Estimates are marked.** Calls without a reported cost are priced from list prices and carry `price_id` and an explanatory `pricing_note`; the UI shows them as `est.`. If a price cannot be established (an unknown model, a Fast-mode call without a Fast price, Claude calls whose billing cannot be classified as API or subscription), leave the cost `NULL` and say why in the note. Never store `0` for "unknown".
- **Subscription usage is not spend.** `billing_mode='subscription'` rows have `cost_usd='0'` and keep the list-price value in `equivalent_cost_usd`. Do not change subscription handling as a side effect of other work.
- **Prices are effective-dated.** Add a new row in `BUILTIN_PRICES` with a source URL and notes rather than editing an old row's numbers; old estimates must stay reproducible. Fast-mode pricing is the row's `priority` multiplier.
- **Compare timestamps as instants, not strings.** `"...10:00:02Z"` sorts after `"...10:00:02.500Z"` as text. Use `timestamp_instant()` / `utc_timestamp()` from `pricing.py`.
- **Token categories are subsets as documented.** Cached input and cache writes are parts of input; reasoning is part of output. Check `docs/accounting.md` before adding a column.

## Changing stored data

- **New or changed column:** add it to `SCHEMA` and to the migration in `db.py`, bump `SCHEMA_VERSION`, and update the test that asserts the schema version.
- **Changed parsing or derived values:** bump the adapter's `PARSER_VERSION` (in `scanner.py`, `claude.py`, `cursor.py`, or `opencode.py`) so stored sources are reread. Make the reread backfill rows that already exist; duplicates are otherwise ignored.
- **Accounting or source-format changes:** update `docs/accounting.md` or `docs/data-sources.md` in the same change.

## Privacy

Spenda handles people's private usage data. These rules are strict:

- Never put real user data into code, tests, fixtures, docs, commit messages, or pull request text: no real spend figures, session or thread ids, timestamps copied from someone's sessions, prompts, usernames, home or project directory names, hostnames, or account details. Use obviously invented values (`/home/demo/src/web-shop`, `$2.00`).
- Persist only metadata the dashboard needs. Never store prompt or response text, tool arguments, command output, file contents, or credentials (see `action_labels.py` for how labels are derived from tool names only).
- Source data is read-only. Only the dashboard database is written.

## Testing and experimenting safely

- Tests must use synthetic data and no network. `tests/conftest.py` already points Cursor paths at empty temporary directories and sets `SPENDA_PRICE_FETCH=0`; keep new tests inside those fixtures.
- **Do not experiment against a user's real dashboard database.** Copy it first (`sqlite3 <db> ".backup /tmp/copy.sqlite"`) and pass `--database` / `SPENDA_DB` pointing at the copy.
- **Watch inherited environment variables.** An agent session may run with `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, or similar set for its own use. Spenda honors them, and an ingest that points at the wrong Claude home treats the other home's sessions as deleted and removes their rows. Pass source paths explicitly (`--codex-home`, `--claude-home`, `--cursor-home`, `--opencode-db`) or unset those variables when running Spenda for real.
- For web changes, render the affected pages for each source filter in light and dark themes. `scripts/demo/make_data.py` generates a realistic synthetic history you can ingest into a temporary database for this.
- After a visible UI change, regenerate the README demo: `uv run --with playwright --with pillow python scripts/demo/record.py`.

## Style

- Match the surrounding code: its naming, comment density, and idioms. Comments explain why, not what.
- Templates are compact single-line Jinja HTML; keep that style rather than reformatting them.
- Keep changes focused. Do not reformat unrelated code or add dependencies without a clear need (`httpx` is dev-only; the price fetcher uses `urllib`).
- Commit messages: a short imperative subject, then a body describing the behavior before and after.
