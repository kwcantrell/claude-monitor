#!/usr/bin/env bash
# Uninstall the monitor tool: strip its hook entries from
# ~/.claude/settings.json (with backup) and remove any legacy install dir.
# Deliberately does NOT touch the SQLite DB or the source repo; remove them
# manually if desired.

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/claude-monitor"
SETTINGS="$HOME/.claude/settings.json"
LEGACY_DIR="$HOME/.claude/monitor"

log() { printf '%s\n' "$*"; }

# ---- strip hooks from settings.json ----
if [[ -f "$SETTINGS" ]]; then
  python3 - "$SETTINGS" "$SOURCE_DIR" "$LEGACY_DIR" "$HOME" <<'PY'
import json, sys, time
from pathlib import Path

settings_path = Path(sys.argv[1])
source_dir = sys.argv[2]
legacy_dir = sys.argv[3]
home = sys.argv[4]

events = [
    "SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "UserPromptSubmit", "PermissionDenied", "InstructionsLoaded",
    "FileChanged", "Stop", "SessionEnd",
]

# Match hooks pointing at: current source, legacy install dir, or tilde form
candidate_commands = {
    f"python3 {source_dir}/hooks/dispatch.py",
    f"python3 {legacy_dir}/hooks/dispatch.py",
    f"python3 ~/.claude/monitor/hooks/dispatch.py",
    f"python3 {home}/.claude/monitor/hooks/dispatch.py",
}

raw = settings_path.read_text()
data = json.loads(raw) if raw.strip() else {}
hooks = data.get("hooks", {})

def is_monitor_entry(entry):
    for hook in entry.get("hooks", []) or []:
        if hook.get("type") == "command" and hook.get("command") in candidate_commands:
            return True
    # also catch any command ending in monitor/hooks/dispatch.py
    for hook in entry.get("hooks", []) or []:
        cmd = hook.get("command", "")
        if hook.get("type") == "command" and cmd.endswith("/hooks/dispatch.py"):
            # check it references a monitor-shaped path
            if "monitor" in cmd:
                return True
    return False

removed = 0
for event in events:
    entries = hooks.get(event)
    if not isinstance(entries, list):
        continue
    kept = [e for e in entries if not is_monitor_entry(e)]
    if len(kept) != len(entries):
        removed += len(entries) - len(kept)
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)

if not hooks:
    data.pop("hooks", None)

if removed == 0:
    print("no monitor hook entries to remove")
    sys.exit(0)

backup = settings_path.with_name(settings_path.name + f".bak-{int(time.time())}")
backup.write_text(raw)
tmp = settings_path.with_name(settings_path.name + ".tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n")
tmp.replace(settings_path)
print(f"removed {removed} monitor hook entries from {settings_path}")
print(f"backed up previous settings to {backup}")
PY
else
  log "no settings.json at $SETTINGS; nothing to strip"
fi

# ---- remove legacy install dir if present ----
if [[ -d "$LEGACY_DIR" && -f "$LEGACY_DIR/hooks/dispatch.py" ]]; then
  rm -rf "$LEGACY_DIR"
  log "removed legacy install dir $LEGACY_DIR"
fi

cat <<EOF

monitor uninstalled.

Your SQLite database and source repo were NOT removed.
  database:    $DB_DIR
  source repo: $SOURCE_DIR

Remove them manually if you want a clean wipe:
  rm -rf $DB_DIR
  # and rm -rf the source repo
EOF
