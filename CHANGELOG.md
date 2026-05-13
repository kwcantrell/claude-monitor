# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-12

Initial release. Replaces the four inline-Python hooks previously embedded in
`~/.claude/settings.json` with a single dispatcher and adds cross-session
analysis.

### Added

- **Hook dispatcher** (`hooks/dispatch.py`) routing 10 Claude Code hook events
  through a single Python entrypoint that never raises. Errors are logged to
  `/tmp/claude-hook-log` so a bug can't block Claude Code.
- **SQLite persistence** (`monitor/db.py`) with 8 tables in WAL mode. Idempotent
  CRUD keyed on `tool_use_id` for `tool_calls`/`tool_failures` and composite PKs
  with timestamps for everything else, so retried hooks never double-insert.
- **Content-hashed Read responses** — every `Read` tool response is sha256'd and
  written to `file_reads`. This is the cornerstone signal for the flagship
  redundancy detector.
- **Quality-signal capture** — tool failures, permission denials, user prompts
  (including interrupt detection), and `InstructionsLoaded` events all land in
  their own tables.
- **Cache invalidation** — `FileChanged` rows plus own-edit tracking on
  `Edit`/`Write`/`MultiEdit` invalidate stale content hashes during analysis.
- **`monitor analyze` CLI** with five aggregations and four suggested-action
  mappings (skill candidate, allowlist entry, CLAUDE.md slim, informational).
- **`monitor sessions`** — recent-session summary.
- **`monitor init-db`** — apply the schema (idempotent).
- **`monitor purge --older-than <window>`** — privacy/space cleanup.
- **Live-TUI contract preserved** — `monitor/tmpfiles.py` writes
  `/tmp/claude-running/`, `/tmp/claude-tool-count`, `/tmp/claude-activity`, and
  `/tmp/claude-hook-log` in the same shape the existing
  `~/.claude/claude-monitor.py` expects.
- **Tests** — 23 tests across `test_db.py`, `test_analyze.py`, `test_tmpfiles.py`.
- **Docs** — `README.md` plus `docs/hooks.md`, `docs/schema.md`,
  `docs/analyze.md`.

### Changed

- `~/.claude/settings.json` hooks section: the four inline-Python hooks
  (`SessionStart`, `PreToolUse`, `PostToolUse`, `Stop`) now delegate to
  `python3 ~/.claude/monitor/hooks/dispatch.py`. Six new hook events are wired:
  `PostToolUseFailure`, `UserPromptSubmit`, `PermissionDenied`,
  `InstructionsLoaded`, `FileChanged`, `SessionEnd`. The previous settings file
  is backed up at `~/.claude/settings.json.bak-<epoch>`.
- The `/tmp/claude-running/{id}` filename now uses Claude Code's `tool_use_id`
  instead of a zero-padded sequence number. The live-TUI reads file *contents*
  and is unaffected; the `/tmp/claude-seq` counter is no longer needed.

### Removed

- Bash `tool_response` is no longer stored. The previous inline hook kept the
  first 2 KB of stdout; that's the highest-risk source for leaked secrets and it
  isn't needed for the v1 aggregations.

### Deferred to v2

Rich Textual TUI rewrite, per-turn token tracking via transcript JSONL parsing,
subagent (`SubagentStart`/`SubagentStop`) and compaction
(`PreCompact`/`PostCompact`) tracking, batch-level analysis, and the remaining
intra-session pattern checkers (high-velocity, read-edit loops, edit-failure
loops, context-pressure).

[0.1.0]: ./
