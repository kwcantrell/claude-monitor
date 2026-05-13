#!/usr/bin/env bash
# Install the monitor tool in-place: wires hooks in ~/.claude/settings.json
# to point at this source repo's hooks/dispatch.py, syncs the venv, and
# migrates the SQLite DB to the XDG data path. Idempotent.

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/claude-monitor"
DB_PATH="$DB_DIR/monitor.db"
SETTINGS="$HOME/.claude/settings.json"
LEGACY_DIR="$HOME/.claude/monitor"
OLD_DB="$LEGACY_DIR/data/monitor.db"

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---- prereqs ----
command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || die "python3 >= 3.11 required (have $(python3 -V))"
command -v uv >/dev/null \
  || die "uv not found. install: curl -LsSf https://astral.sh/uv/install.sh | sh"

# ---- DB migration (from any pre-XDG location) ----
if [[ -f "$OLD_DB" && -f "$DB_PATH" ]]; then
  log "note: DB exists at both $OLD_DB and $DB_PATH"
  log "      leaving both in place; remove one manually to consolidate"
elif [[ -f "$OLD_DB" && ! -f "$DB_PATH" ]]; then
  mkdir -p "$DB_DIR"
  for ext in "" "-wal" "-shm"; do
    [[ -f "$OLD_DB$ext" ]] && mv "$OLD_DB$ext" "$DB_DIR/monitor.db$ext"
  done
  rmdir "$LEGACY_DIR/data" 2>/dev/null || true
  log "migrated DB to $DB_PATH"
else
  mkdir -p "$DB_DIR"
fi

# ---- remove legacy install dir at ~/.claude/monitor (no longer used) ----
# Pre-v0.3.0 versions copied source into ~/.claude/monitor/. We don't do that
# anymore — hooks now point directly at the source repo. Clean it up if present.
if [[ -d "$LEGACY_DIR" ]]; then
  # Only remove if it looks like our former install (has hooks/dispatch.py)
  if [[ -f "$LEGACY_DIR/hooks/dispatch.py" ]]; then
    rm -rf "$LEGACY_DIR"
    log "removed legacy install dir $LEGACY_DIR (no longer needed)"
  else
    log "note: $LEGACY_DIR exists but doesn't look like a monitor install; leaving alone"
  fi
fi

# ---- venv (in source dir) ----
(cd "$SOURCE_DIR" && uv sync --quiet)
log "venv synced in $SOURCE_DIR"

# ---- init DB schema (idempotent) ----
(cd "$SOURCE_DIR" && MONITOR_DB="$DB_PATH" uv run --quiet python3 -m monitor init-db >/dev/null)
log "schema initialized at $DB_PATH"

# ---- merge hooks into settings.json ----
python3 - "$SETTINGS" "$SOURCE_DIR" "$HOME" <<'PY'
import json, os, sys, time
from pathlib import Path

settings_path = Path(sys.argv[1])
source_dir = sys.argv[2]
home = sys.argv[3]

dispatch_cmd = f"python3 {source_dir}/hooks/dispatch.py"

events = [
    ("SessionStart", True),
    ("PreToolUse", False),
    ("PostToolUse", True),
    ("PostToolUseFailure", True),
    ("UserPromptSubmit", True),
    ("PermissionDenied", True),
    ("InstructionsLoaded", True),
    ("FileChanged", True),
    ("Stop", True),
    ("SessionEnd", True),
]

def desired_entry(is_async: bool) -> dict:
    hook = {"type": "command", "command": dispatch_cmd}
    if is_async:
        hook["async"] = True
    return {"hooks": [hook]}

def normalize_cmd(cmd: str) -> str:
    return cmd.replace("~/", home + "/")

settings_path.parent.mkdir(parents=True, exist_ok=True)
if settings_path.exists():
    raw = settings_path.read_text()
    data = json.loads(raw) if raw.strip() else {}
else:
    raw = ""
    data = {}

hooks = data.setdefault("hooks", {})
target_normalized = normalize_cmd(dispatch_cmd)

needs_write = False
for event, is_async in events:
    desired_list = [desired_entry(is_async)]
    current = hooks.get(event)
    if current == desired_list:
        continue
    # idempotent if normalized command matches (handles tilde vs absolute,
    # or pointing at a previous source location)
    matches = (
        isinstance(current, list) and len(current) == 1
        and isinstance(current[0].get("hooks"), list)
        and len(current[0]["hooks"]) == 1
        and current[0]["hooks"][0].get("type") == "command"
        and normalize_cmd(current[0]["hooks"][0].get("command", "")) == target_normalized
        and current[0]["hooks"][0].get("async", False) == is_async
    )
    if matches:
        continue
    hooks[event] = desired_list
    needs_write = True

if not needs_write:
    print("hooks unchanged")
else:
    if settings_path.exists() and raw:
        backup = settings_path.with_name(settings_path.name + f".bak-{int(time.time())}")
        backup.write_text(raw)
        print(f"backed up settings to {backup}")
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(settings_path)
    print(f"wrote {len(events)} hook entries to {settings_path}")
PY

cat <<EOF

monitor installed.
  source:   $SOURCE_DIR
  database: $DB_PATH

verify:  cd $SOURCE_DIR && uv run python3 -m monitor sessions

note: hooks reference this source location ($SOURCE_DIR). If you move or
delete this directory, re-run install.sh from the new location to update.
EOF
