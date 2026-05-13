"""Cross-session aggregations and suggested actions."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

# Thresholds
MIN_READ_SESSIONS = 3
MIN_READ_TOTAL = 5
MIN_CMD_SESSIONS = 3
MIN_CMD_TOTAL = 5
MIN_DENIAL_TOTAL = 3
MIN_DENIAL_SESSIONS = 2
BLOAT_BYTES = 8 * 1024
BLOAT_LOAD_FRACTION = 0.8


@dataclass
class RepeatedRead:
    content_sha256: str
    read_count: int
    session_count: int
    last_read: float
    paths: list[str]
    response_bytes: int


@dataclass
class RepeatedCommand:
    command: str
    invocations: int
    session_count: int
    denial_count: int
    tool_name: str


@dataclass
class RepeatedDenial:
    tool_name: str
    detail: str
    denial_count: int
    session_count: int


@dataclass
class RulesBloat:
    file_path: str
    avg_bytes: int
    load_count: int
    sessions_loaded: int
    total_sessions: int


@dataclass
class QualitySignals:
    interrupt_total: int
    interrupt_sessions: int
    top_interrupted_tools: list[tuple[str, int]]
    failure_total: int
    top_failure_paths: list[tuple[str, int]]


@dataclass
class AnalysisReport:
    since: float | None
    until: float
    repeated_reads: list[RepeatedRead] = field(default_factory=list)
    repeated_commands: list[RepeatedCommand] = field(default_factory=list)
    repeated_denials: list[RepeatedDenial] = field(default_factory=list)
    rules_bloat: list[RulesBloat] = field(default_factory=list)
    quality: QualitySignals | None = None


def _since_clause(col: str, since: float | None) -> tuple[str, tuple]:
    if since is None:
        return "", ()
    return f" AND {col} >= ?", (since,)


def repeated_reads(conn: sqlite3.Connection, since: float | None) -> list[RepeatedRead]:
    where, params = _since_clause("fr.read_at", since)
    # A content hash is invalidated for a path if any file_changes row for that
    # path is newer than the most recent file_read for that hash on that path.
    rows = conn.execute(
        f"""
        WITH live AS (
          SELECT fr.content_sha256, fr.session_id, fr.file_path, fr.read_at, fr.response_bytes
          FROM file_reads fr
          WHERE 1=1 {where}
            AND NOT EXISTS (
              SELECT 1 FROM file_changes fc
              WHERE fc.file_path = fr.file_path AND fc.changed_at > fr.read_at
            )
        )
        SELECT content_sha256,
               COUNT(*) AS read_count,
               COUNT(DISTINCT session_id) AS session_count,
               MAX(read_at) AS last_read,
               MAX(response_bytes) AS response_bytes,
               GROUP_CONCAT(DISTINCT file_path) AS paths
        FROM live
        GROUP BY content_sha256
        HAVING session_count >= ? AND read_count >= ?
        ORDER BY read_count DESC, session_count DESC
        """,
        (*params, MIN_READ_SESSIONS, MIN_READ_TOTAL),
    ).fetchall()
    return [
        RepeatedRead(
            content_sha256=r["content_sha256"],
            read_count=r["read_count"],
            session_count=r["session_count"],
            last_read=r["last_read"],
            response_bytes=r["response_bytes"] or 0,
            paths=(r["paths"] or "").split(","),
        )
        for r in rows
    ]


def repeated_commands(conn: sqlite3.Connection, since: float | None) -> list[RepeatedCommand]:
    where, params = _since_clause("tc.started_at", since)
    rows = conn.execute(
        f"""
        SELECT tc.command,
               tc.tool_name,
               COUNT(*) AS invocations,
               COUNT(DISTINCT tc.session_id) AS session_count,
               (SELECT COUNT(*) FROM permission_events pe
                  WHERE pe.tool_name = tc.tool_name AND pe.detail = tc.command) AS denial_count
        FROM tool_calls tc
        WHERE tc.command IS NOT NULL {where}
        GROUP BY tc.command, tc.tool_name
        HAVING session_count >= ? AND invocations >= ?
        ORDER BY invocations DESC
        """,
        (*params, MIN_CMD_SESSIONS, MIN_CMD_TOTAL),
    ).fetchall()
    return [
        RepeatedCommand(
            command=r["command"],
            tool_name=r["tool_name"],
            invocations=r["invocations"],
            session_count=r["session_count"],
            denial_count=r["denial_count"] or 0,
        )
        for r in rows
    ]


def repeated_denials(conn: sqlite3.Connection, since: float | None) -> list[RepeatedDenial]:
    where, params = _since_clause("occurred_at", since)
    rows = conn.execute(
        f"""
        SELECT tool_name, detail,
               COUNT(*) AS denial_count,
               COUNT(DISTINCT session_id) AS session_count
        FROM permission_events
        WHERE decision='denied' {where}
        GROUP BY tool_name, detail
        HAVING denial_count >= ? AND session_count >= ?
        ORDER BY denial_count DESC
        """,
        (*params, MIN_DENIAL_TOTAL, MIN_DENIAL_SESSIONS),
    ).fetchall()
    return [
        RepeatedDenial(
            tool_name=r["tool_name"],
            detail=r["detail"] or "",
            denial_count=r["denial_count"],
            session_count=r["session_count"],
        )
        for r in rows
    ]


def rules_bloat(conn: sqlite3.Connection, since: float | None) -> list[RulesBloat]:
    where_session, p1 = _since_clause("started_at", since)
    where_load, p2 = _since_clause("loaded_at", since)
    total_sessions_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM sessions WHERE 1=1 {where_session}", p1
    ).fetchone()
    total_sessions = total_sessions_row["n"] if total_sessions_row else 0
    if total_sessions == 0:
        return []
    rows = conn.execute(
        f"""
        SELECT file_path,
               AVG(response_bytes) AS avg_bytes,
               COUNT(*) AS load_count,
               COUNT(DISTINCT session_id) AS sessions_loaded
        FROM instructions_loaded
        WHERE response_bytes IS NOT NULL {where_load}
        GROUP BY file_path
        HAVING avg_bytes > ? AND sessions_loaded * 1.0 / ? >= ?
        ORDER BY avg_bytes DESC
        """,
        (*p2, BLOAT_BYTES, total_sessions, BLOAT_LOAD_FRACTION),
    ).fetchall()
    return [
        RulesBloat(
            file_path=r["file_path"],
            avg_bytes=int(r["avg_bytes"] or 0),
            load_count=r["load_count"],
            sessions_loaded=r["sessions_loaded"],
            total_sessions=total_sessions,
        )
        for r in rows
    ]


def quality_signals(conn: sqlite3.Connection, since: float | None) -> QualitySignals:
    where_up, p_up = _since_clause("submitted_at", since)
    where_tf, p_tf = _since_clause("failed_at", since)

    int_row = conn.execute(
        f"""SELECT COUNT(*) AS total, COUNT(DISTINCT session_id) AS sessions
            FROM user_prompts WHERE was_interrupt=1 {where_up}""",
        p_up,
    ).fetchone()

    top_int = conn.execute(
        f"""
        SELECT tc.tool_name, COUNT(*) AS n
        FROM user_prompts up
        JOIN tool_calls tc
          ON tc.session_id = up.session_id
         AND tc.started_at <= up.submitted_at
         AND (tc.completed_at IS NULL OR tc.completed_at > up.submitted_at)
        WHERE up.was_interrupt=1 {where_up}
        GROUP BY tc.tool_name
        ORDER BY n DESC LIMIT 5
        """,
        p_up,
    ).fetchall()

    fail_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM tool_failures WHERE 1=1 {where_tf}", p_tf
    ).fetchone()

    top_fail = conn.execute(
        f"""
        SELECT file_path, COUNT(*) AS n
        FROM tool_failures
        WHERE file_path IS NOT NULL {where_tf}
        GROUP BY file_path
        ORDER BY n DESC LIMIT 5
        """,
        p_tf,
    ).fetchall()

    return QualitySignals(
        interrupt_total=int_row["total"] if int_row else 0,
        interrupt_sessions=int_row["sessions"] if int_row else 0,
        top_interrupted_tools=[(r["tool_name"], r["n"]) for r in top_int],
        failure_total=fail_row["n"] if fail_row else 0,
        top_failure_paths=[(r["file_path"], r["n"]) for r in top_fail],
    )


def run(conn: sqlite3.Connection, since: float | None) -> AnalysisReport:
    return AnalysisReport(
        since=since,
        until=time.time(),
        repeated_reads=repeated_reads(conn, since),
        repeated_commands=repeated_commands(conn, since),
        repeated_denials=repeated_denials(conn, since),
        rules_bloat=rules_bloat(conn, since),
        quality=quality_signals(conn, since),
    )


# ── Suggestion mapping ────────────────────────────────────────────────────

def suggest_skill_name(paths: list[str]) -> str:
    """Derive a skill slug from one of the file paths in the group."""
    if not paths:
        return "cached-context"
    name = paths[0].rsplit("/", 1)[-1]
    name = name.rsplit(".", 1)[0]
    return name.lower().replace("_", "-") + "-overview"


def suggest_allowlist_entry(tool_name: str, detail: str) -> str:
    if tool_name == "Bash":
        # take the first word of the command as the binary
        prog = detail.strip().split(maxsplit=1)[0] if detail else ""
        if prog:
            return f'"Bash({prog}:*)"'
    return f'"{tool_name}({detail})"'
