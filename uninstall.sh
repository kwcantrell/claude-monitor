#!/usr/bin/env bash
# Uninstall the monitor tool: strip its hook entries from
# ~/.claude/settings.json (with backup) and remove the install dir.
# Deliberately does NOT touch the SQLite DB; remove it manually if desired.

set -euo pipefail

INSTALL_DIR="${MONITOR_INSTALL_DIR:-$HOME/.claude/monitor}"
DB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/claude-monitor"
SETTINGS="$HOME/.claude/settings.json"

log() { printf '%s\n' "$*"; }

# ---- strip hooks from settings.json ----
if [[ -f "$SETTINGS" ]]; then
  python3 - "$SETTINGS" "$INSTALL_DIR" "$HOME" <<'PY'
import json, os, sys, time
from pathlib import Path

settings_path = Path(sys.argv[1])
install_dir = sys.argv[2]
home = sys.argv[3]

events = [
    "SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "UserPromptSubmit", "PermissionDenied", "InstructionsLoaded",
    "FileChanged", "Stop", "SessionEnd",
]
candidate_paths = {
    f"python3 {install_dir}/hooks/dispatch.py",
    f"python3 ~/.claude/monitor/hooks/dispatch.py",
    f"python3 {home}/.claude/monitor/hooks/dispatch.py",
}

raw = settings_path.read_text()
data = json.loads(raw) if raw.strip() else {}
hooks = data.get("hooks", {})

def is_monitor_entry(entry):
    for hook in entry.get("hooks", []) or []:
        if hook.get("type") == "command" and hook.get("command") in candidate_paths:
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

# ---- remove install dir ----
if [[ -d "$INSTALL_DIR" ]]; then
  rm -rf "$INSTALL_DIR"
  log "removed $INSTALL_DIR"
else
  log "no install dir at $INSTALL_DIR"
fi

cat <<EOF

monitor uninstalled.

Your SQLite database was NOT removed. If you want to delete it:
  rm -rf $DB_DIR
EOF
