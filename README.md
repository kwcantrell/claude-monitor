# monitor

> Cross-session analysis for Claude Code. Detects when the agent re-reads the same content across sessions and suggests concrete optimizations (skills, allowlist entries, CLAUDE.md slimming).

**Version:** 0.2.0 · **Status:** v1 shipped · **Python:** 3.11+

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

```bash
git clone <your-repo-url> claude-monitor
cd claude-monitor
./install.sh
```

`install.sh` copies source to `~/.claude/monitor/`, runs `uv sync`, initializes the SQLite schema, and wires 10 hook entries into `~/.claude/settings.json` (with a timestamped backup). It's idempotent — safe to re-run for upgrades.

New Claude Code sessions start recording immediately. After a few days of normal use:

```bash
cd ~/.claude/monitor
uv run python3 -m monitor analyze --since 7d
```

To remove:

```bash
./uninstall.sh
```

Uninstall strips the hooks (with backup) and removes the install dir but **deliberately leaves your SQLite database in place**. Delete it manually with `rm -rf ~/.local/share/claude-monitor` if you want a clean wipe.

### Configuration

Three env vars override defaults at install time and runtime:

| Var | Default | Purpose |
|---|---|---|
| `MONITOR_INSTALL_DIR` | `~/.claude/monitor` | Where `install.sh` copies source. |
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

All commands run as `uv run python3 -m monitor <cmd>` from the install dir.

---

## Layout

Three directories, one purpose each:

```
<source repo>/                # what you clone (git-tracked)
├── install.sh                # populates ~/.claude/monitor and migrates DB
├── uninstall.sh
├── pyproject.toml
├── hooks/dispatch.py         # single entrypoint for all hook events
├── monitor/                  # Python package
│   ├── db.py                 # SQLite schema + idempotent CRUD
│   ├── tmpfiles.py           # preserves the live-TUI /tmp contract
│   ├── analyze.py            # cross-session aggregations + suggestions
│   ├── cli.py
│   └── __main__.py
└── tests/                    # pytest, 23 tests

~/.claude/monitor/            # runtime install (managed by install.sh)
├── hooks/, monitor/, pyproject.toml, uv.lock, .venv/
                              # what `python3 ~/.claude/monitor/hooks/dispatch.py` invokes

~/.local/share/claude-monitor/   # user data (survives reinstalls)
└── monitor.db                # WAL-mode SQLite
```

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

From the source repo (not the install dir):

```bash
uv run pytest tests/ -v          # 23 tests
```

To test source changes against the live DB without reinstalling:

```bash
uv run --project /path/to/source python3 -m monitor analyze
```

The hook dispatcher (`hooks/dispatch.py`) is intentionally stdlib-only so it can be invoked as `python3 ~/.claude/monitor/hooks/dispatch.py` without `uv run` overhead on every tool call. The `monitor` CLI uses `rich` for output and runs via `uv run`.
