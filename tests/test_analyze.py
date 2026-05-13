"""Threshold and suggestion logic for cross-session analysis."""

from __future__ import annotations

import pytest

from monitor import analyze, db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "a.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def _seed_reads(conn, *, hash_, sessions, reads_per_session, base_t=1000.0, file_path="/repo/x.ts"):
    t = base_t
    for s in range(sessions):
        sid = f"s{s}"
        db.upsert_session(conn, sid, None, None, base_t)
        for _ in range(reads_per_session):
            db.insert_file_read(
                conn,
                content_sha256=hash_,
                session_id=sid,
                file_path=file_path,
                read_at=t,
                response_bytes=1024,
            )
            t += 1


def test_repeated_reads_below_threshold(conn):
    _seed_reads(conn, hash_="h1", sessions=2, reads_per_session=2)  # 4 reads, 2 sessions
    out = analyze.repeated_reads(conn, since=None)
    assert out == []


def test_repeated_reads_above_threshold(conn):
    _seed_reads(conn, hash_="h2", sessions=3, reads_per_session=3)  # 9 reads, 3 sessions
    out = analyze.repeated_reads(conn, since=None)
    assert len(out) == 1
    assert out[0].content_sha256 == "h2"
    assert out[0].read_count == 9
    assert out[0].session_count == 3


def test_repeated_reads_invalidated_by_file_change(conn):
    _seed_reads(conn, hash_="h3", sessions=3, reads_per_session=3, base_t=1000.0)
    # invalidate after all reads — should drop the group
    db.insert_file_change(
        conn,
        file_path="/repo/x.ts",
        changed_at=2000.0,
        source="external",
        session_id=None,
    )
    out = analyze.repeated_reads(conn, since=None)
    assert out == []


def test_repeated_reads_change_before_reads_does_not_invalidate(conn):
    # file changed long before the reads — those reads remain "live"
    db.insert_file_change(
        conn,
        file_path="/repo/x.ts",
        changed_at=500.0,
        source="external",
        session_id=None,
    )
    _seed_reads(conn, hash_="h4", sessions=3, reads_per_session=3, base_t=1000.0)
    out = analyze.repeated_reads(conn, since=None)
    assert len(out) == 1


def test_repeated_commands_threshold(conn):
    for s in range(3):
        sid = f"s{s}"
        db.upsert_session(conn, sid, None, None, 1.0)
        for _ in range(2):
            db.insert_tool_call(
                conn,
                tool_use_id=f"{sid}-{_}-{id((s,_))}",
                session_id=sid,
                tool_name="Bash",
                file_path=None,
                command="rg --files",
                input_json="{}",
                started_at=1.0 + _,
            )
    out = analyze.repeated_commands(conn, since=None)
    assert len(out) == 1
    assert out[0].command == "rg --files"
    assert out[0].invocations == 6
    assert out[0].session_count == 3


def test_repeated_denials_threshold(conn):
    for s in range(2):
        for i in range(3):
            db.insert_permission_event(
                conn,
                session_id=f"s{s}",
                tool_name="Bash",
                decision="denied",
                detail="gh pr list",
                occurred_at=1.0 + s * 100 + i,
            )
    out = analyze.repeated_denials(conn, since=None)
    assert len(out) == 1
    assert out[0].denial_count == 6
    assert out[0].session_count == 2


def test_rules_bloat_surfaces_large_always_loaded_file(conn):
    for s in range(5):
        sid = f"s{s}"
        db.upsert_session(conn, sid, None, None, 1.0)
        db.insert_instructions_loaded(
            conn,
            session_id=sid,
            file_path="/repo/CLAUDE.md",
            load_reason="session_start",
            response_bytes=12_000,
            loaded_at=2.0,
        )
    out = analyze.rules_bloat(conn, since=None)
    assert len(out) == 1
    assert out[0].file_path == "/repo/CLAUDE.md"
    assert out[0].sessions_loaded == 5
    assert out[0].total_sessions == 5


def test_rules_bloat_skips_small_files(conn):
    for s in range(5):
        sid = f"s{s}"
        db.upsert_session(conn, sid, None, None, 1.0)
        db.insert_instructions_loaded(
            conn,
            session_id=sid,
            file_path="/repo/tiny.md",
            load_reason="session_start",
            response_bytes=500,
            loaded_at=2.0,
        )
    assert analyze.rules_bloat(conn, since=None) == []


def test_suggest_allowlist_entry_bash():
    assert analyze.suggest_allowlist_entry("Bash", "gh pr list") == '"Bash(gh:*)"'
    assert analyze.suggest_allowlist_entry("Bash", "  rg --files  ") == '"Bash(rg:*)"'


def test_suggest_skill_name():
    assert analyze.suggest_skill_name(["/repo/src/auth/middleware.ts"]) == "middleware-overview"
    assert analyze.suggest_skill_name([]) == "cached-context"


def test_quality_signals_aggregates(conn):
    db.upsert_session(conn, "s1", None, None, 1.0)
    db.insert_tool_call(
        conn,
        tool_use_id="t1",
        session_id="s1",
        tool_name="Bash",
        file_path=None,
        command="sleep 9999",
        input_json="{}",
        started_at=10.0,
    )
    db.insert_user_prompt(
        conn,
        session_id="s1",
        prompt_text="stop",
        was_interrupt=True,
        idle_seconds=None,
        submitted_at=11.0,
    )
    db.insert_tool_failure(
        conn,
        tool_use_id="t2",
        session_id="s1",
        tool_name="Edit",
        file_path="/repo/parser.py",
        error_text="stale",
        failed_at=12.0,
    )
    q = analyze.quality_signals(conn, since=None)
    assert q.interrupt_total == 1
    assert q.failure_total == 1
    assert q.top_failure_paths[0][0] == "/repo/parser.py"
