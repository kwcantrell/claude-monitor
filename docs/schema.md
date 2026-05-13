# Database schema

SQLite, WAL journal mode, located at `~/.claude/monitor/data/monitor.db`.
Schema is defined in `monitor/db.py` and applied by `monitor init-db`. All
statements use `CREATE TABLE IF NOT EXISTS`, so re-running is safe.

## Pragmas

| Pragma | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Concurrent reads + serialized writes — multiple agents share one DB |
| `synchronous` | `NORMAL` | Hooks fire often; durability isn't worth the per-call fsync |
| `foreign_keys` | `ON` | Cheap consistency |
| `busy_timeout` | `2000` ms | Tolerate brief lock contention when multiple agents write |

## Tables

### `sessions`

One row per Claude Code session.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT PK | Provided by Claude Code (UUID-like) |
| `workspace` | TEXT | `workspace.current_dir` from the SessionStart payload |
| `transcript_path` | TEXT | Path to the session's JSONL transcript |
| `started_at` | REAL | Unix epoch seconds |
| `ended_at` | REAL | Set by `SessionEnd` (first-end-wins via `WHERE ended_at IS NULL`) |
| `tool_count` | INTEGER | Incremented on every `complete_tool_call` |
| `interrupt_count` | INTEGER | Incremented when a `UserPromptSubmit` arrives with in-flight tools |

### `tool_calls`

One row per tool invocation. The primary signal table.

| Column | Type | Notes |
|---|---|---|
| `tool_use_id` | TEXT PK | From Claude Code; idempotency key. Synthesized if missing |
| `session_id` | TEXT NOT NULL | FK-shaped, no enforced FK |
| `tool_name` | TEXT NOT NULL | e.g. `Read`, `Bash`, `mcp__puppeteer__puppeteer_click` |
| `file_path` | TEXT | `realpath`-normalized; NULL for non-file tools |
| `command` | TEXT | Bash commands only; whitespace-collapsed for grouping |
| `input_json` | TEXT | Raw `tool_input`, truncated to 4 KB |
| `started_at` | REAL NOT NULL | Unix epoch seconds |
| `completed_at` | REAL | NULL until `PostToolUse` lands |
| `response_bytes` | INTEGER | Only filled for `Read` |
| `content_sha256` | TEXT | Only filled for `Read` — the cross-session redundancy key |
| `was_error` | INTEGER | 0/1 |

Indexes: `idx_tc_session (session_id)`, `idx_tc_hash (content_sha256 WHERE NOT NULL)`,
`idx_tc_cmd (command WHERE NOT NULL)`.

### `file_reads`

Denormalized fast-path for cross-session redundancy aggregation. Populated on
every successful `Read` `PostToolUse`.

| Column | Type | Notes |
|---|---|---|
| `content_sha256` | TEXT NOT NULL | The content hash |
| `session_id` | TEXT NOT NULL | |
| `file_path` | TEXT NOT NULL | `realpath`-normalized |
| `read_at` | REAL NOT NULL | |
| `response_bytes` | INTEGER | |

PK: `(content_sha256, session_id, read_at)`.
Indexes: `idx_fr_hash`, `idx_fr_path`.

### `tool_failures`

One row per `PostToolUseFailure`.

| Column | Type | Notes |
|---|---|---|
| `tool_use_id` | TEXT PK | Same idempotency key as `tool_calls` |
| `session_id` | TEXT NOT NULL | |
| `tool_name` | TEXT NOT NULL | |
| `file_path` | TEXT | |
| `error_text` | TEXT | Truncated to 2 KB |
| `failed_at` | REAL NOT NULL | |

### `permission_events`

One row per `PermissionDenied`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `session_id` | TEXT NOT NULL | |
| `tool_name` | TEXT NOT NULL | |
| `decision` | TEXT NOT NULL | Always `'denied'` in v1 |
| `detail` | TEXT | The command/url/path that was denied |
| `occurred_at` | REAL NOT NULL | |

Unique: `(session_id, tool_name, detail, occurred_at)`.

### `user_prompts`

One row per `UserPromptSubmit`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `session_id` | TEXT NOT NULL | |
| `prompt_text` | TEXT | Truncated to 2 KB |
| `was_interrupt` | INTEGER | 1 if any in-flight tool call existed at submission |
| `idle_seconds` | REAL | From the hook payload if provided |
| `submitted_at` | REAL NOT NULL | |

### `instructions_loaded`

One row per `InstructionsLoaded` event.

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT NOT NULL | |
| `file_path` | TEXT NOT NULL | `realpath`-normalized |
| `load_reason` | TEXT | e.g. `session_start` |
| `response_bytes` | INTEGER | Size of the loaded content |
| `loaded_at` | REAL NOT NULL | |

PK: `(session_id, file_path, loaded_at)`.

### `file_changes`

External file changes (`source='external'`) and own edits (`source='edit'`).
Used to invalidate stale `content_sha256` values during analysis: a `Read`'s
hash is "live" only if no `file_changes.changed_at` is newer than the read for
that path.

| Column | Type | Notes |
|---|---|---|
| `file_path` | TEXT NOT NULL | |
| `changed_at` | REAL NOT NULL | |
| `source` | TEXT NOT NULL | `'external'` \| `'edit'` |
| `session_id` | TEXT | NULL for external changes |

PK: `(file_path, changed_at)`.
Index: `idx_fc_path`.

## Idempotency

- `tool_calls`: `INSERT OR IGNORE` on `PreToolUse`; `UPDATE … WHERE completed_at IS NULL` on `PostToolUse`. A retried hook can't double-complete.
- `tool_failures`: PK is `tool_use_id` → `INSERT OR IGNORE`.
- `file_reads`, `permission_events`, `instructions_loaded`, `file_changes`: composite PKs include a timestamp → safe under retries.

## Privacy

The truncation limits live in `monitor/db.py`:

| Constant | Value |
|---|---|
| `MAX_INPUT_JSON` | 4096 bytes |
| `MAX_ERROR` | 2048 bytes |
| `MAX_PROMPT` | 2048 bytes |

To delete history: `monitor purge --older-than 7d`. The purge runs `VACUUM` to
reclaim disk space.

## Inspecting the DB

```bash
cd ~/.claude/monitor && uv run python3 -c "
from monitor import db
c = db.connect()
for r in c.execute('SELECT tool_name, COUNT(*) FROM tool_calls GROUP BY tool_name ORDER BY 2 DESC'):
    print(*r)
"
```
