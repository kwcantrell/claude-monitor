#!/usr/bin/env bash
# Install the monitor tool: copies source to ~/.claude/monitor, wires hooks
# into ~/.claude/settings.json, and migrates the SQLite DB to an XDG path.
# Idempotent: safe to re-run.

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${MONITOR_INSTALL_DIR:-$HOME/.claude/monitor}"
DB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/claude-monitor"
DB_PATH="$DB_DIR/monitor.db"
SETTINGS="$HOME/.claude/settings.json"
OLD_DB="$HOME/.claude/monitor/data/monitor.db"

log() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ---- prereqs ----
command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
  || die "python3 >= 3.11 required (have $(python3 -V))"
command -v uv >/dev/null \
  || die "uv not found. install: curl -LsSf https://astral.sh/uv/install.sh | sh"

# ---- DB migration (before source copy, so we know the old data/ dir state) ----
if [[ -f "$OLD_DB" && -f "$DB_PATH" ]]; then
  log "note: DB exists at both $OLD_DB and $DB_PATH"
  log "      leaving both in place; remove one manually to consolidate"
elif [[ -f "$OLD_DB" && ! -f "$DB_PATH" ]]; then
  mkdir -p "$DB_DIR"
  for ext in "" "-wal" "-shm"; do
    if [[ -f "$OLD_DB$ext" ]]; then
      mv "$OLD_DB$ext" "$DB_DIR/monitor.db$ext"
    fi
  done
  rmdir "$HOME/.claude/monitor/data" 2>/dev/null || true
  log "migrated DB to $DB_PATH"
else
  mkdir -p "$DB_DIR"
fi

# ---- copy source ----
mkdir -p "$INSTALL_DIR"
for item in hooks monitor pyproject.toml uv.lock README.md CHANGELOG.md LICENSE; do
  if [[ -e "$SOURCE_DIR/$item" ]]; then
    rm -rf "${INSTALL_DIR:?}/$item"
    cp -a "$SOURCE_DIR/$item" "$INSTALL_DIR/"
  fi
done
# clean any stale runtime artifacts that shouldn't carry over
rm -rf "$INSTALL_DIR/__pycache__" "$INSTALL_DIR/monitor/__pycache__" \
       "$INSTALL_DIR/hooks/__pycache__" "$INSTALL_DIR/.pytest_cache"
log "copied source to $INSTALL_DIR"

# ---- venv ----
(cd "$INSTALL_DIR" && uv sync --quiet)
log "venv synced"

# ---- init DB schema (idempotent; uses CREATE TABLE IF NOT EXISTS) ----
(cd "$INSTALL_DIR" && MONITOR_DB="$DB_PATH" uv run --quiet python3 -m monitor init-db >/dev/null)
log "schema initialized at $DB_PATH"

# ---- merge hooks into settings.json ----
python3 - "$SETTINGS" "$INSTALL_DIR" "$HOME" <<'PY'
import json, os, sys, time
from pathlib import Path

settings_path = Path(sys.argv[1])
install_dir = sys.argv[2]
home = sys.argv[3]

dispatch_cmd = f"python3 {install_dir}/hooks/dispatch.py"

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

def looks_like_monitor(entry_list, target_cmd_normalized) -> bool:
    if not isinstance(entry_list, list):
        return False
    for entry in entry_list:
        for hook in entry.get("hooks", []) or []:
            if hook.get("type") == "command":
                if normalize_cmd(hook.get("command", "")) == target_cmd_normalized:
                    return True
    return False

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
    # idempotent if current is exactly the single desired entry
    if current == desired_list:
        continue
    # also idempotent if normalized command matches (handles tilde vs absolute)
    if isinstance(current, list) and len(current) == 1 \
       and looks_like_monitor(current, target_normalized) \
       and current[0].get("hooks", [{}])[0].get("async", False) == is_async:
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
  install dir: $INSTALL_DIR
  database:    $DB_PATH

verify:  cd $INSTALL_DIR && uv run python3 -m monitor sessions
EOF
