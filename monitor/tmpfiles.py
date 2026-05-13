"""Live-TUI contract: writes /tmp/claude-* files consumed by ~/.claude/claude-monitor.py."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

RUNNING_DIR = Path("/tmp/claude-running")
TOOL_COUNT = Path("/tmp/claude-tool-count")
PENDING_TOOLS = Path("/tmp/claude-pending-tools")
ACTIVITY = Path("/tmp/claude-activity")
HOOK_LOG = Path("/tmp/claude-hook-log")


def _safe_int(path: Path) -> int:
    try:
        return int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_atomic(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except OSError:
        pass


def _safe_filename(name: str) -> str:
    """Make a tool_use_id safe to use as a filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:128]


def label_for(tool_name: str, tool_input: dict) -> str:
    """Reproduce the label format the previous inline hooks produced."""
    detail = ""
    if tool_name == "Bash":
        detail = (tool_input.get("command") or "").replace("\n", " ")[:60]
    elif tool_name in ("Read", "Edit", "Write", "MultiEdit"):
        detail = tool_input.get("file_path") or ""
    elif tool_name == "Agent":
        detail = (tool_input.get("description") or "")[:50]
    elif tool_name == "WebFetch":
        detail = (tool_input.get("url") or "")[:50]
    elif tool_name == "WebSearch":
        detail = (tool_input.get("query") or "")[:50]
    return f"{tool_name}: {detail}" if detail else tool_name


def session_start_reset() -> None:
    try:
        RUNNING_DIR.mkdir(parents=True, exist_ok=True)
        for f in RUNNING_DIR.iterdir():
            with contextlib.suppress(OSError):
                f.unlink()
    except OSError:
        pass
    _write_atomic(TOOL_COUNT, "0")
    _write_atomic(PENDING_TOOLS, "0")
    log_event("─── session start")


def session_end_clear() -> None:
    with contextlib.suppress(OSError):
        ACTIVITY.unlink()
    try:
        RUNNING_DIR.mkdir(parents=True, exist_ok=True)
        for f in RUNNING_DIR.iterdir():
            with contextlib.suppress(OSError):
                f.unlink()
    except OSError:
        pass
    _write_atomic(PENDING_TOOLS, "0")
    log_event("─── turn done")


def mark_running(tool_use_id: str, label: str) -> None:
    try:
        RUNNING_DIR.mkdir(parents=True, exist_ok=True)
        path = RUNNING_DIR / _safe_filename(tool_use_id)
        path.write_text(label)
    except OSError:
        pass
    _write_atomic(PENDING_TOOLS, str(_safe_int(PENDING_TOOLS) + 1))
    log_event(f"▶ {label}")


def mark_done(tool_use_id: str, label: str) -> int:
    """Rename {id} → {id}.done. Returns remaining in-flight count."""
    safe = _safe_filename(tool_use_id)
    src = RUNNING_DIR / safe
    dst = RUNNING_DIR / f"{safe}.done"
    try:
        if src.exists():
            os.rename(src, dst)
        else:
            # fallback: prefix match by tool name (older record without tool_use_id)
            try:
                tool_name = label.split(":", 1)[0]
                for f in sorted(RUNNING_DIR.iterdir()):
                    if f.name.endswith(".done"):
                        continue
                    with contextlib.suppress(OSError):
                        if f.read_text().strip().startswith(tool_name):
                            os.rename(f, RUNNING_DIR / f"{f.name}.done")
                            break
            except OSError:
                pass
    except OSError:
        pass

    _write_atomic(PENDING_TOOLS, str(max(0, _safe_int(PENDING_TOOLS) - 1)))
    _write_atomic(TOOL_COUNT, str(_safe_int(TOOL_COUNT) + 1))

    remaining = 0
    try:
        remaining = sum(
            1 for f in RUNNING_DIR.iterdir() if not f.name.endswith(".done")
        )
    except OSError:
        pass

    activity = f"thinking... (after {label})"
    if remaining > 0:
        activity += f" [{remaining} pending]"
    _write_atomic(ACTIVITY, activity)
    suffix = f"  [{remaining} pending]" if remaining > 0 else ""
    log_event(f"✓ {label}{suffix}")
    return remaining


def log_event(message: str) -> None:
    try:
        ts = time.strftime("%H:%M:%S")
        with HOOK_LOG.open("a") as f:
            f.write(f"{ts} {message}\n")
    except OSError:
        pass


def log_error(message: str) -> None:
    log_event(f"⚠ {message}")
