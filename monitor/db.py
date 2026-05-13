"""SQLite schema and idempotent CRUD for the monitor."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

def _default_db_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "claude-monitor" / "monitor.db"


DB_PATH = Path(os.environ.get("MONITOR_DB", _default_db_path()))

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=2000;

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  workspace TEXT,
  transcript_path TEXT,
  started_at REAL NOT NULL,
  ended_at REAL,
  tool_count INTEGER DEFAULT 0,
  interrupt_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_calls (
  tool_use_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  file_path TEXT,
  command TEXT,
  input_json TEXT,
  started_at REAL NOT NULL,
  completed_at REAL,
  response_bytes INTEGER,
  content_sha256 TEXT,
  was_error INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tc_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tc_hash ON tool_calls(content_sha256) WHERE content_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tc_cmd ON tool_calls(command) WHERE command IS NOT NULL;

CREATE TABLE IF NOT EXISTS file_reads (
  content_sha256 TEXT NOT NULL,
  session_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  read_at REAL NOT NULL,
  response_bytes INTEGER,
  PRIMARY KEY (content_sha256, session_id, read_at)
);
CREATE INDEX IF NOT EXISTS idx_fr_hash ON file_reads(content_sha256);
CREATE INDEX IF NOT EXISTS idx_fr_path ON file_reads(file_path);

CREATE TABLE IF NOT EXISTS tool_failures (
  tool_use_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  file_path TEXT,
  error_text TEXT,
  failed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS permission_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  decision TEXT NOT NULL,
  detail TEXT,
  occurred_at REAL NOT NULL,
  UNIQUE(session_id, tool_name, detail, occurred_at)
);

CREATE TABLE IF NOT EXISTS user_prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  prompt_text TEXT,
  was_interrupt INTEGER DEFAULT 0,
  idle_seconds REAL,
  submitted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS instructions_loaded (
  session_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  load_reason TEXT,
  response_bytes INTEGER,
  loaded_at REAL NOT NULL,
  PRIMARY KEY (session_id, file_path, loaded_at)
);

CREATE TABLE IF NOT EXISTS file_changes (
  file_path TEXT NOT NULL,
  changed_at REAL NOT NULL,
  source TEXT NOT NULL,
  session_id TEXT,
  PRIMARY KEY (file_path, changed_at)
);
CREATE INDEX IF NOT EXISTS idx_fc_path ON file_changes(file_path);
"""

MAX_INPUT_JSON = 4096
MAX_ERROR = 2048
MAX_PROMPT = 2048


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _truncate(s: str | None, n: int) -> str | None:
    if s is None:
        return None
    return s if len(s) <= n else s[:n]


def upsert_session(
    conn: sqlite3.Connection,
    session_id: str,
    workspace: str | None,
    transcript_path: str | None,
    started_at: float,
) -> None:
    conn.execute(
        """INSERT INTO sessions (session_id, workspace, transcript_path, started_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
             workspace=COALESCE(excluded.workspace, sessions.workspace),
             transcript_path=COALESCE(excluded.transcript_path, sessions.transcript_path)""",
        (session_id, workspace, transcript_path, started_at),
    )


def end_session(conn: sqlite3.Connection, session_id: str, ended_at: float) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at=? WHERE session_id=? AND ended_at IS NULL",
        (ended_at, session_id),
    )


def insert_tool_call(
    conn: sqlite3.Connection,
    *,
    tool_use_id: str,
    session_id: str,
    tool_name: str,
    file_path: str | None,
    command: str | None,
    input_json: str | None,
    started_at: float,
) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO tool_calls
           (tool_use_id, session_id, tool_name, file_path, command, input_json, started_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            tool_use_id,
            session_id,
            tool_name,
            file_path,
            command,
            _truncate(input_json, MAX_INPUT_JSON),
            started_at,
        ),
    )
    return cur.rowcount > 0


def complete_tool_call(
    conn: sqlite3.Connection,
    *,
    tool_use_id: str,
    completed_at: float,
    response_bytes: int | None,
    content_sha256: str | None,
    was_error: bool,
) -> bool:
    cur = conn.execute(
        """UPDATE tool_calls
           SET completed_at=?, response_bytes=?, content_sha256=?, was_error=?
           WHERE tool_use_id=? AND completed_at IS NULL""",
        (completed_at, response_bytes, content_sha256, 1 if was_error else 0, tool_use_id),
    )
    if cur.rowcount > 0:
        conn.execute(
            "UPDATE sessions SET tool_count=tool_count+1 WHERE session_id=(SELECT session_id FROM tool_calls WHERE tool_use_id=?)",
            (tool_use_id,),
        )
        return True
    return False


def insert_file_read(
    conn: sqlite3.Connection,
    *,
    content_sha256: str,
    session_id: str,
    file_path: str,
    read_at: float,
    response_bytes: int,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO file_reads
           (content_sha256, session_id, file_path, read_at, response_bytes)
           VALUES (?, ?, ?, ?, ?)""",
        (content_sha256, session_id, file_path, read_at, response_bytes),
    )


def insert_tool_failure(
    conn: sqlite3.Connection,
    *,
    tool_use_id: str,
    session_id: str,
    tool_name: str,
    file_path: str | None,
    error_text: str | None,
    failed_at: float,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO tool_failures
           (tool_use_id, session_id, tool_name, file_path, error_text, failed_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (tool_use_id, session_id, tool_name, file_path, _truncate(error_text, MAX_ERROR), failed_at),
    )


def insert_permission_event(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    tool_name: str,
    decision: str,
    detail: str | None,
    occurred_at: float,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO permission_events
           (session_id, tool_name, decision, detail, occurred_at)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, tool_name, decision, detail, occurred_at),
    )


def insert_user_prompt(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    prompt_text: str | None,
    was_interrupt: bool,
    idle_seconds: float | None,
    submitted_at: float,
) -> None:
    conn.execute(
        """INSERT INTO user_prompts
           (session_id, prompt_text, was_interrupt, idle_seconds, submitted_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            session_id,
            _truncate(prompt_text, MAX_PROMPT),
            1 if was_interrupt else 0,
            idle_seconds,
            submitted_at,
        ),
    )
    if was_interrupt:
        conn.execute(
            "UPDATE sessions SET interrupt_count=interrupt_count+1 WHERE session_id=?",
            (session_id,),
        )


def insert_instructions_loaded(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    file_path: str,
    load_reason: str | None,
    response_bytes: int | None,
    loaded_at: float,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO instructions_loaded
           (session_id, file_path, load_reason, response_bytes, loaded_at)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, file_path, load_reason, response_bytes, loaded_at),
    )


def insert_file_change(
    conn: sqlite3.Connection,
    *,
    file_path: str,
    changed_at: float,
    source: str,
    session_id: str | None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO file_changes
           (file_path, changed_at, source, session_id)
           VALUES (?, ?, ?, ?)""",
        (file_path, changed_at, source, session_id),
    )


def has_inflight_tools(conn: sqlite3.Connection, session_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tool_calls WHERE session_id=? AND completed_at IS NULL LIMIT 1",
        (session_id,),
    ).fetchone()
    return row is not None


def purge_older_than(conn: sqlite3.Connection, cutoff: float) -> dict[str, int]:
    """Delete rows with timestamps older than `cutoff`. Returns row counts deleted per table."""
    counts: dict[str, int] = {}
    deletes = [
        ("file_reads", "read_at"),
        ("tool_failures", "failed_at"),
        ("permission_events", "occurred_at"),
        ("user_prompts", "submitted_at"),
        ("instructions_loaded", "loaded_at"),
        ("file_changes", "changed_at"),
        ("tool_calls", "started_at"),
        ("sessions", "started_at"),
    ]
    for table, ts_col in deletes:
        cur = conn.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,))
        counts[table] = cur.rowcount
    conn.execute("VACUUM")
    return counts
