## What and why

<!-- The problem, and the behavior before and after this change. -->

## How it was verified

<!-- Tests added or changed, commands run, pages checked (light and dark), totals compared. -->

## Checklist

- [ ] `uv run pytest` and `scripts/lint.sh` pass (CI runs only the linters).
- [ ] New behavior and its failure cases have tests built from synthetic data.
- [ ] `docs/accounting.md` / `docs/data-sources.md` are updated if accounting or a source format changed.
- [ ] `SCHEMA_VERSION` / the adapter's `PARSER_VERSION` are bumped if stored data or parsing changed.
- [ ] No real usage data (amounts, ids, paths, prompts, usernames) in code, tests, commits, or this description.
