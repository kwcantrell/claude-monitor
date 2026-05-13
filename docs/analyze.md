# `monitor analyze` — aggregations & suggestions

`analyze` runs five cross-session aggregations and maps each hit to a concrete
suggested action. Default window is the last 7 days; pass `--all` for full
history.

Source: [`monitor/analyze.py`](../monitor/analyze.py).

## Thresholds

Tuned to surface real signal without noise. Adjust at the top of `analyze.py`
if needed.

| Aggregation | Constant | Default | Meaning |
|---|---|---|---|
| Repeated reads | `MIN_READ_SESSIONS` | 3 | distinct sessions reading the same content hash |
| Repeated reads | `MIN_READ_TOTAL` | 5 | total reads of that hash |
| Repeated commands | `MIN_CMD_SESSIONS` | 3 | distinct sessions running the command |
| Repeated commands | `MIN_CMD_TOTAL` | 5 | total invocations |
| Repeated denials | `MIN_DENIAL_TOTAL` | 3 | total denials |
| Repeated denials | `MIN_DENIAL_SESSIONS` | 2 | distinct sessions |
| Rules bloat | `BLOAT_BYTES` | 8 KB | min average size to flag |
| Rules bloat | `BLOAT_LOAD_FRACTION` | 0.8 | min fraction of sessions that loaded it |

## Aggregations

### 1. Repeated content reads — `repeated_reads`

For each `content_sha256` in `file_reads`, count rows that haven't been
invalidated by a newer `file_changes` row for the same path. If a hash crosses
both thresholds, it's a hit.

```sql
WITH live AS (
  SELECT fr.* FROM file_reads fr
  WHERE NOT EXISTS (
    SELECT 1 FROM file_changes fc
    WHERE fc.file_path = fr.file_path AND fc.changed_at > fr.read_at
  )
)
SELECT content_sha256, COUNT(*), COUNT(DISTINCT session_id), ...
FROM live GROUP BY content_sha256
HAVING session_count >= 3 AND read_count >= 5
```

**Why content hash, not file path?** Two different files with the same content
(e.g. a vendored library copied around) collapse to one signal. A path that's
been edited mid-session splits into multiple hashes — only the unchanged copy
gets flagged.

### 2. Repeated commands — `repeated_commands`

Group `tool_calls` by normalized `command` (whitespace-collapsed at hook time)
and `tool_name`. Joins to `permission_events` to surface a `denial_count` per
group.

### 3. Repeated denials — `repeated_denials`

Group `permission_events` by `(tool_name, detail)`. The `detail` is the
command/url/path that was denied.

### 4. CLAUDE.md / rules bloat — `rules_bloat`

For each `instructions_loaded.file_path`, compute `avg(response_bytes)`,
`load_count`, and `count(distinct session_id)`. Hits surface files >8 KB
loaded in ≥80% of sessions in the window.

### 5. Quality signals — `quality_signals`

Cross-cuts: total interrupts and sessions with interrupts, top tool names that
were running at interrupt time, total post-edit failures, top failure paths.

## Suggestion mapping

| Aggregation hit | Suggested action | Helper |
|---|---|---|
| Repeated content reads | **Skill candidate** — cache analysis keyed by the content hash | `suggest_skill_name(paths)` derives a slug from a file name |
| Repeated commands | Informational only — high-volume commands are common and may not need an allowlist | — |
| Repeated denials | **Allowlist entry** — print the exact `settings.json` permission string to add | `suggest_allowlist_entry(tool, detail)` |
| Rules bloat | **CLAUDE.md slim** — split into an on-demand skill loaded only when relevant | — |

`suggest_allowlist_entry("Bash", "gh pr list")` returns `"Bash(gh:*)"` — the
program name only. The rest of the command is intentionally dropped because
allowlist entries that include arguments rarely match more than once.

## Output

Renders via `rich.Console`. Use `monitor analyze --since 30d` for a longer
window or `--all` to bypass the time filter entirely.

Sample with `--all`:

```
monitor analyze  all history

────────── REPEATED READS  (same content across sessions) ──────────
sha256:b05a96272939…  /repo/auth.ts  (36B)
  read 6× across 3 sessions, last 2026-05-12 21:14
  → SUGGEST SKILL: "auth-overview"  (cache keyed by sha256:b05a…; invalidate on file_changes)

────────── CLAUDE.md / RULES BLOAT ──────────
/repo/CLAUDE.md  (12.2KB)
  loaded 3× across 3/3 sessions (100%)
  → SUGGEST CLAUDE.md SLIM: split into on-demand skill

────────── PERMISSION FRICTION ──────────
Bash(gh pr list)  denied 3× across 3 sessions
  → SUGGEST ALLOWLIST: add "Bash(gh:*)" to ~/.claude/settings.json

────────── QUALITY SIGNALS ──────────
Interrupts: 0 across 0 sessions
Tool failures: 0
```

## Verifying

```bash
cd ~/.claude/monitor && uv run pytest tests/test_analyze.py -v
```

11 tests cover each threshold (above + below), the invalidation logic, and the
suggestion helpers.
