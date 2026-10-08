#!/usr/bin/env python3
"""Collect a paste-safe support bundle for debugging with an AI assistant.

Usage:
    python scripts/bug_report.py [--out FILE] [--lines N] [--no-doctor]

Writes one markdown file (default: <logs>/bug-report-<timestamp>.md) holding
everything a remote helper needs to diagnose a problem: versions, config,
row counts, redacted health checks, and the tails of the app log and the
newest run log. Every section is FERPA-safe by design — no student-level
values ever appear — so the whole file can be pasted into a chat session.

Each section is collected independently: if one collector fails (broken DB,
missing file), the error text is embedded and the rest still generates.
"""

import argparse
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.runlog import runlog_dir  # noqa: E402


def _section(title: str, body_fn) -> str:
    try:
        body = body_fn()
    except Exception as exc:  # a broken section is itself useful signal
        body = f"~~~\ncollector failed: {type(exc).__name__}: {exc}\n~~~"
    return f"## {title}\n\n{body}\n"


def collect_environment() -> str:
    lines = [
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Platform: {platform.platform()}",
        f"- Python: {sys.version.split()[0]} ({sys.executable})",
    ]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        n_dirty = len(dirty.splitlines()) if dirty else 0
        lines.append(f"- Git commit: {commit or 'unknown'}"
                     + (f" (+{n_dirty} locally modified files)" if n_dirty else " (clean)"))
    except Exception as exc:
        lines.append(f"- Git commit: unavailable ({exc})")

    from importlib.metadata import PackageNotFoundError, version
    pkgs = ["ortools", "fastapi", "sqlmodel", "sqlalchemy", "pydantic",
            "textual", "pandas", "networkx", "uvicorn"]
    versions = []
    for p in pkgs:
        try:
            versions.append(f"{p} {version(p)}")
        except PackageNotFoundError:
            versions.append(f"{p} MISSING")
    lines.append("- Packages: " + ", ".join(versions))
    return "\n".join(lines)


def collect_config() -> str:
    import yaml

    from src.core.scheduler_config import SchedulerConfig
    cfg = SchedulerConfig.load_default()
    digest = {
        "solver_settings": cfg.solver_settings,
        "hard_constraints": cfg.hard_constraints,
        "soft_constraints": cfg.soft_constraints,
        "section_settings": getattr(cfg, "section_settings", None),
    }
    return "~~~yaml\n" + yaml.safe_dump(digest, sort_keys=False) + "~~~"


def collect_db_counts() -> str:
    from sqlmodel import Session, func, select

    from src.database import models
    from src.database.connection import get_engine

    tables = ["Student", "Course", "Section", "SectionMeeting", "SectionEnrollment",
              "Faculty", "Room", "TimeSlot", "ScheduleVersion", "StudentDemand",
              "FinalsSchedule"]
    engine = get_engine()
    out = [f"- DB engine: {engine.url}"]
    with Session(engine) as session:
        for name in tables:
            model = getattr(models, name, None)
            if model is None:
                continue
            n = session.exec(select(func.count()).select_from(model)).one()
            out.append(f"- {name}: {n}")
    return "\n".join(out)


def collect_doctor() -> str:
    from sqlmodel import Session

    from src.core.doctor import overall_status, run_health_checks
    from src.database.connection import create_tables, get_engine

    engine = get_engine()
    create_tables(engine)
    with Session(engine) as session:
        results = run_health_checks(session, deep=False)
    lines = [f"Overall: **{overall_status(results).upper()}** (quick checks only; "
             f"run `scripts/doctor.py` for the full pre-flight)"]
    for r in results:
        d = r.as_dict()
        detail = ""
        if d.get("details"):
            if r.protected:
                detail = f" [{len(d['details'])} detail row(s) redacted]"
            else:
                shown = d["details"][:3]
                detail = " — " + "; ".join(str(x) for x in shown)
                if len(d["details"]) > 3:
                    detail += f" … (+{len(d['details']) - 3} more)"
        lines.append(f"- {d['status'].upper():5s} {d['name']}: {d['summary']}{detail}")
    return "\n".join(lines)


def _tail(path: Path, n: int) -> str:
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    body = "\n".join(lines[-n:])
    header = f"(last {min(n, len(lines))} of {len(lines)} lines)"
    return f"`{path}` {header}\n\n~~~\n{body}\n~~~"


def collect_app_log(n_lines: int) -> str:
    from src.core.logsetup import log_file_path
    logfile = log_file_path()
    if not logfile.exists():
        return (f"`{logfile}` does not exist yet — the app has not logged "
                "anything since file logging was enabled (or crashed before startup).")
    return _tail(logfile, n_lines)


def collect_run_logs(n_lines: int) -> str:
    logs = sorted(runlog_dir().glob("run-*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return "No run logs found (no imports/solves/doctor runs recorded yet)."
    listing = "\n".join(
        f"- {p.name} ({p.stat().st_size} bytes, "
        f"{datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec='seconds')})"
        for p in logs[:5]
    )
    return listing + "\n\nNewest run log tail:\n\n" + _tail(logs[0], n_lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Collect a paste-safe support bundle")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output file (default: <logs>/bug-report-<timestamp>.md)")
    ap.add_argument("--lines", type=int, default=150,
                    help="How many lines of each log tail to include (default 150)")
    ap.add_argument("--no-doctor", action="store_true",
                    help="Skip the data health checks (use if the DB itself is broken)")
    args = ap.parse_args()

    out = args.out or (runlog_dir() / f"bug-report-{datetime.now():%Y%m%d-%H%M%S}.md")
    out.parent.mkdir(parents=True, exist_ok=True)

    parts = [
        "# Gradimize bug report\n",
        "Generated by `scripts/bug_report.py`. Safe to paste into a support chat:\n"
        "every section is student-data-free by design (counts, codes, config,\n"
        "and redacted health checks only).\n",
        _section("Environment", collect_environment),
        _section("Solver / constraint config", collect_config),
        _section("Database row counts", collect_db_counts),
    ]
    if not args.no_doctor:
        parts.append(_section("Data health (redacted)", collect_doctor))
    parts.append(_section("Application log", lambda: collect_app_log(args.lines)))
    parts.append(_section("Run logs", lambda: collect_run_logs(args.lines)))
    parts.append(
        "## What to do with this file\n\n"
        "Paste the whole file into your Claude (or other assistant) session and\n"
        "describe what you were doing when the problem happened. For questions\n"
        "about a generated schedule's *content*, also run\n"
        "`python scripts/run_report.py` and share that report — it is likewise\n"
        "student-data-free.\n"
    )

    out.write_text("\n".join(parts))
    print(f"Bug report written to: {out}")
    print("Review it if you like, then paste the whole file into your support chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
