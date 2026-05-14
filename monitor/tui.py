"""Live activity Textual TUI — `python3 -m monitor watch`.

Reads the existing live-TUI tmpfile contract (/tmp/claude-running/, /tmp/claude-tool-count,
/tmp/claude-hook-log) populated by hooks/dispatch.py, plus a periodic SQLite lookup for
the current session's workspace.
"""

from __future__ import annotations

import contextlib
import os
import time
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, RichLog, Static

from monitor import db, tmpfiles

SESSION_HINT = Path("/tmp/claude-monitor-session")
HOOKLOG_TAIL_LINES = 14


# ── stdlib helpers (testable without a Textual app) ──────────────────────────

def _read_running(now: float | None = None) -> list[tuple[str, float]]:
    """Return [(label, age_sec), ...] for in-flight tool jobs.

    Orphan entries (cancelled siblings of failed parallel batches, identified by
    age > tmpfiles.ORPHAN_THRESHOLD_SEC) are skipped — they fire Pre but never
    Post, so they'd otherwise sit in the RUNNING pane forever.
    """
    now = now if now is not None else time.time()
    out: list[tuple[str, float]] = []
    try:
        for f in sorted(tmpfiles.RUNNING_DIR.iterdir()):
            if f.name.endswith(".done"):
                continue
            with contextlib.suppress(OSError):
                age = now - f.stat().st_mtime
                if age > tmpfiles.ORPHAN_THRESHOLD_SEC:
                    continue
                out.append((f.read_text().strip(), age))
    except OSError:
        pass
    return out


def _read_done(
    now: float | None = None,
    cache: dict[str, tuple[str, float]] | None = None,
) -> list[tuple[str, float]]:
    """Return [(label, age_sec), ...] for jobs finished this turn, newest first.

    No time filter: the Stop hook (session_end_clear) wipes RUNNING_DIR at
    end-of-turn, so .done files naturally only exist for the current turn.

    If `cache` is supplied, it's mutated to remember (label, mtime) keyed by
    filename — subsequent calls skip the per-file stat+read for entries already
    cached. Entries whose files have disappeared (e.g. after session_end_clear)
    are evicted. The caller owns the cache.
    """
    now = now if now is not None else time.time()
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    try:
        for f in tmpfiles.RUNNING_DIR.iterdir():
            if not f.name.endswith(".done"):
                continue
            seen.add(f.name)
            if cache is not None and f.name in cache:
                label, mtime = cache[f.name]
            else:
                try:
                    mtime = f.stat().st_mtime
                    label = f.read_text().strip()
                except OSError:
                    continue
                if cache is not None:
                    cache[f.name] = (label, mtime)
            out.append((label, now - mtime))
    except OSError:
        pass
    if cache is not None:
        for stale in [k for k in cache if k not in seen]:
            del cache[stale]
    out.sort(key=lambda x: x[1])  # newest (smallest age) first
    return out


def _tail_log(n: int = HOOKLOG_TAIL_LINES, path: Path | None = None) -> list[str]:
    """Return the last `n` lines of the hook log, oldest first."""
    p = path if path is not None else tmpfiles.HOOK_LOG
    try:
        with p.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                read_size = min(size, 8192)
                f.seek(size - read_size, os.SEEK_SET)
                chunk = f.read().decode("utf-8", errors="replace")
            except OSError:
                chunk = p.read_text(errors="replace")
        lines = [ln for ln in chunk.splitlines() if ln]
        return lines[-n:]
    except OSError:
        return []


def _read_tool_count() -> int:
    try:
        return int(tmpfiles.TOOL_COUNT.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _current_session_id() -> str | None:
    try:
        sid = SESSION_HINT.read_text().strip()
        return sid or None
    except OSError:
        return None


def _lookup_session(session_id: str) -> tuple[str, str] | None:
    """Return (short_id, workspace) for a session_id, or None if not found."""
    try:
        conn = db.connect()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT session_id, workspace FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    if not row:
        return None
    sid = row["session_id"] or session_id
    return sid[:8], (row["workspace"] or "")


def _fmt_age(age: float) -> str:
    if age < 1.0:
        return f"{age * 1000:.0f}ms"
    if age < 60.0:
        return f"{age:.1f}s"
    m, s = divmod(int(age), 60)
    return f"{m}m{s:02d}s"


# ── Textual app ──────────────────────────────────────────────────────────────

CSS = """
Screen {
    layout: vertical;
}
#status {
    dock: top;
    height: 1;
    padding: 0 1;
    background: $accent 20%;
    color: $text;
}
#panes {
    height: 1fr;
}
.pane {
    border: round $panel;
    padding: 0 1;
    width: 1fr;
}
.pane-title {
    color: $accent;
    text-style: bold;
    height: 1;
}
#running, #recent-scroll {
    height: 1fr;
}
#recent {
    height: auto;
}
#hooklog {
    height: 14;
    border: round $panel;
}
"""


class MonitorApp(App):
    CSS = CSS
    TITLE = "monitor"
    SUB_TITLE = "claude code · live activity"
    BINDINGS = [
        ("q", "quit", "quit"),
        ("c", "clear_log_view", "clear log"),
        ("r", "force_refresh", "refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._session_id: str | None = None
        self._workspace: str = ""
        self._log_offset: int = 0
        self._log_initialized: bool = False
        self._done_cache: dict[str, tuple[str, float]] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="status")
        with Horizontal(id="panes"):
            with Vertical(classes="pane"):
                yield Static("RUNNING", classes="pane-title")
                yield Static("", id="running")
            with Vertical(classes="pane"):
                yield Static("RECENT", classes="pane-title")
                with VerticalScroll(id="recent-scroll"):
                    yield Static("", id="recent")
        yield RichLog(id="hooklog", highlight=False, markup=False, max_lines=400)
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_session()
        self.refresh_fast()
        self.set_interval(0.5, self.refresh_fast)
        self.set_interval(1.0, self.refresh_clock)
        self.set_interval(2.0, self.refresh_session)

    # ── refresh callbacks ────────────────────────────────────────────────

    def refresh_fast(self) -> None:
        self._render_running()
        self._render_recent()
        self._render_hooklog()
        self._render_status()

    def refresh_clock(self) -> None:
        self._render_status()

    def refresh_session(self) -> None:
        sid = _current_session_id()
        if not sid:
            self._session_id = None
            self._workspace = ""
            return
        result = _lookup_session(sid)
        if result is None:
            self._session_id = sid[:8]
            self._workspace = ""
        else:
            self._session_id, self._workspace = result

    # ── actions ──────────────────────────────────────────────────────────

    def action_clear_log_view(self) -> None:
        log = self.query_one("#hooklog", RichLog)
        log.clear()

    def action_force_refresh(self) -> None:
        self.refresh_session()
        self.refresh_fast()

    # ── render helpers ───────────────────────────────────────────────────

    def _render_status(self) -> None:
        clock = datetime.now().strftime("%H:%M:%S")
        tools = _read_tool_count()
        session = self._session_id or "(no session)"
        ws = self._workspace or "—"
        if len(ws) > 50:
            ws = "…" + ws[-49:]
        text = f"session: {session}  ·  workspace: {ws}  ·  tools: {tools}  ·  {clock}"
        self.query_one("#status", Static).update(text)

    def _render_running(self) -> None:
        jobs = _read_running()
        if not jobs:
            self.query_one("#running", Static).update("[dim]idle[/]")
            return
        lines = [f"[yellow]▶[/]  {label}  [dim]({_fmt_age(age)})[/]" for label, age in jobs]
        self.query_one("#running", Static).update("\n".join(lines))

    def _render_recent(self) -> None:
        done = _read_done(cache=self._done_cache)
        if not done:
            self.query_one("#recent", Static).update("[dim](none)[/]")
            return
        lines = [f"[green]✓[/]  [dim]{label}[/]  [dim]({_fmt_age(age)} ago)[/]" for label, age in done]
        self.query_one("#recent", Static).update("\n".join(lines))

    def _render_hooklog(self) -> None:
        log = self.query_one("#hooklog", RichLog)
        path = tmpfiles.HOOK_LOG
        try:
            size = path.stat().st_size
        except OSError:
            return

        # First call: seed with the tail and remember the file size.
        if not self._log_initialized:
            for ln in _tail_log():
                log.write(ln)
            self._log_offset = size
            self._log_initialized = True
            return

        # File rotated/truncated — reset offset.
        if size < self._log_offset:
            self._log_offset = 0
        if size == self._log_offset:
            return

        try:
            with path.open("rb") as f:
                f.seek(self._log_offset)
                chunk = f.read(size - self._log_offset)
        except OSError:
            return
        self._log_offset = size
        text = chunk.decode("utf-8", errors="replace")
        for ln in text.splitlines():
            if ln:
                log.write(ln)


def run() -> int:
    try:
        MonitorApp().run()
    except KeyboardInterrupt:
        pass
    return 0
