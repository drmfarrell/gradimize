"""Canary regression test: FERPA-protected values must NEVER reach run logs.

Sentinel values (ZZ9…) are planted in student-data files and driven through
the real pipelines — upload import (uploads → ingest / registrations), the
doctor, and the solve-stream tee. Afterwards every byte written to the log
directory is swept for every canary. If a future change routes a student
value into a run log (a lazy f-string, an unscrubbed exception, a new check
that forgets `protected=True`), this file fails.

The tests also assert the opposite direction, twice over:
- the OPERATOR-facing channels (errors lists, CheckResult.details) still
  carry the full values — redaction happens only in the run log; and
- the run log still carries the *useful* skeleton (row numbers, columns,
  shapes, counts) — a log that leaks nothing because it contains nothing
  would pass a naive sweep.
"""

import json
import os
from pathlib import Path

from src.core.doctor import run_health_checks
from src.core.generate import _teed
from src.core.runlog import RunLog
from src.core.uploads import import_data_file
from src.database.models import ClassStanding, Student, StudentCourseHistory

# Sentinels: distinctive, greppable, never legitimate data. One per protected
# field family, including a spaced one (scrub_text's known weak spot — it must
# be kept out structurally, not by scrubbing).
CANARY_SID = "ZZ9SID001"
CANARY_SID2 = "ZZ9SID002"
CANARY_SID3 = "ZZ9SID003"
CANARY_NAME = "ZZ9NAME SPACED"
CANARY_CREDITS = "ZZ9CRED1"
CANARY_STANDING = "ZZ9STAND"
CANARY_GRADE = "ZZ9GRADE"
ALL_CANARIES = [
    CANARY_SID, CANARY_SID2, CANARY_SID3, CANARY_NAME,
    CANARY_CREDITS, CANARY_STANDING, CANARY_GRADE,
    # Substring guard: "ZZ9" alone catches mangled/truncated leaks too.
    "ZZ9",
]


def _log_dir() -> Path:
    # Set per-test by the autouse _runlog_isolation fixture in conftest.py.
    return Path(os.environ["GRADIMIZE_LOG_DIR"])


def _log_text() -> str:
    d = _log_dir()
    if not d.exists():
        return ""
    return "\n".join(p.read_text() for p in sorted(d.glob("run-*.jsonl")))


def _log_events() -> list[dict]:
    return [json.loads(line) for line in _log_text().splitlines() if line]


def _assert_no_canaries(text: str) -> None:
    for canary in ALL_CANARIES:
        assert canary not in text, f"canary {canary!r} leaked into a run log"


# ─────────────────────────────────────────────────────────────────────
# Import pipeline: students.csv
# ─────────────────────────────────────────────────────────────────────

def test_students_import_logs_shapes_not_values(db_manager):
    csv = (
        "id,name,class_standing,credits_earned,major,catalog_year\n"
        f"S001,{CANARY_NAME},FR,12,BIO,2024\n"          # good (name col dropped)
        f"{CANARY_SID},Ok Name,FR,{CANARY_CREDITS},BIO,2024\n"   # bad credits
        f"{CANARY_SID2},Ok Name,{CANARY_STANDING},15,BIO,2024\n"  # bad standing
    ).encode()

    with db_manager.get_session() as session:
        result = import_data_file(session, "students", csv, replace=True)

    assert result.rows_loaded == 1
    # Operator channel keeps full detail — redaction is log-only.
    assert any(CANARY_SID in e for e in result.errors)
    assert any(CANARY_CREDITS in e for e in result.errors)

    text = _log_text()
    _assert_no_canaries(text)

    # The log must still be a useful debugging record: structured events
    # naming file/row/column and the value's SHAPE.
    events = _log_events()
    bad_credits = [e for e in events if e.get("event") == "invalid_value"
                   and e.get("column") == "credits_earned"]
    assert bad_credits, "expected an invalid_value event for credits_earned"
    ev = bad_credits[0]
    assert ev["file"] == "students.csv"
    assert ev["row"] == 3  # 1-based + header
    assert ev["shape"].startswith("text(")  # a shape, not a value
    assert [e for e in events if e.get("event") == "invalid_value"
            and e.get("column") == "class_standing"]
    # The dropped identifier column is reported by NAME (diagnostic: the
    # extract wasn't de-identified upstream).
    dropped = [e for e in events if e.get("event") == "identifier_columns_dropped"]
    assert dropped and "name" in dropped[0]["columns"]
    assert any(e.get("event") == "import_done" for e in events)


def test_course_history_import_logs_exception_type_not_row(db_manager):
    csv = (
        "student_id,course_code,grade,semester,credits_earned\n"
        f"{CANARY_SID},BIO101,{CANARY_GRADE},2024FA,3\n"   # parses fine
        f"{CANARY_SID2},BIO102,B,2024FA,{CANARY_CREDITS}\n"  # int() raises
    ).encode()

    with db_manager.get_session() as session:
        result = import_data_file(session, "course_history", csv, replace=True)

    assert result.rows_loaded == 1
    assert any(CANARY_CREDITS in e for e in result.errors)  # operator channel

    text = _log_text()
    _assert_no_canaries(text)

    skipped = [e for e in _log_events() if e.get("event") == "row_skipped"]
    assert skipped, "expected a row_skipped event"
    ev = skipped[0]
    assert ev["exception"] == "ValueError"
    assert ev["row"] == 3
    # Per-column shapes: enough to see WHICH column is malformed and how.
    assert ev["shapes"]["credits_earned"].startswith("text(")
    assert ev["shapes"]["grade"] == "text(tiny, letters)"  # "B", not the canary row


def test_registrations_import_logs_row_numbers_not_ids(db_manager):
    csv = (
        "student_id,term,crn,course_code\n"
        f"{CANARY_SID},202680,10001,NOPE101\n"   # unknown student
    ).encode()

    with db_manager.get_session() as session:
        result = import_data_file(session, "registrations", csv, replace=True)

    assert result.rows_loaded == 0
    assert any(CANARY_SID in e for e in result.errors)  # operator channel

    text = _log_text()
    _assert_no_canaries(text)

    events = _log_events()
    unknown = [e for e in events if e.get("event") == "unknown_student"]
    assert unknown and unknown[0]["row"] == 1 and "sid" not in unknown[0]
    summary = [e for e in events if e.get("event") == "registrations_import"]
    assert summary and summary[0]["skipped_unknown_student"] == 1
    assert "errors" not in summary[0]  # the id-bearing list stays out


# ─────────────────────────────────────────────────────────────────────
# Doctor: protected checks are redacted in the log, intact for the operator
# ─────────────────────────────────────────────────────────────────────

def test_doctor_redacts_protected_details_in_runlog(db_manager):
    with db_manager.get_session() as session:
        session.add(Student(
            id=CANARY_SID3, name="X", email="", class_standing=ClassStanding.SR,
            credits_earned=10, major="BIO", catalog_year=2024,
        ))  # SR with 10 credits → standing-credits-mismatch (protected)
        session.add(StudentCourseHistory(
            student_id=CANARY_SID, course_code="NOPE101",
            grade=CANARY_GRADE, semester="2024FA", credits_earned=3,
        ))  # orphan sid (protected) + unknown grade (protected)
            # + unknown course NOPE101 (NOT protected — catalog-level)
        session.commit()

        results = run_health_checks(session, deep=False)

    by_name = {r.name: r for r in results}
    # Operator-facing details keep the values.
    assert any(CANARY_SID in d for d in by_name["history-orphan-students"].details)
    assert by_name["history-orphan-students"].protected
    assert any(CANARY_GRADE in d for d in by_name["unknown-grades"].details)
    assert not by_name["history-unknown-courses"].protected
    assert by_name["standing-credits-mismatch"].protected
    assert by_name["history-orphan-students"].as_dict()["protected"] is True

    text = _log_text()
    _assert_no_canaries(text)

    checks = {e["name"]: e for e in _log_events() if e.get("event") == "check"}
    # Protected → counts only, flagged as redacted.
    assert checks["history-orphan-students"]["details_redacted"] is True
    assert "details" not in checks["history-orphan-students"]
    assert checks["history-orphan-students"]["count"] == 1
    assert checks["unknown-grades"]["details_redacted"] is True
    # Non-protected → details verbatim (course codes are allowed and useful).
    assert any("NOPE101" in d
               for d in checks["history-unknown-courses"]["details"])


# ─────────────────────────────────────────────────────────────────────
# Solve-stream tee: error messages scrubbed in the log, verbatim to the UI
# ─────────────────────────────────────────────────────────────────────

def test_solve_tee_scrubs_error_events(tmp_path):
    received = []
    rl = RunLog("solve", directory=tmp_path)
    emit = _teed(lambda etype, **f: received.append((etype, f)), rl)

    emit("log", msg="Created 42 sections to schedule.")
    emit("error", message=f"KeyError: '{CANARY_SID}'")
    rl.close()

    # Client stream is untouched — the UI shows the real message.
    assert received[1][1]["message"] == f"KeyError: '{CANARY_SID}'"

    text = "\n".join(p.read_text() for p in tmp_path.glob("run-*.jsonl"))
    _assert_no_canaries(text)
    events = [json.loads(line) for line in text.splitlines()]
    assert any(e.get("msg") == "Created 42 sections to schedule."
               for e in events)  # safe events verbatim
    err = next(e for e in events if e["event"] == "error")
    assert "KeyError" in err["message"]  # diagnostic template survives
