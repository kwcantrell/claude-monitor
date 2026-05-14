"""Verify the live-TUI contract: /tmp/claude-* files written in the right shape."""

from __future__ import annotations

import os
import time

import pytest

from monitor import tmpfiles


@pytest.fixture
def tmp_contract(tmp_path, monkeypatch):
    """Redirect tmpfiles' fixed paths into a per-test directory."""
    monkeypatch.setattr(tmpfiles, "RUNNING_DIR", tmp_path / "running")
    monkeypatch.setattr(tmpfiles, "TOOL_COUNT", tmp_path / "tool-count")
    monkeypatch.setattr(tmpfiles, "PENDING_TOOLS", tmp_path / "pending")
    monkeypatch.setattr(tmpfiles, "ACTIVITY", tmp_path / "activity")
    monkeypatch.setattr(tmpfiles, "HOOK_LOG", tmp_path / "hook-log")
    return tmp_path


def test_label_for_known_tools():
    assert tmpfiles.label_for("Read", {"file_path": "/x"}) == "Read: /x"
    assert tmpfiles.label_for("Bash", {"command": "ls\n-la"}).startswith("Bash: ls -la")
    assert tmpfiles.label_for("Agent", {"description": "find foo"}) == "Agent: find foo"
    assert tmpfiles.label_for("Glob", {}) == "Glob"


def test_session_start_resets_counts(tmp_contract):
    (tmp_contract / "running").mkdir()
    (tmp_contract / "running" / "stale").write_text("old")
    (tmp_contract / "tool-count").write_text("47")
    tmpfiles.session_start_reset()
    assert (tmp_contract / "tool-count").read_text() == "0"
    assert (tmp_contract / "pending").read_text() == "0"
    assert list((tmp_contract / "running").iterdir()) == []


def test_mark_running_then_done_lifecycle(tmp_contract):
    tmpfiles.session_start_reset()
    tmpfiles.mark_running("toolu_abc", "Read: /x")

    running = tmp_contract / "running"
    in_flight = list(running.iterdir())
    assert len(in_flight) == 1
    assert in_flight[0].read_text() == "Read: /x"
    assert (tmp_contract / "pending").read_text() == "1"

    remaining = tmpfiles.mark_done("toolu_abc", "Read: /x")
    assert remaining == 0

    all_files = list(running.iterdir())
    assert len(all_files) == 1
    assert all_files[0].name.endswith(".done")
    assert (tmp_contract / "tool-count").read_text() == "1"
    assert (tmp_contract / "pending").read_text() == "0"
    activity = (tmp_contract / "activity").read_text()
    assert "thinking..." in activity
    assert "Read: /x" in activity


def test_mark_done_reports_remaining_in_flight(tmp_contract):
    tmpfiles.session_start_reset()
    tmpfiles.mark_running("a", "Read: /x")
    tmpfiles.mark_running("b", "Read: /y")
    remaining = tmpfiles.mark_done("a", "Read: /x")
    assert remaining == 1
    assert "[1 pending]" in (tmp_contract / "activity").read_text()


def test_unsafe_tool_use_id_is_sanitized(tmp_contract):
    tmpfiles.session_start_reset()
    tmpfiles.mark_running("../etc/passwd", "Bash: rm -rf /")
    files = list((tmp_contract / "running").iterdir())
    assert len(files) == 1
    # path-traversal characters must be stripped
    assert "/" not in files[0].name
    assert ".." not in files[0].name


def _age(path, age_sec):
    ts = time.time() - age_sec
    os.utime(path, (ts, ts))


# ── orphan-aware count_running + PENDING_TOOLS resync ────────────────────────


def test_count_running_excludes_orphans(tmp_contract):
    tmpfiles.session_start_reset()
    running = tmp_contract / "running"
    tmpfiles.mark_running("fresh", "Bash: ls")
    # Synthesize an orphan: write a running-file, then age it past the threshold.
    (running / "stale").write_text("Bash: stuck")
    _age(running / "stale", tmpfiles.ORPHAN_THRESHOLD_SEC + 60)

    assert tmpfiles.count_running() == 1  # fresh only; orphan excluded


def test_mark_done_resyncs_pending_against_orphans(tmp_contract):
    """Pre/Post pairs of cancelled siblings leave non-.done files that inflate
    the legacy decrement-only PENDING_TOOLS. After this fix, mark_done resyncs
    PENDING_TOOLS from disk truth (excluding orphans) so the counter recovers."""
    tmpfiles.session_start_reset()
    running = tmp_contract / "running"

    # Simulate the cancelled-batch scenario: three Pre's fire, only one Post fires.
    tmpfiles.mark_running("survivor", "Read: /x")
    tmpfiles.mark_running("cancelled_a", "Read: /a")
    tmpfiles.mark_running("cancelled_b", "Read: /b")
    assert (tmp_contract / "pending").read_text() == "3"

    # Age the two cancelled siblings past the orphan threshold.
    _age(running / "cancelled_a", tmpfiles.ORPHAN_THRESHOLD_SEC + 10)
    _age(running / "cancelled_b", tmpfiles.ORPHAN_THRESHOLD_SEC + 10)

    remaining = tmpfiles.mark_done("survivor", "Read: /x")
    assert remaining == 0  # orphans excluded
    assert (tmp_contract / "pending").read_text() == "0"


def test_mark_done_failed_glyph_and_activity(tmp_contract):
    tmpfiles.session_start_reset()
    tmpfiles.mark_running("a", "Bash: ls")
    tmpfiles.mark_done("a", "Bash: ls", failed=True)
    assert "failed Bash: ls" in (tmp_contract / "activity").read_text()
    log = (tmp_contract / "hook-log").read_text()
    assert "✗ Bash: ls" in log
