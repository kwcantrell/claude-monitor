#!/usr/bin/env python3
"""Single entrypoint for all Claude Code hooks.

Reads a hook payload from stdin, routes to a handler keyed by hook_event_name,
catches every exception so a buggy hook never blocks Claude Code.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Allow importing the `monitor` package regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import db, tmpfiles  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _realpath(p: str | None) -> str | None:
    if not p:
        return None
    try:
        return os.path.realpath(p)
    except OSError:
        return p


def _extract_read_content(tool_response: object) -> str | None:
    """Pull the file-content string out of a Read response (multiple shapes possible)."""
    if isinstance(tool_response, str):
        return tool_response
    if not isinstance(tool_response, dict):
        return None
    # common shapes
    if isinstance(tool_response.get("content"), str):
        return tool_response["content"]
    file_obj = tool_response.get("file")
    if isinstance(file_obj, dict) and isinstance(file_obj.get("content"), str):
        return file_obj["content"]
    # nested content list (assistant-message style)
    content = tool_response.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    return None


def _command_norm(cmd: str | None) -> str | None:
    if not cmd:
        return None
    return " ".join(cmd.strip().split())


def _tool_use_id(payload: dict) -> str:
    tuid = payload.get("tool_use_id")
    if tuid:
        return str(tuid)
    # synthesize a deterministic fallback so Pre/Post can match
    parts = [
        str(payload.get("session_id", "")),
        str(payload.get("tool_name", "")),
        json.dumps(payload.get("tool_input", {}), sort_keys=True, default=str),
    ]
    return "fallback-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


# ── Handlers ──────────────────────────────────────────────────────────────

def handle_session_start(p: dict) -> None:
    tmpfiles.session_start_reset()
    conn = db.connect()
    try:
        db.upsert_session(
            conn,
            session_id=p.get("session_id", "unknown"),
            workspace=(p.get("workspace") or {}).get("current_dir") or p.get("cwd"),
            transcript_path=p.get("transcript_path"),
            started_at=time.time(),
        )
    finally:
        conn.close()
    # remember session_id for cross-hook fallback
    try:
        Path("/tmp/claude-monitor-session").write_text(p.get("session_id", ""))
    except OSError:
        pass


def handle_pre_tool_use(p: dict) -> None:
    tool_name = p.get("tool_name", "")
    tool_input = p.get("tool_input") or {}
    tuid = _tool_use_id(p)
    session_id = p.get("session_id", "unknown")

    file_path = _realpath(tool_input.get("file_path"))
    command = _command_norm(tool_input.get("command"))
    label = tmpfiles.label_for(tool_name, tool_input)
    tmpfiles.mark_running(tuid, label)

    conn = db.connect()
    try:
        db.insert_tool_call(
            conn,
            tool_use_id=tuid,
            session_id=session_id,
            tool_name=tool_name,
            file_path=file_path,
            command=command,
            input_json=json.dumps(tool_input, default=str),
            started_at=time.time(),
        )
    finally:
        conn.close()


def handle_post_tool_use(p: dict) -> None:
    tool_name = p.get("tool_name", "")
    tool_input = p.get("tool_input") or {}
    tool_response = p.get("tool_response")
    tuid = _tool_use_id(p)
    session_id = p.get("session_id", "unknown")
    is_error = bool(p.get("is_error"))
    now = time.time()

    label = tmpfiles.label_for(tool_name, tool_input)
    tmpfiles.mark_done(tuid, label)

    # Hash Read responses; track edits as file_changes.
    content_sha = None
    response_bytes = None
    if tool_name == "Read" and not is_error:
        content = _extract_read_content(tool_response)
        if content is not None:
            response_bytes = len(content.encode("utf-8", errors="replace"))
            content_sha = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

    conn = db.connect()
    try:
        db.complete_tool_call(
            conn,
            tool_use_id=tuid,
            completed_at=now,
            response_bytes=response_bytes,
            content_sha256=content_sha,
            was_error=is_error,
        )
        if content_sha and tool_input.get("file_path"):
            db.insert_file_read(
                conn,
                content_sha256=content_sha,
                session_id=session_id,
                file_path=_realpath(tool_input.get("file_path")) or "",
                read_at=now,
                response_bytes=response_bytes or 0,
            )
        if tool_name in EDIT_TOOLS and not is_error:
            fp = _realpath(tool_input.get("file_path") or tool_input.get("notebook_path"))
            if fp:
                db.insert_file_change(
                    conn,
                    file_path=fp,
                    changed_at=now,
                    source="edit",
                    session_id=session_id,
                )
    finally:
        conn.close()


def handle_post_tool_use_failure(p: dict) -> None:
    tuid = _tool_use_id(p)
    tool_name = p.get("tool_name", "")
    tool_input = p.get("tool_input") or {}
    tmpfiles.mark_done(tuid, tmpfiles.label_for(tool_name, tool_input), failed=True)
    conn = db.connect()
    try:
        db.insert_tool_failure(
            conn,
            tool_use_id=tuid,
            session_id=p.get("session_id", "unknown"),
            tool_name=tool_name,
            file_path=_realpath(tool_input.get("file_path")),
            error_text=str(p.get("error") or p.get("error_text") or p.get("tool_response") or ""),
            failed_at=time.time(),
        )
    finally:
        conn.close()


def handle_user_prompt_submit(p: dict) -> None:
    session_id = p.get("session_id", "unknown")
    conn = db.connect()
    try:
        was_interrupt = db.has_inflight_tools(conn, session_id)
        db.insert_user_prompt(
            conn,
            session_id=session_id,
            prompt_text=p.get("prompt") or p.get("user_prompt"),
            was_interrupt=was_interrupt,
            idle_seconds=p.get("idle_seconds"),
            submitted_at=time.time(),
        )
    finally:
        conn.close()


def handle_permission_denied(p: dict) -> None:
    tool_input = p.get("tool_input") or {}
    detail = (
        tool_input.get("command")
        or tool_input.get("url")
        or tool_input.get("file_path")
        or p.get("detail")
        or ""
    )
    conn = db.connect()
    try:
        db.insert_permission_event(
            conn,
            session_id=p.get("session_id", "unknown"),
            tool_name=p.get("tool_name", ""),
            decision="denied",
            detail=str(detail)[:512],
            occurred_at=time.time(),
        )
    finally:
        conn.close()


def handle_instructions_loaded(p: dict) -> None:
    path = p.get("file_path") or p.get("path")
    if not path:
        return
    size = p.get("response_bytes") or p.get("size")
    conn = db.connect()
    try:
        db.insert_instructions_loaded(
            conn,
            session_id=p.get("session_id", "unknown"),
            file_path=_realpath(path) or path,
            load_reason=p.get("load_reason") or p.get("reason"),
            response_bytes=int(size) if size is not None else None,
            loaded_at=time.time(),
        )
    finally:
        conn.close()


def handle_file_changed(p: dict) -> None:
    path = p.get("file_path") or p.get("path")
    if not path:
        return
    conn = db.connect()
    try:
        db.insert_file_change(
            conn,
            file_path=_realpath(path) or path,
            changed_at=time.time(),
            source="external",
            session_id=p.get("session_id"),
        )
    finally:
        conn.close()


def handle_stop(p: dict) -> None:
    """End-of-turn: clear live-TUI state only. Does not stamp session end."""
    tmpfiles.session_end_clear()


def handle_session_end(p: dict) -> None:
    conn = db.connect()
    try:
        db.end_session(conn, p.get("session_id", "unknown"), time.time())
    finally:
        conn.close()
    tmpfiles.session_end_clear()


HANDLERS = {
    "SessionStart": handle_session_start,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
    "PostToolUseFailure": handle_post_tool_use_failure,
    "UserPromptSubmit": handle_user_prompt_submit,
    "PermissionDenied": handle_permission_denied,
    "InstructionsLoaded": handle_instructions_loaded,
    "FileChanged": handle_file_changed,
    "SessionEnd": handle_session_end,
    "Stop": handle_stop,
}


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        tmpfiles.log_error(f"dispatch parse error: {e}")
        return

    event = payload.get("hook_event_name") or ""
    handler = HANDLERS.get(event)
    if handler is None:
        tmpfiles.log_error(f"unknown hook event: {event}")
        return

    try:
        handler(payload)
    except Exception as e:  # noqa: BLE001
        tmpfiles.log_error(f"dispatch {event} error: {e}")


if __name__ == "__main__":
    main()
