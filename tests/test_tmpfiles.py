"""Verify the live-TUI contract: /tmp/claude-* files written in the right shape."""

from __future__ import annotations

import os

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
