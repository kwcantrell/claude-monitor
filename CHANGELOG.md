# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] — 2026-05-14

### Added

- **`monitor watch` — Textual live-activity TUI.** New `watch` subcommand
  opens a Rich Textual app showing the current session, running tools,
  recently-completed tools, and a tail of `/tmp/claude-hook-log`. Reads the
  existing tmpfile contract plus a periodic SQLite lookup for the workspace.
  First step toward the v2 "Textual TUI rewrite". The legacy
  `~/.claude/claude-monitor.py` ANSI script is kept in place as a fallback.
  The RECENT pane retains completed tools until end-of-turn (when the `Stop`
  hook clears `/tmp/claude-running/`) rather than aging entries out by wall
  clock — gives you a full picture of what the agent did this turn.
- `textual>=0.86` dependency.
- Unit tests for the pure-stdlib helpers in `monitor/tui.py`
  (`tests/test_tui_helpers.py`).

### Fixed

- **Cancelled parallel-batch orphan inflation.** When a sibling of a parallel
  tool batch errored, the cancelled siblings fired `PreToolUse` but never
  `PostToolUse`, leaving non-`.done` files in `/tmp/claude-running/` and
  inflating the `[N pending]` log suffix. `monitor/tmpfiles.py` now defines
  `ORPHAN_THRESHOLD_SEC` (300s) and a `count_running()` helper that excludes
  aged-out entries; `mark_done` resyncs `PENDING_TOOLS` from this count
  instead of unconditionally decrementing. Files are skipped from
  counts/display rather than deleted, so a legitimately long-running tool
  can still rename its file on Post. `tui._read_running` likewise skips
  orphans for display.
- **Failing tools no longer leak running-files.** `handle_post_tool_use_failure`
  now calls `tmpfiles.mark_done(..., failed=True)` so the failing tool's
  running-file is properly renamed to `.done`. The hook log uses ✗ and
  "failed `<label>`" activity for failed tools.

### Changed

- **`tui._read_done` caches per-file metadata between ticks.** The 0.5s
  refresh loop previously re-stat'd and re-read every `.done` file each
  tick. The helper now accepts an optional `cache` dict keyed by filename
  → `(label, mtime)` and `MonitorApp` owns a single instance. New entries
  are stat'd once; disappeared entries (e.g. after `session_end_clear`)
  are evicted. 50-tick microbenchmark with 1000 files: 1238ms → 132ms
  (9.4× speedup).

## [0.3.0] — 2026-05-13

### Changed

- **`install.sh` no longer copies source into `~/.claude/monitor/`.** Hooks
  now reference the source repo directly at its clone location. Avoids name
  collision with possible future Anthropic features under `~/.claude/` and
  eliminates the "deployed copy vs working copy" drift. Source edits take
  effect on the next tool call without a reinstall step.
- **`uninstall.sh` only strips hooks**; the source repo and database are left
  untouched. If a legacy `~/.claude/monitor/` install dir exists from v0.2.0,
  both scripts now detect and remove it.

### Removed

- `MONITOR_INSTALL_DIR` env var (no install dir to point at anymore).
- `~/.claude/monitor/` runtime install dir.

## [0.2.0] — 2026-05-13

### Changed

- **Restructured as installable standalone tool.** The git-tracked source repo
  now lives separately from the runtime install. `install.sh` copies source
  into `~/.claude/monitor/` (overridable via `MONITOR_INSTALL_DIR`), runs
  `uv sync`, initializes the schema, and merges hook entries into
  `~/.claude/settings.json` with a timestamped backup. Idempotent.
- **SQLite database moved to XDG path.** Default DB path is now
  `${XDG_DATA_HOME:-~/.local/share}/claude-monitor/monitor.db` instead of
  `~/.claude/monitor/data/monitor.db`. `install.sh` auto-migrates an existing
  DB (and any `-wal` / `-shm` siblings) the first time it runs. `MONITOR_DB`
  env var still overrides the default. Rationale: data must survive install
  upgrades and uninstalls; co-locating with code is upgrade-hostile.

### Added

- `install.sh` and `uninstall.sh` at the source repo root.
- `LICENSE` (MIT).

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
