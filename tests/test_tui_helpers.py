"""Unit tests for the pure-stdlib helpers in monitor.tui.

The Textual App itself is not tested — these helpers are extracted so they
can be exercised without instantiating a Screen.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from monitor import tmpfiles, tui


@pytest.fixture
def fake_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    running = tmp_path / "claude-running"
    running.mkdir()
    hook_log = tmp_path / "claude-hook-log"
    tool_count = tmp_path / "claude-tool-count"
    monkeypatch.setattr(tmpfiles, "RUNNING_DIR", running)
    monkeypatch.setattr(tmpfiles, "HOOK_LOG", hook_log)
    monkeypatch.setattr(tmpfiles, "TOOL_COUNT", tool_count)
    return tmp_path


def _touch(path: Path, content: str, age_sec: float = 0.0) -> None:
    path.write_text(content)
    if age_sec:
        ts = time.time() - age_sec
        os.utime(path, (ts, ts))


# ── _read_running ────────────────────────────────────────────────────────────

def test_read_running_returns_in_flight_jobs(fake_dirs: Path) -> None:
    running = tmpfiles.RUNNING_DIR
    _touch(running / "job1", "Read: /a.py")
    _touch(running / "job2", "Bash: ls")
    _touch(running / "job3.done", "Edit: /b.py")  # finished — should be excluded

    out = tui._read_running()
    labels = {label for label, _age in out}
    assert labels == {"Read: /a.py", "Bash: ls"}


def test_read_running_returns_age(fake_dirs: Path) -> None:
    _touch(tmpfiles.RUNNING_DIR / "job1", "Read: /a.py", age_sec=2.0)
    out = tui._read_running()
    assert len(out) == 1
    _, age = out[0]
    assert 1.5 < age < 3.0


def test_read_running_handles_missing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmpfiles, "RUNNING_DIR", tmp_path / "does-not-exist")
    assert tui._read_running() == []


def test_read_running_skips_orphans(fake_dirs: Path) -> None:
    """Non-.done files older than ORPHAN_THRESHOLD_SEC are treated as cancelled."""
    running = tmpfiles.RUNNING_DIR
    _touch(running / "fresh", "Read: /a.py", age_sec=2.0)
    _touch(running / "orphan", "Bash: stuck", age_sec=tmpfiles.ORPHAN_THRESHOLD_SEC + 60)

    out = tui._read_running()
    labels = {label for label, _age in out}
    assert labels == {"Read: /a.py"}


# ── _read_done ───────────────────────────────────────────────────────────────

def test_read_done_returns_all_done_files(fake_dirs: Path) -> None:
    """No time filter — Stop hook clears RUNNING_DIR at end-of-turn."""
    running = tmpfiles.RUNNING_DIR
    _touch(running / "fresh.done", "Bash: ls", age_sec=2.0)
    _touch(running / "older.done", "Bash: rm", age_sec=30.0)
    _touch(running / "running", "Read: /a.py")  # not .done — excluded

    out = tui._read_done()
    labels = {label for label, _age in out}
    assert labels == {"Bash: ls", "Bash: rm"}


def test_read_done_newest_first(fake_dirs: Path) -> None:
    running = tmpfiles.RUNNING_DIR
    _touch(running / "old.done", "Bash: a", age_sec=8.0)
    _touch(running / "new.done", "Bash: b", age_sec=1.0)
    _touch(running / "mid.done", "Bash: c", age_sec=4.0)

    out = tui._read_done()
    labels = [label for label, _age in out]
    assert labels == ["Bash: b", "Bash: c", "Bash: a"]


def test_read_done_cache_populates_and_reuses(fake_dirs: Path) -> None:
    """Cache avoids per-tick stat+read on entries it's already seen."""
    running = tmpfiles.RUNNING_DIR
    _touch(running / "a.done", "Bash: ls", age_sec=1.0)
    _touch(running / "b.done", "Bash: pwd", age_sec=2.0)

    cache: dict[str, tuple[str, float]] = {}
    out1 = tui._read_done(cache=cache)
    assert {label for label, _ in out1} == {"Bash: ls", "Bash: pwd"}
    assert set(cache) == {"a.done", "b.done"}

    # Mutate file contents on disk; cache should be authoritative, so labels
    # returned should still reflect the cached value, proving no re-read.
    (running / "a.done").write_text("CHANGED")
    out2 = tui._read_done(cache=cache)
    labels = {label for label, _ in out2}
    assert "Bash: ls" in labels and "CHANGED" not in labels


def test_read_done_cache_evicts_disappeared_files(fake_dirs: Path) -> None:
    """When session_end_clear wipes RUNNING_DIR, the cache should drain too."""
    running = tmpfiles.RUNNING_DIR
    _touch(running / "a.done", "Bash: ls", age_sec=1.0)

    cache: dict[str, tuple[str, float]] = {}
    tui._read_done(cache=cache)
    assert "a.done" in cache

    (running / "a.done").unlink()
    out = tui._read_done(cache=cache)
    assert out == []
    assert cache == {}


def test_read_done_cache_picks_up_new_entries(fake_dirs: Path) -> None:
    running = tmpfiles.RUNNING_DIR
    _touch(running / "a.done", "Bash: ls", age_sec=1.0)

    cache: dict[str, tuple[str, float]] = {}
    tui._read_done(cache=cache)

    _touch(running / "b.done", "Bash: pwd", age_sec=0.5)
    out = tui._read_done(cache=cache)
    assert {label for label, _ in out} == {"Bash: ls", "Bash: pwd"}
    assert set(cache) == {"a.done", "b.done"}


# ── _tail_log ────────────────────────────────────────────────────────────────

def test_tail_log_returns_last_n_lines(fake_dirs: Path) -> None:
    tmpfiles.HOOK_LOG.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")
    out = tui._tail_log(n=5)
    assert out == ["line 16", "line 17", "line 18", "line 19", "line 20"]


def test_tail_log_handles_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmpfiles, "HOOK_LOG", tmp_path / "missing")
    assert tui._tail_log(n=10) == []


def test_tail_log_handles_empty_file(fake_dirs: Path) -> None:
    tmpfiles.HOOK_LOG.write_text("")
    assert tui._tail_log(n=10) == []


def test_tail_log_skips_blank_lines(fake_dirs: Path) -> None:
    tmpfiles.HOOK_LOG.write_text("a\n\nb\n\n\nc\n")
    assert tui._tail_log(n=10) == ["a", "b", "c"]


# ── _read_tool_count ─────────────────────────────────────────────────────────

def test_read_tool_count_missing_returns_zero(fake_dirs: Path) -> None:
    assert tui._read_tool_count() == 0


def test_read_tool_count_parses_value(fake_dirs: Path) -> None:
    tmpfiles.TOOL_COUNT.write_text("42")
    assert tui._read_tool_count() == 42


def test_read_tool_count_handles_garbage(fake_dirs: Path) -> None:
    tmpfiles.TOOL_COUNT.write_text("oops")
    assert tui._read_tool_count() == 0


# ── _fmt_age ─────────────────────────────────────────────────────────────────

def test_fmt_age() -> None:
    assert tui._fmt_age(0.42).endswith("ms")
    assert tui._fmt_age(3.14) == "3.1s"
    assert tui._fmt_age(125) == "2m05s"
