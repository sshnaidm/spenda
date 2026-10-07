"""Write synthetic Codex and Claude Code history for the README demo.

Everything here is invented: projects, prompts, sessions, and token counts.
The files use the same on-disk formats the agents write, so the demo runs
through Spenda's normal ingestion instead of a hand-built database.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECTS = ("web-shop", "billing-api", "mobile-app", "data-pipeline", "docs-site")
TASKS = (
    "Add pagination to the orders endpoint",
    "Fix flaky checkout test",
    "Migrate settings page to the new form library",
    "Speed up the nightly export job",
    "Add retry with backoff to the payment client",
    "Write release notes for 2.4",
    "Refactor the auth middleware",
    "Investigate memory growth in the worker",
    "Add dark mode to the dashboard",
    "Upgrade the ORM and fix deprecations",
    "Document the public API",
    "Add CSV import for customers",
)
CODEX_MODELS = ("gpt-6.1-sol", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna")
CLAUDE_MODELS = ("claude-opus-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")
# Per-million list prices (input, cached, write, output) used only to make the
# synthetic Claude Code cost-state agree with the transcript's tokens.
CLAUDE_PRICES = {
    "claude-opus-5": (5, 0.5, 6.25, 25),
    "claude-sonnet-5": (2, 0.2, 2.5, 10),
    "claude-haiku-4-5": (1, 0.1, 1.25, 5),
}
CODEX_TOOLS = (
    ("exec_command", {"cmd": "uv run pytest -q"}),
    ("exec_command", {"cmd": "rg -n TODO src"}),
    ("exec_command", {"cmd": "sed -n 1,80p src/app.py"}),
    ("apply_patch", "*** Begin Patch"),
    ("exec_command", {"cmd": "git diff --stat"}),
)
CLAUDE_TOOLS = ("Bash", "Read", "Edit", "Grep", "Bash", "Edit")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records), encoding="utf-8")


def _usage_series(rng: random.Random, calls: int) -> list[tuple[int, int, int, int]]:
    """Return (input, cached, write, output) per call with a growing context."""
    context = rng.randint(14_000, 26_000)
    series = []
    for index in range(calls):
        added = rng.randint(400, 4_000)
        cached = 0 if index == 0 else context
        context += added
        series.append((context, cached, added if index else context, rng.randint(150, 2_400)))
    return series


def _codex_rollout(
    home: Path, rng: random.Random, thread_id: str, start: datetime, cwd: str, model: str,
    source: object = "cli", root_id: str | None = None,
) -> tuple[Path, datetime]:
    meta = {"cwd": cwd, "cli_version": "0.160.1", "model_provider": "openai",
            "git": {"branch": "main", "commit_hash": "0" * 40}}
    records: list[dict] = [{"timestamp": _iso(start), "type": "session_meta", "ordinal": 0,
                            "payload": {**meta, "id": thread_id, "source": source}}]
    if root_id:
        # Subagent rollouts repeat the parent's metadata after their own.
        records.append({"timestamp": _iso(start), "type": "session_meta", "ordinal": 1,
                        "payload": {**meta, "id": root_id, "source": "cli"}})
    tier = "priority" if rng.random() < 0.35 else "default"
    records.append({"timestamp": _iso(start), "type": "event_msg", "ordinal": len(records), "payload": {
        "type": "thread_settings_applied", "thread_id": thread_id,
        "thread_settings": {"model": model, "service_tier": tier},
    }})
    moment = start
    for turn in range(rng.randint(1, 3) if root_id else rng.randint(2, 5)):
        turn_id = f"{thread_id}-turn-{turn}"
        records.append({"timestamp": _iso(moment), "type": "turn_context", "ordinal": len(records), "payload": {
            "turn_id": turn_id, "model": model, "reasoning_effort": rng.choice(("medium", "high")),
        }})
        for call, (inp, cached, _write, out) in enumerate(_usage_series(rng, rng.randint(4, 14))):
            moment += timedelta(seconds=rng.uniform(6, 50))
            name, arguments = rng.choice(CODEX_TOOLS)
            records.append({"timestamp": _iso(moment), "type": "response_item", "ordinal": len(records),
                            "payload": {"type": "function_call", "name": name, "arguments": arguments}})
            usage = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
                     "output_tokens": out, "reasoning_output_tokens": out // 3, "total_tokens": inp + out}
            records.append({"timestamp": _iso(moment), "type": "token_usage_record", "ordinal": len(records),
                            "payload": {"thread_id": thread_id, "turn_id": turn_id,
                                        "response_id": f"resp_{thread_id}_{turn}_{call}", "usage": usage}})
        moment += timedelta(minutes=rng.uniform(2, 20))
    path = home / "sessions" / f"{start:%Y/%m/%d}" / f"rollout-{start:%Y-%m-%dT%H-%M-%S}-{thread_id}.jsonl"
    _jsonl(path, records)
    return path, moment


def write_codex(home: Path, rng: random.Random, now: datetime, sessions: int) -> None:
    threads, edges = [], []
    for number in range(sessions):
        thread_id = f"0199d{number:03d}-demo-4000-8000-{number:012d}"
        start = now - timedelta(days=rng.uniform(0.2, 29.5), hours=rng.uniform(0, 6))
        cwd = f"/home/demo/src/{rng.choice(PROJECTS)}"
        task = rng.choice(TASKS)
        model = rng.choice(CODEX_MODELS)
        path, end = _codex_rollout(home, rng, thread_id, start, cwd, model)
        threads.append((thread_id, path, start, end, cwd, task, model, "cli", "/root", "root"))
        # About a third of tasks delegate to a cheaper explorer subagent.
        if rng.random() < 0.35:
            child_id = f"0199e{number:03d}-demo-4000-8000-{number:012d}"
            child_start = start + timedelta(minutes=rng.uniform(1, 5))
            source = {"subagent": {"thread_spawn": {
                "parent_thread_id": thread_id, "depth": 1, "agent_path": "/root/explorer",
            }}}
            child_path, child_end = _codex_rollout(
                home, rng, child_id, child_start, cwd, "gpt-6-luna", source, thread_id
            )
            threads.append((child_id, child_path, child_start, child_end, cwd, task, "gpt-6-luna",
                            json.dumps(source), "/root/explorer", "explorer"))
            edges.append((thread_id, child_id))

    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "state_1.sqlite")
    conn.executescript("""
        CREATE TABLE threads(
          id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER,
          source TEXT, model_provider TEXT, cwd TEXT, title TEXT, tokens_used INTEGER,
          git_sha TEXT, git_branch TEXT, git_origin_url TEXT, cli_version TEXT,
          first_user_message TEXT, agent_nickname TEXT, agent_role TEXT, model TEXT,
          reasoning_effort TEXT, agent_path TEXT, thread_source TEXT, name TEXT
        );
        CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT PRIMARY KEY,status TEXT);
        CREATE TABLE _sqlx_migrations(version INTEGER,success INTEGER);
        INSERT INTO _sqlx_migrations VALUES(1,1);
    """)
    for thread_id, path, start, end, cwd, task, model, source, agent_path, role in threads:
        conn.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,'openai',?,?,0,?,'main',NULL,'0.160.1',?,NULL,?,?,"
            "'high',?,?,NULL)",
            (thread_id, str(path), int(start.timestamp()), int(end.timestamp()), source, cwd, task, "0" * 40,
             task, role, model, agent_path, "user" if source == "cli" else "subagent"),
        )
    conn.executemany("INSERT INTO thread_spawn_edges VALUES(?,?,'closed')", edges)
    conn.commit()
    conn.close()


def write_claude(home: Path, rng: random.Random, now: datetime, sessions: int) -> None:
    for number in range(sessions):
        session = f"5e55d{number:03d}-demo-4000-8000-{number:012d}"
        start = now - timedelta(days=rng.uniform(0.2, 29.5), hours=rng.uniform(0, 6))
        project = rng.choice(PROJECTS)
        cwd = f"/home/demo/src/{project}"
        model = rng.choice(CLAUDE_MODELS)
        # Message id prefixes identify the API backend, as in real transcripts.
        prefix = "msg_vrtx_" if rng.random() < 0.5 else "msg_"
        line = {"sessionId": session, "cwd": cwd, "version": "2.1.263", "gitBranch": "main"}
        records: list[dict] = [{
            **line, "type": "user", "timestamp": _iso(start), "uuid": f"{session}-prompt",
            "origin": {"kind": "human"}, "promptSource": "typed",
            "message": {"role": "user", "content": "synthetic prompt"},
        }, {**line, "type": "ai-title", "timestamp": _iso(start), "aiTitle": rng.choice(TASKS)}]
        moment, total = start, 0.0
        prices = CLAUDE_PRICES[model]
        for call, (inp, cached, write, out) in enumerate(_usage_series(rng, rng.randint(12, 45))):
            moment += timedelta(seconds=rng.uniform(5, 40))
            uncached = max(0, inp - cached - write)
            total += (uncached * prices[0] + cached * prices[1] + write * prices[2] + out * prices[3]) / 1e6
            records.append({**line, "type": "assistant", "timestamp": _iso(moment), "message": {
                "id": f"{prefix}{session[:8]}{call:04d}", "model": model, "role": "assistant",
                "content": [{"type": "tool_use", "name": rng.choice(CLAUDE_TOOLS), "input": {}}],
                "usage": {"input_tokens": uncached, "cache_read_input_tokens": cached,
                          "cache_creation_input_tokens": write, "output_tokens": out,
                          "output_tokens_details": {"thinking_tokens": out // 3}},
            }})
        # Most sessions end cleanly and record Claude Code's own cost total.
        if rng.random() < 0.6:
            records.append({**line, "type": "cost-state", "timestamp": _iso(moment + timedelta(seconds=2)),
                            "startTime": int(start.timestamp() * 1000), "totalCostUSD": round(total, 6),
                            "modelUsage": {model: {"costUSD": round(total, 6)}}, "hasUnknownModelCost": False})
        _jsonl(home / "projects" / f"-home-demo-src-{project}" / f"{session}.jsonl", records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory to create codex/ and claude/ homes in")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    now = datetime.now(UTC)
    write_codex(args.root / "codex", rng, now, sessions=26)
    write_claude(args.root / "claude", rng, now, sessions=22)


if __name__ == "__main__":
    main()
