"""Idempotency and basic CRUD."""

from __future__ import annotations

import pytest

from monitor import db


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def test_init_creates_all_tables(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected = {
        "sessions",
        "tool_calls",
        "file_reads",
        "tool_failures",
        "permission_events",
        "user_prompts",
        "instructions_loaded",
        "file_changes",
    }
    assert expected <= tables


def test_tool_call_pretooluse_idempotent(conn):
    db.upsert_session(conn, "s1", None, None, 1.0)
    assert db.insert_tool_call(
        conn,
        tool_use_id="t1",
        session_id="s1",
        tool_name="Read",
        file_path="/x",
        command=None,
        input_json="{}",
        started_at=1.0,
    )
    # second call must be a no-op
    assert not db.insert_tool_call(
        conn,
        tool_use_id="t1",
        session_id="s1",
        tool_name="Read",
        file_path="/x",
        command=None,
        input_json="{}",
        started_at=1.0,
    )
    n = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    assert n == 1


def test_complete_tool_call_only_once(conn):
    db.upsert_session(conn, "s1", None, None, 1.0)
    db.insert_tool_call(
        conn,
        tool_use_id="t1",
        session_id="s1",
        tool_name="Read",
        file_path="/x",
        command=None,
        input_json="{}",
        started_at=1.0,
    )
    assert db.complete_tool_call(
        conn,
        tool_use_id="t1",
        completed_at=2.0,
        response_bytes=100,
        content_sha256="abc",
        was_error=False,
    )
    # retry must not double-complete or double-increment tool_count
    assert not db.complete_tool_call(
        conn,
        tool_use_id="t1",
        completed_at=3.0,
        response_bytes=999,
        content_sha256="zzz",
        was_error=False,
    )
    row = conn.execute("SELECT tool_count FROM sessions WHERE session_id='s1'").fetchone()
    assert row["tool_count"] == 1


def test_file_read_pk_dedups_on_retry(conn):
    db.insert_file_read(
        conn,
        content_sha256="h",
        session_id="s1",
        file_path="/x",
        read_at=1.0,
        response_bytes=10,
    )
    db.insert_file_read(
        conn,
        content_sha256="h",
        session_id="s1",
        file_path="/x",
        read_at=1.0,
        response_bytes=10,
    )
    n = conn.execute("SELECT COUNT(*) FROM file_reads").fetchone()[0]
    assert n == 1


def test_user_prompt_interrupt_increments_session(conn):
    db.upsert_session(conn, "s1", None, None, 1.0)
    db.insert_user_prompt(
        conn,
        session_id="s1",
        prompt_text="stop",
        was_interrupt=True,
        idle_seconds=None,
        submitted_at=2.0,
    )
    db.insert_user_prompt(
        conn,
        session_id="s1",
        prompt_text="hi",
        was_interrupt=False,
        idle_seconds=None,
        submitted_at=3.0,
    )
    row = conn.execute("SELECT interrupt_count FROM sessions WHERE session_id='s1'").fetchone()
    assert row["interrupt_count"] == 1


def test_truncation(conn):
    db.upsert_session(conn, "s1", None, None, 1.0)
    big = "x" * (db.MAX_INPUT_JSON + 1000)
    db.insert_tool_call(
        conn,
        tool_use_id="t1",
        session_id="s1",
        tool_name="Bash",
        file_path=None,
        command=None,
        input_json=big,
        started_at=1.0,
    )
    row = conn.execute("SELECT input_json FROM tool_calls WHERE tool_use_id='t1'").fetchone()
    assert len(row["input_json"]) == db.MAX_INPUT_JSON


def test_purge_older_than(conn):
    db.upsert_session(conn, "s1", None, None, 1.0)
    db.insert_file_read(
        conn,
        content_sha256="h",
        session_id="s1",
        file_path="/x",
        read_at=1.0,
        response_bytes=10,
    )
    db.insert_file_read(
        conn,
        content_sha256="h",
        session_id="s1",
        file_path="/x",
        read_at=100.0,
        response_bytes=10,
    )
    counts = db.purge_older_than(conn, cutoff=50.0)
    assert counts["file_reads"] == 1
    n = conn.execute("SELECT COUNT(*) FROM file_reads").fetchone()[0]
    assert n == 1
