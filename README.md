# monitor

> Cross-session analysis for Claude Code. Detects when the agent re-reads the same content across sessions and suggests concrete optimizations (skills, allowlist entries, CLAUDE.md slimming).

**Version:** 0.3.0 · **Status:** v1 shipped · **Python:** 3.11+

---

## What it does

Every tool call Claude Code makes flows through a hook into a local SQLite database. The flagship signal is the **sha256 of every `Read` response** — if the same content shows up across many sessions, that's wasted context the agent could be skipping.

`monitor analyze` aggregates the data and emits suggested actions:

| Pattern | Suggested action |
|---|---|
| Same file content read in many sessions | Skill candidate (cache analysis keyed by content hash) |
| Same Bash command run repeatedly | Informational (or allowlist if denied) |
| Same permission denial across sessions | Allowlist entry for `~/.claude/settings.json` |
| Large CLAUDE.md / instruction file loaded every session | CLAUDE.md slim candidate |

The existing `~/.claude/claude-monitor.py` live-TUI keeps working unchanged — `monitor` writes the same `/tmp/claude-*` contract.

---

## Install

Clone the repo to a stable location (you'll keep it there — hooks reference it directly):

```bash
git clone <your-repo-url> ~/code/claude-monitor    # or wherever you like
cd ~/code/claude-monitor
./install.sh
```

`install.sh` runs `uv sync` in place, initializes the SQLite schema at the XDG data path, and merges 10 hook entries into `~/.claude/settings.json` (with timestamped backup). The hooks reference this source location directly — there is no separate install dir. Idempotent; safe to re-run.

New Claude Code sessions start recording immediately. After a few days of normal use:

```bash
uv run python3 -m monitor analyze --since 7d
```

If you move the source repo, re-run `./install.sh` from the new location to update the hook paths.

To remove:

```bash
./uninstall.sh
```

Uninstall strips the hooks (with backup) but **deliberately leaves your SQLite database and the source repo in place**. Delete them manually if you want a clean wipe:

```bash
rm -rf ~/.local/share/claude-monitor    # the database
rm -rf ~/code/claude-monitor            # the source repo (wherever you cloned)
```

### Configuration

| Var | Default | Purpose |
|---|---|---|
| `XDG_DATA_HOME` | `~/.local/share` | Standard XDG base dir. DB lives at `$XDG_DATA_HOME/claude-monitor/monitor.db`. |
| `MONITOR_DB` | `$XDG_DATA_HOME/claude-monitor/monitor.db` | Direct override for the SQLite path. |

---

## Commands

```
monitor analyze [--since 7d|--all]   # flagship: aggregations + suggestions
monitor sessions [--limit 20]         # recent session summary
monitor init-db                       # create the database + schema (idempotent)
monitor purge --older-than 30d        # delete rows older than the window
```

All commands run as `uv run python3 -m monitor <cmd>` from the source repo.

---

## Layout

Two locations:

```
<source repo>/                   # wherever you cloned it; hooks reference it directly
├── install.sh                   # in-place install (no file copy)
├── uninstall.sh                 # strips hooks; leaves source + DB alone
├── pyproject.toml
├── hooks/dispatch.py            # entrypoint for all hook events
├── monitor/                     # Python package
│   ├── db.py                    # SQLite schema + idempotent CRUD
│   ├── tmpfiles.py              # preserves the live-TUI /tmp contract
│   ├── analyze.py               # cross-session aggregations + suggestions
│   ├── cli.py
│   └── __main__.py
├── tests/                       # pytest, 23 tests
└── .venv/                       # created by uv sync; gitignored

~/.local/share/claude-monitor/   # user data (XDG; survives reinstalls)
└── monitor.db                   # WAL-mode SQLite
```

No footprint inside `~/.claude/` beyond the hook entries in `settings.json`.

See [docs/](./docs/) for hook reference, schema reference, and analyzer details.

---

## How concurrency works

Multiple Claude Code agents can run at the same time. Each row is tagged with its `session_id`, and SQLite WAL mode serializes writes with a short `busy_timeout`. Hook contention is negligible at typical tool-call frequency.

The live-TUI is process-global though (`/tmp/claude-*` paths). If two agents run simultaneously, the TUI displays a merged view — this matches the pre-monitor behavior.

---

## Privacy

The following may contain sensitive content and are stored truncated:

| Column | Limit |
|---|---|
| `tool_calls.input_json` | 4 KB |
| `tool_failures.error_text` | 2 KB |
| `user_prompts.prompt_text` | 2 KB |

Bash `tool_response` (stdout) is **not** stored — eliminating the highest-risk source of secrets.

To delete history: `monitor purge --older-than 7d` (or `0s` for everything).

The DB lives at `~/.local/share/claude-monitor/monitor.db` (or `$XDG_DATA_HOME/claude-monitor/monitor.db` when set) and never leaves your machine.

---

## Deferred to v2

Rich Textual TUI rewrite · per-turn token tracking via transcript parsing · subagent / compaction tracking · intra-session pattern engine (high-velocity tool use, read-edit loops, edit-failure loops, context-pressure detection).

---

## Development

```bash
uv run pytest tests/ -v          # 23 tests
```

Source edits take effect immediately — hooks reference this directory, so the next tool call picks up your changes. No reinstall step.

The hook dispatcher (`hooks/dispatch.py`) is intentionally stdlib-only so it can be invoked as `python3 <source>/hooks/dispatch.py` without `uv run` overhead on every tool call. The `monitor` CLI uses `rich` for output and runs via `uv run`.
