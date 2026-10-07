# Data sources

The dashboard reads local coding-agent history without changing the source
applications. Each adapter opens source files or databases read-only and writes
only normalized metadata, token counters, and costs to the dashboard database.
It never stores prompts, assistant text, thinking text, tool arguments or
output, attachments, source code, credentials, or full conversations.

## Codex

Codex is discovered from `${CODEX_HOME:-~/.codex}` or `--codex-home`. The
adapter reads the latest `state_*.sqlite` database and rollout JSONL files under
`sessions/` and `archived_sessions/`.

The state database supplies thread metadata, project and Git fields, agent
attributes, and explicit parent-child relationships from `thread_spawn_edges`.
Rollouts supply the auditable per-response accounting records. The preferred
record is `token_usage_record.payload.usage`, identified by `response_id`.
Older rollouts use `event_msg` / `token_count` records and compute deltas from
the categorized cumulative counter. Thread-level aggregate counters are used
only for diagnostics and are never imported as usage rows.

`turn_context` supplies the model and reasoning effort active for subsequent
responses. Input includes cached-input and cache-write subsets; reasoning is a
subset of output. Partial final JSONL lines remain unread until a later scan.
Stable response identities and dashboard-owned byte offsets make rescans,
rollout moves, and process restarts safe.

## OpenCode

OpenCode is discovered from `OPENCODE_DB`, or from
`${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`, and accepts
`--opencode-db`. The adapter opens the SQLite database with `mode=ro` and
`PRAGMA query_only=ON`.

It reads session and project metadata from `session` and `project`, plus scalar
assistant accounting fields extracted from `message.data`. When available, it
also extracts only the part type and tool name from `part.data` to produce a
fixed action label; it never selects part content, tool arguments, or output,
and never reads the `credential` table. Top-level sessions become dashboard
tasks; sessions with `parent_id` become nested agents. Imported identifiers
are prefixed with `opencode:`.

Each assistant message is one usage row, keyed by its stable message ID.
`tokens.input`, cache reads, cache writes, output, and reasoning are normalized
so cached and cache-write values remain input subsets, and reasoning remains an
output subset. OpenCode's recorded `cost` is preserved directly and is never
repriced from the Codex price table. A dashboard-owned update cursor supports
incremental scans, while a complete read reconciles only OpenCode-derived rows.

## Claude Code

Claude Code is discovered from `CLAUDE_CONFIG_DIR`, or from `~/.claude`, and
accepts `--claude-home`. Root transcripts are read from:

```text
<claude-home>/projects/<project>/<session-id>.jsonl
```

Subagent transcripts are read from:

```text
<claude-home>/projects/<project>/<root-session-id>/subagents/agent-<agent-id>.jsonl
```

Root transcripts become dashboard tasks and subagent transcripts become agents
under their root. Imported identifiers are prefixed with `claude:`. The adapter
uses transcript envelope metadata and scalar assistant fields. It also inspects
only assistant content-block types and tool names to produce a fixed action
label; content bodies and tool arguments are discarded.

Assistant usage is keyed by `(sessionId, message.id)`. Streaming updates for
the same message keep the latest record, including when Claude copies shared
history into several subagent transcript files. Uncached input, cache-read input,
cache-creation input, output, and thinking-token subsets map directly into the
normalized token fields; the `cache_creation.ephemeral_1h_input_tokens` subset
is kept as `cache_write_1h_input_tokens`. Claude's output already includes
thinking tokens.

`cost-state` records are cumulative. Consecutive snapshots become dated cost
changes whose sum equals the latest source total, preserving historical trends
without inventing per-message dollar allocations. Claude Code's cumulative
total covers the whole session including subagent calls (models that only
appear in subagent transcripts are listed in the root's cost-state), so every
call recorded up to the snapshot's timestamp is represented by that record.
A snapshot does not cover calls recorded after it: a resumed session, or a
subagent still running, adds calls that are priced like calls of a session
without a cost-state until a later snapshot covers them.
When `modelUsage` includes cumulative token counters, those counters are also
authoritative for reports; transcript call rows remain auditable but do not add
their copied or incomplete token totals again.

The Claude session's turn count is the number of root records whose structured
`origin.kind` is `human` (with a conservative fallback for older typed prompt
envelopes). Tool results and generated task notifications are not user turns.
Prompt bodies are never retained. A source session may contain several human
prompts while keeping its original generated title, so the UI labels such rows
with their prompt count.

### API backend and billing

Claude Code can talk to Anthropic directly, to Vertex AI, or to Amazon
Bedrock, and a direct connection can be paid per token (API key) or covered by
a claude.ai subscription (OAuth login). Every Claude usage row, agent, and
session records a `backend` so these can be filtered and totaled separately:

| `backend` | Detected from |
|---|---|
| `vertex` | `message.id` starts with `msg_vrtx_` or the envelope `requestId` starts with `req_vrtx_` |
| `bedrock` | `message.id` starts with `msg_bdrk_` |
| `anthropic-oauth` | `msg_` id and the home's login profile is a claude.ai subscription plan |
| `anthropic-api` | `msg_` id and an API key or API-key helper is configured for Claude Code |
| `anthropic` | `msg_` id and the billing is unverified (no login, or a login without subscription billing fields) |
| `mixed` | a session or transcript whose calls used several backends |
| `unknown` | no supported backend evidence is available |

Transcripts do not say whether a direct call was paid by subscription or API
key, so the adapter reads a small allow-list of non-secret scalar fields from
the installation's login profile: `oauthAccount.billingType`,
`oauthAccount.organizationType`, and `oauthAccount.organizationName` plus
whether `customApiKeyResponses.approved` is non-empty from `.claude.json`
(inside `CLAUDE_CONFIG_DIR`, otherwise the `~/.claude.json` sibling of the
home), and from `settings.json` the presence of `apiKeyHelper` or of the
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_VERTEX`,
`CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_VERTEX_PROJECT_ID`, and `CLOUD_ML_REGION`
variables in its `env` block or in the process environment. Only the names
of key variables are inspected, never their values. `.credentials.json` holds
tokens and is never opened. Both JSON files are parsed in memory to find
those fields, and nothing else from them is kept. A key or key helper in
Claude Code's own `settings.json` outranks the OAuth login, matching Claude
Code's credential precedence. A key variable that is only in Spenda's process
environment says nothing about the logged-in Claude Code sessions, so it
counts only when there is no login. A login counts as a subscription only
when its `billingType` ends in `subscription` or its `organizationType` is a
`claude_*` plan; any other login, for example a Console organization, leaves
`msg_` calls as unverified `anthropic`. Historical key approval alone is
diagnostic and does not prove that a key is currently active. `spenda
doctor` reports the evidence and warns when the login and key evidence
disagree.

Because project directories are sometimes copied between machines, one home
can hold sessions from several backends. Per-message detection is therefore
primary, and the login profile only classifies `msg_` calls. When the profile
is wrong for that history, `--claude-billing subscription|api` (or
`SPENDA_CLAUDE_BILLING`) forces the classification. Changing the profile or
the override rereads once every unit that has direct Anthropic calls, so
their rows are relabeled; Vertex-only and Bedrock-only units stay skipped.

Subscription usage has no metered charge. Its rows carry
`billing_mode='subscription'`, a real `cost_usd` of `0`, and the list-price
value of the calls in `equivalent_cost_usd`. Reports show real spend by
default and offer an "Include subscription value" toggle. A cost-state whose
snapshot spans subscription and metered calls cannot be split; its rows carry
`billing_mode='unresolved'` with no real or equivalent value, and both totals
report the session as unknown. Newly imported snapshots from an incomplete
unit scan also keep the split unresolved, even if all readable calls appear
metered: an unreadable child may contain subscription calls. Backends seen on
an earlier complete scan remain evidence during a temporary read failure.

Claude cost-state totals are whole-session source evidence. If a cost-state
covers metered calls from more than one backend, the session is classified as
`mixed`; its total is not prorated and component backend filters exclude it. Calls no cost-state
covers are estimated from Anthropic list prices on the direct API,
subscription, Bedrock, and Vertex AI backends; partner regional-endpoint
premiums are not in the transcript and are not modeled. Unclassified direct
calls stay unpriced because they may be subscription usage.

Claude scans import valid records from a readable transcript even when another
line or transcript has an error. Destructive reconciliation is limited to
complete, readable scope: a fully read transcript can reconcile its own rows,
and removal of missing transcripts requires a complete directory scan. A scan
reconciles only the selected Claude home; sessions, usage, and fingerprints
imported from other Claude homes are preserved.

A root transcript and its subagent transcripts are one unit. After a unit is
read completely, the inode, size, and mtime of each of its files are recorded
in the dashboard's `ingestion_state` table. On later passes a unit whose
membership and fingerprints are all unchanged is skipped without opening any
file; its rows are left alone by reconciliation and only its running or
completed status is recomputed from the stored activity timestamp. A change to
any file, a new or removed subagent transcript, or `spenda ingest --all`
rereads the whole unit. A unit that could not be read completely has its
fingerprints dropped, so the next pass retries it even when nothing changed on
disk. While its root transcript is unreadable, a previously imported complete
cost-state remains authoritative and readable subagent calls are not estimated
against it (see [accounting.md](accounting.md)). Repricing does not clear an
incomplete unit's accounting warning merely because its readable calls now
have prices.

## Cursor

Cursor is discovered from `CURSOR_HOME`, or from `~/.cursor`, and accepts
`--cursor-home`. The Cursor editor's per-user data directory is discovered
from `CURSOR_USER_DIR`, or from the platform default (`~/.config/Cursor/User`
on Linux, `~/Library/Application Support/Cursor/User` on macOS,
`%APPDATA%\Cursor\User` on Windows), and accepts `--cursor-user-dir`. Both
environment variables are Spenda overrides; Cursor itself does not define
them.

Three read-only locations contribute:

```text
<cursor-home>/projects/<slug>/agent-transcripts/<session-id>/<session-id>.jsonl
<cursor-home>/projects/<slug>/agent-transcripts/<session-id>/subagents/<agent-id>.jsonl
<cursor-home>/chats/<workspace-hash>/<session-id>/store.db      (CLI, `meta` table only)
<cursor-user-dir>/globalStorage/state.vscdb                      (editor, `cursorDiskKV` only)
```

Agent transcripts are written by both the editor and the `cursor-agent` CLI
and define sessions, subagents, and one usage row per assistant message.
Only the message role and the content-block types and tool names are
inspected to derive a fixed action label; message text, tool arguments, and
tool results are discarded. Transcripts carry no timestamps or token counts
of their own.

The editor state supplies the generated title, working directory, model,
reasoning setting, creation and update times, and subagent links for editor
sessions. Those scalars are extracted inside SQLite with `json_extract`; the
composer and bubble documents never leave the database, and the `ItemTable`,
which holds credentials, is never queried. Consecutive assistant bubbles are
grouped into model responses: a response starts at a thinking bubble, at a
text bubble that does not follow thinking, or at a tool bubble whose
model call (the uuid inside Cursor's `call-<uuid>-<index>` tool call
identifier) differs from the one already seen in the current response.
When the number of grouped responses equals the transcript's assistant
messages, each usage row takes the response's timestamp, the model selected
for that turn, and the summed `tokenCount` values; otherwise every row uses
the session start time, keeps zero tokens, and a `cursor_bubble_alignment`
warning is recorded. The editor's `lastUpdatedAt` is recorded per composer
after a successful read, so a title generated later, a rename, or new token
counts are picked up even when the transcript file itself is unchanged. Editor
composers with no transcript (older Cursor releases) are imported from their
bubbles alone, keyed by the first bubble of each response.

CLI stores supply the session title, creation time, and last-used model from
the `meta` table. The `blobs` table, which holds the conversation, is never
read. The project slug is resolved to a local working directory by walking
the filesystem for a directory name that matches the slug tokens; when no
local directory matches, the slug itself is used as the project name.
Cursor's edit-tracking database (`ai-tracking/ai-code-tracking.db`) is
consulted only for the model and activity range per conversation.

Imported identifiers are prefixed with `cursor:`. A root transcript and its
subagent transcripts form one unit that is skipped when every file's inode,
size, and mtime are unchanged, exactly as for Claude Code. Removal of a
transcript is reconciled only after a complete directory walk, and editor-only
sessions are removed only when the editor state was readable and no longer
lists them. A missing `projects` directory preserves previously imported rows.

## Accounting and source changes

See [accounting.md](accounting.md) for normalization and pricing formulas.
Source formats are implementation details that can change with each tool
release. An incompatible OpenCode schema is reported as a source error; missing
or unreadable Claude or Cursor data preserves previously imported rows; unknown
Codex rollout records are ignored rather than interpreted as usage.
