"""`python3 -m monitor` — analyze | sessions | init-db | purge."""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime

from rich.console import Console
from rich.table import Table

from monitor import analyze, db

console = Console()


def _parse_duration(s: str) -> float:
    """Parse '7d', '12h', '30m' → seconds."""
    m = re.fullmatch(r"(\d+)([smhdw])", s.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {s!r}; expected e.g. 7d, 12h")
    n, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return float(n * mult)


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1048576:.1f}MB"


# ── render ────────────────────────────────────────────────────────────────

def _render_reads(report: analyze.AnalysisReport) -> None:
    console.rule("[bold cyan]REPEATED READS[/]  (same content across sessions)")
    if not report.repeated_reads:
        console.print("[dim]none above threshold[/]")
        return
    for r in report.repeated_reads:
        first_path = r.paths[0] if r.paths else "(unknown)"
        console.print(
            f"[bold]sha256:{r.content_sha256[:12]}…[/]  {first_path}  ({_fmt_bytes(r.response_bytes)})"
        )
        console.print(
            f"  read {r.read_count}× across {r.session_count} sessions, last {_fmt_ts(r.last_read)}"
        )
        if len(r.paths) > 1:
            console.print(f"  also seen at: {', '.join(r.paths[1:4])}")
        slug = analyze.suggest_skill_name(r.paths)
        console.print(
            f"  [green]→ SUGGEST SKILL[/]: \"{slug}\"  "
            f"(cache keyed by sha256:{r.content_sha256[:12]}…; invalidate on file_changes)"
        )
        console.print()


def _render_bloat(report: analyze.AnalysisReport) -> None:
    if not report.rules_bloat:
        return
    console.rule("[bold cyan]CLAUDE.md / RULES BLOAT[/]")
    for r in report.rules_bloat:
        pct = round(100.0 * r.sessions_loaded / r.total_sessions) if r.total_sessions else 0
        console.print(
            f"[bold]{r.file_path}[/]  ({_fmt_bytes(r.avg_bytes)})"
        )
        console.print(
            f"  loaded {r.load_count}× across {r.sessions_loaded}/{r.total_sessions} sessions ({pct}%)"
        )
        console.print(
            "  [green]→ SUGGEST CLAUDE.md SLIM[/]: split into on-demand skill"
        )
        console.print()


def _render_commands(report: analyze.AnalysisReport) -> None:
    console.rule("[bold cyan]REPEATED COMMANDS[/]")
    if not report.repeated_commands:
        console.print("[dim]none above threshold[/]")
        return
    table = Table(show_edge=False)
    table.add_column("tool")
    table.add_column("command")
    table.add_column("count", justify="right")
    table.add_column("sessions", justify="right")
    table.add_column("denials", justify="right")
    for c in report.repeated_commands[:15]:
        cmd = c.command if len(c.command) <= 60 else c.command[:57] + "…"
        table.add_row(c.tool_name, cmd, str(c.invocations), str(c.session_count), str(c.denial_count))
    console.print(table)


def _render_denials(report: analyze.AnalysisReport) -> None:
    console.rule("[bold cyan]PERMISSION FRICTION[/]")
    if not report.repeated_denials:
        console.print("[dim]none above threshold[/]")
        return
    for d in report.repeated_denials:
        detail = d.detail[:60]
        console.print(f"[bold]{d.tool_name}[/]({detail})  denied {d.denial_count}× across {d.session_count} sessions")
        entry = analyze.suggest_allowlist_entry(d.tool_name, d.detail)
        console.print(f"  [green]→ SUGGEST ALLOWLIST[/]: add {entry} to ~/.claude/settings.json")
        console.print()


def _render_quality(report: analyze.AnalysisReport) -> None:
    q = report.quality
    if q is None:
        return
    console.rule("[bold cyan]QUALITY SIGNALS[/]")
    parts = [f"Interrupts: {q.interrupt_total} across {q.interrupt_sessions} sessions"]
    if q.top_interrupted_tools:
        parts.append("(top: " + ", ".join(f"{t} {n}×" for t, n in q.top_interrupted_tools) + ")")
    console.print(" ".join(parts))
    console.print(f"Tool failures: {q.failure_total}")
    if q.top_failure_paths:
        console.print(
            "  top paths: " + ", ".join(f"{p} ({n})" for p, n in q.top_failure_paths)
        )


# ── commands ──────────────────────────────────────────────────────────────

def cmd_analyze(args: argparse.Namespace) -> int:
    since = None if args.all else (time.time() - _parse_duration(args.since))
    db.init_db()
    conn = db.connect()
    try:
        report = analyze.run(conn, since)
    finally:
        conn.close()
    window = "all history" if args.all else f"since {_fmt_ts(since)}"
    console.print(f"\n[bold]monitor analyze[/]  [dim]{window}[/]\n")
    _render_reads(report)
    _render_bloat(report)
    _render_commands(report)
    _render_denials(report)
    _render_quality(report)
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT session_id, workspace, started_at, ended_at, tool_count, interrupt_count
               FROM sessions ORDER BY started_at DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()
    finally:
        conn.close()
    table = Table(title="recent sessions", show_edge=False)
    table.add_column("started")
    table.add_column("session_id")
    table.add_column("workspace")
    table.add_column("tools", justify="right")
    table.add_column("interrupts", justify="right")
    table.add_column("ended")
    for r in rows:
        table.add_row(
            _fmt_ts(r["started_at"]),
            r["session_id"][:10] + "…" if r["session_id"] and len(r["session_id"]) > 10 else (r["session_id"] or ""),
            (r["workspace"] or "")[-40:],
            str(r["tool_count"] or 0),
            str(r["interrupt_count"] or 0),
            _fmt_ts(r["ended_at"]) if r["ended_at"] else "[dim](open)[/]",
        )
    console.print(table)
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    db.init_db()
    console.print(f"[green]✓[/] schema applied to {db.DB_PATH}")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    cutoff = time.time() - _parse_duration(args.older_than)
    conn = db.connect()
    try:
        counts = db.purge_older_than(conn, cutoff)
    finally:
        conn.close()
    console.print(f"purged rows older than {_fmt_ts(cutoff)}")
    for table, n in counts.items():
        console.print(f"  {table}: {n}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="monitor")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="cross-session aggregations + suggestions")
    a.add_argument("--since", default="7d", help="window, e.g. 7d, 30d (default: 7d)")
    a.add_argument("--all", action="store_true", help="ignore --since and use full history")
    a.set_defaults(func=cmd_analyze)

    s = sub.add_parser("sessions", help="recent session summary")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    i = sub.add_parser("init-db", help="create the database and schema")
    i.set_defaults(func=cmd_init_db)

    pp = sub.add_parser("purge", help="delete rows older than a window")
    pp.add_argument("--older-than", default="30d", help="e.g. 7d, 30d (default: 30d)")
    pp.set_defaults(func=cmd_purge)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
