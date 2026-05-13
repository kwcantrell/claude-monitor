# Hook reference

`hooks/dispatch.py` is the single entrypoint for every hook event. It reads a
JSON payload from stdin, looks up `hook_event_name`, and routes to a handler.
Every handler is wrapped in `try/except` and logs failures to
`/tmp/claude-hook-log` — a hook must never raise or it blocks Claude Code.

## Hooks wired in v1

| Event | Mode | Handler | What it does |
|---|---|---|---|
| `SessionStart` | async | `handle_session_start` | Resets `/tmp/claude-running/*`; upserts a `sessions` row; writes `/tmp/claude-monitor-session` |
| `PreToolUse` | **sync** | `handle_pre_tool_use` | `INSERT OR IGNORE` into `tool_calls` keyed by `tool_use_id`; writes `/tmp/claude-running/{tool_use_id}` |
| `PostToolUse` | async | `handle_post_tool_use` | Completes the `tool_calls` row; if `Read`, sha256s the response and inserts into `file_reads`; if `Edit`/`Write`/`MultiEdit`/`NotebookEdit`, inserts a `file_changes(source='edit')` row; renames `/tmp/claude-running/{tool_use_id}` → `.done` |
| `PostToolUseFailure` | async | `handle_post_tool_use_failure` | Inserts a `tool_failures` row (error text truncated to 2 KB) |
| `UserPromptSubmit` | async | `handle_user_prompt_submit` | Inserts a `user_prompts` row; sets `was_interrupt=1` if the session has in-flight `tool_calls` |
| `PermissionDenied` | async | `handle_permission_denied` | Inserts a `permission_events(decision='denied')` row |
| `InstructionsLoaded` | async | `handle_instructions_loaded` | Inserts an `instructions_loaded` row (file path, reason, size) |
| `FileChanged` | async | `handle_file_changed` | Inserts a `file_changes(source='external')` row — used to invalidate stale content hashes during analysis |
| `Stop` | async | `handle_stop` | Per-turn cleanup of `/tmp/claude-activity` and `/tmp/claude-running/*` for the live-TUI; **does not** stamp `sessions.ended_at` |
| `SessionEnd` | async | `handle_session_end` | `UPDATE sessions SET ended_at=…` (first-end-wins via `WHERE ended_at IS NULL`); clears `/tmp` state |

Only `PreToolUse` is synchronous because it needs to land the `tool_calls` row
before `PostToolUse` can update it. Target latency: <20 ms (one
`INSERT OR IGNORE` plus one file write).

## Payload notes

These are the fields the dispatcher reads. Anything not listed is ignored.

### SessionStart

```json
{
  "hook_event_name": "SessionStart",
  "session_id": "<uuid>",
  "workspace": {"current_dir": "/repo"},
  "transcript_path": "/path/to/transcript.jsonl",
  "cwd": "/repo"
}
```

### PreToolUse / PostToolUse

```json
{
  "hook_event_name": "PreToolUse",
  "session_id": "<uuid>",
  "tool_name": "Read",
  "tool_input": {"file_path": "/repo/x.ts"},
  "tool_use_id": "toolu_..."
}
```

`PostToolUse` adds `tool_response` (string, `{"content": …}`, `{"file": {"content": …}}`,
or a content-list of text parts) and `is_error` (bool).

If `tool_use_id` is missing, the dispatcher synthesizes a deterministic
fallback so `Pre` and `Post` still match.

### PostToolUseFailure

```json
{
  "hook_event_name": "PostToolUseFailure",
  "session_id": "<uuid>",
  "tool_name": "Edit",
  "tool_use_id": "toolu_...",
  "tool_input": {"file_path": "/repo/x.py"},
  "error": "string with failure details"
}
```

`error` may also arrive as `error_text` or in `tool_response`; the dispatcher
tries all three.

### PermissionDenied

```json
{
  "hook_event_name": "PermissionDenied",
  "session_id": "<uuid>",
  "tool_name": "Bash",
  "tool_input": {"command": "gh pr list"}
}
```

The dispatcher records `tool_input.command` / `.url` / `.file_path` as the
`detail` field — that's what groups denials in `analyze`.

### InstructionsLoaded

```json
{
  "hook_event_name": "InstructionsLoaded",
  "session_id": "<uuid>",
  "file_path": "/repo/CLAUDE.md",
  "load_reason": "session_start",
  "response_bytes": 12500
}
```

`path` / `reason` / `size` aliases are also accepted.

### FileChanged

```json
{
  "hook_event_name": "FileChanged",
  "session_id": "<uuid>",
  "file_path": "/repo/x.ts"
}
```

`session_id` is optional — external file changes from outside Claude Code may
arrive without one.

### UserPromptSubmit

```json
{
  "hook_event_name": "UserPromptSubmit",
  "session_id": "<uuid>",
  "prompt": "stop, do this instead",
  "idle_seconds": 4.2
}
```

`user_prompt` is accepted as an alias for `prompt`.

## Safety

- Every handler is wrapped in `try/except`. Failures log a `⚠` line to
  `/tmp/claude-hook-log` and return; they never propagate to Claude Code.
- Unknown `hook_event_name` values log `⚠ unknown hook event: <name>` and
  return without touching the DB.
- Empty stdin is a no-op (returns immediately).
- All file paths get `os.path.realpath` so `./foo` and `/abs/foo` deduplicate.

## Verifying

```bash
# replay an event manually
echo '{"hook_event_name":"PreToolUse","session_id":"test","tool_name":"Read","tool_input":{"file_path":"/etc/hostname"},"tool_use_id":"tu-1"}' \
  | python3 ~/.claude/monitor/hooks/dispatch.py

# check the DB
cd ~/.claude/monitor && uv run python3 -c "
from monitor import db
c = db.connect()
for r in c.execute('SELECT * FROM tool_calls ORDER BY started_at DESC LIMIT 5'):
    print(dict(r))
"
```
