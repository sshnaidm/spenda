# Contributing

Contributions to Spenda are welcome. Changes should preserve accurate accounting, source isolation, and the privacy boundary described below.

## Development setup

Install Python 3.11 or newer and [uv](https://docs.astral.sh/uv/), then create the development environment:

```bash
uv sync
uv run spenda --help
```

Tests use synthetic temporary source data. They must not depend on a contributor's real Codex, OpenCode, or Claude history.

## Repository layout

- `src/spenda/ingestion/` contains source discovery, parsing, normalization, and reconciliation.
- `src/spenda/web/` contains FastAPI routes, templates, and static presentation.
- `src/spenda/config.py` resolves source and dashboard paths.
- `src/spenda/db.py` owns the normalized schema and migrations.
- `src/spenda/pricing.py` contains effective-dated price calculations.
- `tests/` contains synthetic unit and integration fixtures.
- `scripts/demo/` records the README demo animation from synthetic data.
- `docs/` records source schemas and accounting decisions.

## Documentation

- [Data sources](docs/data-sources.md) describes discovery, source fields, and normalization for each supported tool.
- [Accounting rules](docs/accounting.md) explains cost and token calculations.
- `docs/demo.webp` is generated: after a visible UI change, rerun
  `uv run --with playwright --with pillow python scripts/demo/record.py` (needs Chromium or Chrome).
  It invents its own agent history and never reads yours.

## Make a change

1. Create a focused branch.
2. Add or update tests that exercise the behavior and its failure cases.
3. Update the relevant source or accounting document when persisted fields or calculations change.
4. Run the validation commands below.
5. Describe the concrete before-and-after behavior in the pull request.

Keep unrelated generated files, local databases, exports, and personal agent history out of commits.

## Source-adapter requirements

New and changed adapters must satisfy these rules:

- Open source-owned files and databases read-only. Write only to the configured dashboard database.
- Reject a dashboard database path that overlaps source-owned storage.
- Prefix external identifiers so sessions, agents, and usage rows cannot collide across sources.
- Deduplicate with stable source identities and document how streaming or cumulative records are handled.
- Persist only metadata needed by the dashboard. Never store assistant text, thinking text, tool payloads, command output, attachments, source code, credentials, or full conversations.
- Define token categories explicitly, including whether cached input, cache writes, or reasoning are subsets of another counter.
- Preserve source-reported costs. Do not overwrite them through generic repricing.
- Mark incomplete accounting explicitly. Do not turn absent cost data into a fabricated estimate.
- Scope deletion reconciliation to the adapter's namespace, and reconcile only after a complete, readable source scan.
- Preserve existing data when a source is temporarily missing, unreadable, locked, or partially written.
- Build fixtures from synthetic data and include malformed, duplicate, deletion, and partial-scan cases where applicable.

If a source schema changes, update the observed version and fields in [Data sources](docs/data-sources.md).

## Validation

Run the full test suite and package checks:

```bash
uv run pytest
uv run python -m compileall -q src tests
uv lock --check
uv build
```

Run the linters, which CI also runs on every push and pull request:

```bash
scripts/lint.sh          # ruff and flake8
scripts/ruff.sh --fix    # ruff only, applying safe autofixes
scripts/flake8.sh        # flake8 only
```

Both linters read their settings from the repository (`[tool.ruff]` in `pyproject.toml` and `.flake8`) with a 120-character line limit.

For web changes, exercise each affected source filter and check both light and dark themes at narrow and wide widths. For accounting changes, verify exact raw totals in SQLite as well as formatted output.

Before submitting, confirm that `git diff --check` reports no whitespace errors.

## Documentation style

Use concrete field names, paths, and examples. Separate observed source behavior from assumptions. State whether a cost is source-reported, calculated, partial, or unavailable.

Do not include real prompts, responses, credentials, private repository names, or other personal data in documentation or fixtures.

## Reporting security or privacy problems

Report the smallest reproducible description through the project's issue tracker. Do not attach real transcripts, databases, credentials, or conversation content. If a public report would expose sensitive data, contact a maintainer privately before sharing details.

## License

By contributing, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
