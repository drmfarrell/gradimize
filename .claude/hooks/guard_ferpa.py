#!/usr/bin/env python3
"""PreToolUse guard: deny Claude's tools access to FERPA-zone student data.

Layer 2 of the isolation ladder (USER_GUIDE "Real-data isolation & run
analysis"): mechanical tool-call denial. Layer 1 is repo layout + .gitignore;
layer 3 — the only enforcement that holds against a malfunctioning agent —
is unix permissions (data zone owned by a separate service user, mode 700).

Blocked, for Read/Grep/Glob/Edit/Write/NotebookEdit paths and anywhere in a
Bash command's text:
  - the live data zone: /mnt/nvme/gradimize-data and $GRADIMIZE_DATA_DIR
  - the repo data/ tree, EXCEPT the synthetic data/sample/, data/README.md,
    and data/semester_guide_template.csv (mirrors .gitignore's re-includes)
  - repo backups/ (DB backups contain the same student rows)
  - student-level exports: exports/**.csv, exports/**.xlsx (rosters land
    there via the app and scripts/export_student_schedules.py)
  - the doctor's --show-values flag (it prints student ids into the
    terminal, i.e. straight into Claude's context)

Known limits (documented, not bugs): this guard sees tool inputs, not
program behavior — a script Claude *writes* could open the DB internally.
That class is covered by the run-log design (programs log shapes, not
values) and, ultimately, by layer 3. This guard exists to stop every
realistic accident: a stray cat/grep/sqlite3, a Read of an extract, a
--show-values run.

Fail-closed: ANY internal error (including unparseable stdin) blocks the
call with exit 2. Hook contract: exit 0 = allow; exit 2 = block, stderr is
shown to Claude.

Test standalone (see tests at bottom of file's history / USER_GUIDE):
  echo '{"tool_name":"Read","tool_input":{"file_path":"data/students.csv"}}' \
    | python3 .claude/hooks/guard_ferpa.py ; echo "exit=$?"
"""

import json
import os
import re
import sys

# repo root = two levels up from .claude/hooks/
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Synthetic, deliberately tracked files under data/ — the only readable part.
SAFE_DATA_PREFIXES = ("data/sample/",)
SAFE_DATA_FILES = ("data/sample", "data/README.md", "data/semester_guide_template.csv")

# Bash-command text patterns. Lookbehind excludes word chars and '-' so
# "database/", "metadata/" and "GRADIMIZE_DATA_DIR" don't match, while
# "./data/x", "repo/data/x" do. Lookahead exempts the synthetic files.
_RE_REPO_DATA = re.compile(
    r"(?<![\w-])data/(?!sample/|sample\b|README\.md|semester_guide_template)"
)
_RE_BACKUPS = re.compile(r"(?<![\w-])backups/")
_RE_EXPORT_FILES = re.compile(r"exports/[^\s'\"]*\.(csv|xlsx)\b")
_RE_SHOW_VALUES = re.compile(r"--show-values\b")


def data_zones() -> list[str]:
    zones = ["/mnt/nvme/gradimize-data"]
    env = os.environ.get("GRADIMIZE_DATA_DIR", "").strip()
    if env:
        zones.append(os.path.realpath(os.path.expanduser(env)))
    return zones


def path_block_reason(raw: str) -> str | None:
    if not raw:
        return None
    # realpath: resolves any existing symlink prefix, so a link into the
    # zone doesn't launder the path.
    rp = os.path.realpath(os.path.expanduser(str(raw)))
    for zone in data_zones():
        if rp == zone or rp.startswith(zone + os.sep):
            return f"'{raw}' is inside the FERPA data zone ({zone})"

    repo_data = os.path.join(REPO, "data")
    if rp == repo_data or rp.startswith(repo_data + os.sep):
        rel = os.path.relpath(rp, REPO).replace(os.sep, "/")
        if rel not in SAFE_DATA_FILES and not rel.startswith(SAFE_DATA_PREFIXES):
            return (f"'{raw}' is under the repo data/ tree (live extracts may "
                    f"land there; only data/sample/ is readable)")

    repo_backups = os.path.join(REPO, "backups")
    if rp == repo_backups or rp.startswith(repo_backups + os.sep):
        return f"'{raw}' is under backups/ (DB backups hold student rows)"

    if "/exports/" in rp + "/" and rp.endswith((".csv", ".xlsx")):
        return (f"'{raw}' is a student-level export (csv/xlsx under exports/ "
                f"carry rosters)")
    return None


def command_block_reason(cmd: str) -> str | None:
    if not cmd:
        return None
    lowered = cmd  # paths are case-sensitive on this box; keep as-is
    for zone in data_zones():
        if zone in lowered:
            return f"command references the FERPA data zone ({zone})"
    if "gradimize-data" in lowered:
        return "command references the FERPA data zone (gradimize-data)"
    if _RE_SHOW_VALUES.search(lowered):
        return ("--show-values prints student ids/grades into the terminal "
                "(Claude's context); run it yourself in your own shell")
    if _RE_REPO_DATA.search(lowered):
        return ("command references the repo data/ tree (only data/sample/ "
                "is readable — live extracts may land in data/)")
    if _RE_BACKUPS.search(lowered):
        return "command references backups/ (DB backups hold student rows)"
    m = _RE_EXPORT_FILES.search(lowered)
    if m:
        return (f"command references a student-level export "
                f"({m.group(0)}) — csv/xlsx under exports/ carry rosters")
    return None


def main() -> int:
    payload = json.load(sys.stdin)
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    reason = None
    if tool == "Bash":
        reason = command_block_reason(str(tool_input.get("command", "")))
    else:
        # Read/Edit/Write: file_path. NotebookEdit: notebook_path.
        # Grep/Glob: path (and a pattern that can itself target the zone).
        for key in ("file_path", "notebook_path", "path"):
            if tool_input.get(key):
                reason = path_block_reason(str(tool_input[key]))
                if reason:
                    break
        if reason is None and tool_input.get("pattern"):
            reason = command_block_reason(str(tool_input["pattern"]))

    if reason:
        print(f"FERPA guard: blocked — {reason}. Debug from the run logs "
              f"(logs/run-*.jsonl) or the safe report "
              f"(scripts/run_report.py) instead.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # fail CLOSED: a guard that can't see can't approve
        print(f"FERPA guard: internal error ({type(e).__name__}: {e}) — "
              f"failing closed.", file=sys.stderr)
        sys.exit(2)
