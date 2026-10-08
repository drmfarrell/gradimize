"""Tests for scripts/deidentify.py — the documented proof that Gradimize
can operate on fully de-identified extracts."""

import csv
import importlib.util
import sys
from pathlib import Path

import pytest
from sqlmodel import select

from src.core.ingest import DataIngestor
from src.database.models import Student

_spec = importlib.util.spec_from_file_location(
    "deidentify", Path(__file__).parent.parent / "scripts" / "deidentify.py"
)
deid = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deid)


@pytest.fixture
def raw_dir(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "students.csv").write_text(
        "id,name,email,class_standing,credits_earned,major,catalog_year\n"
        "900123456,Jane Doe,jdoe@school.edu,SR,98,BIOL,2023\n"
        "900654321,Bob Roe,broe@school.edu,FR,12,CHEM,2026\n"
    )
    (raw / "course_history.csv").write_text(
        "student_id,course_code,grade,semester,credits_earned\n"
        "900123456,BIO101,A,2023FA,3\n"
        "900654321,CHM101,B+,2026SP,3\n"
    )
    (raw / "rooms.csv").write_text(
        "id,building,number,capacity,room_type\nR1,Main,101,30,LECTURE\n"
    )
    return raw


def run_deid(raw, out, extra=()):
    argv = ["deidentify.py", str(raw), str(out), *extra]
    old_argv = sys.argv
    sys.argv = argv
    try:
        deid.main()
    finally:
        sys.argv = old_argv


def test_no_identifiers_leave_the_raw_dir(raw_dir, tmp_path):
    out = tmp_path / "out"
    run_deid(raw_dir, out)

    blob = (out / "students.csv").read_text() + (out / "course_history.csv").read_text()
    for identifier in ("900123456", "900654321", "Jane", "Doe", "jdoe",
                       "Bob", "Roe", "broe", "@school.edu"):
        assert identifier not in blob, f"identifier {identifier!r} leaked to output"

    header = (out / "students.csv").read_text().splitlines()[0]
    assert "name" not in header and "email" not in header


def test_pseudonyms_are_consistent_across_files_and_runs(raw_dir, tmp_path):
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    run_deid(raw_dir, out1)
    run_deid(raw_dir, out2)  # same key file (created in raw_dir on first run)

    def ids(path, col):
        with open(path, newline="") as f:
            return [r[col] for r in csv.DictReader(f)]

    s1 = ids(out1 / "students.csv", "id")
    h1 = ids(out1 / "course_history.csv", "student_id")
    assert set(s1) == set(h1), "same student must map to same pseudonym across files"
    assert s1 == ids(out2 / "students.csv", "id"), "re-runs must be deterministic"
    assert all(p.startswith("S") and len(p) == 13 for p in s1)


def test_different_keys_give_unlinkable_pseudonyms(raw_dir, tmp_path):
    out1, out2 = tmp_path / "o1", tmp_path / "o2"
    run_deid(raw_dir, out1, extra=["--key-file", str(tmp_path / "k1")])
    run_deid(raw_dir, out2, extra=["--key-file", str(tmp_path / "k2")])
    ids1 = (out1 / "students.csv").read_text()
    ids2 = (out2 / "students.csv").read_text()
    assert ids1.splitlines()[1] != ids2.splitlines()[1]


def test_mapping_file_reidentifies(raw_dir, tmp_path):
    out = tmp_path / "out"
    mapping = tmp_path / "mapping.csv"
    run_deid(raw_dir, out, extra=["--mapping", str(mapping)])

    with open(mapping, newline="") as f:
        m = {r["pseudonym"]: r["real_id"] for r in csv.DictReader(f)}
    assert set(m.values()) == {"900123456", "900654321"}


def test_non_csv_data_files_are_refused(raw_dir, tmp_path):
    """Regression: an .xlsx dropped in RAW_DIR used to be copied through with
    all identifiers intact. It must be refused, loudly, with a nonzero exit."""
    (raw_dir / "students.xlsx").write_bytes(b"binary-with-identifiers")
    (raw_dir / "time_slots.yaml").write_text("slots: []\n")
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as exc:
        run_deid(raw_dir, out)

    assert exc.value.code == 1
    assert not (out / "students.xlsx").exists(), ".xlsx must never reach the output"
    assert (out / "time_slots.yaml").exists(), "config yaml still passes through"
    assert (out / "students.csv").exists(), "CSV work still completes"


def test_extended_identifier_columns_are_covered(tmp_path):
    """Banner-flavored headers: PIDM pseudonymized, contact/DOB columns dropped."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "students.csv").write_text(
        "id,banner_id,preferred_name,email_address,phone_number,birth_date,"
        "zip_code,major\n"
        "900111222,12345,JD,jd@school.edu,555-0100,2004-05-06,19319,BIOL\n"
    )
    out = tmp_path / "out"
    run_deid(raw, out)

    text = (out / "students.csv").read_text()
    for leaked in ("JD", "jd@school.edu", "555-0100", "2004-05-06", "19319"):
        assert leaked not in text, f"{leaked!r} leaked to output"
    assert text.splitlines()[0] == "id,banner_id,major"

    with open(out / "students.csv", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["id"].startswith("S") and row["banner_id"].startswith("S")


def test_faculty_names_kept_but_contact_dropped(raw_dir, tmp_path):
    """faculty.csv keeps NAMES (they appear on the printed schedule and
    load_faculty needs them) but contact columns are not requested (July
    2026 amendment) and must be dropped even if delivered by mistake."""
    (raw_dir / "faculty.csv").write_text(
        "id,name,email,phone,max_credits\n"
        "F1,Dr. Smith,smith@school.edu,555-0100,12\n"
    )
    out = tmp_path / "out"
    run_deid(raw_dir, out)

    text = (out / "faculty.csv").read_text()
    assert "Dr. Smith" in text
    assert "smith@school.edu" not in text and "email" not in text.splitlines()[0]
    assert "555-0100" not in text
    assert "max_credits" in text.splitlines()[0]


def test_student_file_with_unrecognized_id_column_is_refused(raw_dir, tmp_path):
    """A renamed student file whose ID header isn't recognized must be REFUSED,
    not silently copied through with raw IDs intact."""
    (raw_dir / "students_2026FA.csv").write_text(
        "id,class_standing,major\n900111222,SR,BIOL\n"
    )
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as exc:
        run_deid(raw_dir, out)

    assert exc.value.code == 1
    assert not (out / "students_2026FA.csv").exists(), \
        "unrecognized student file must not reach the output"
    assert (out / "students.csv").exists(), "known files still processed"


def test_ambiguous_bare_id_roster_is_refused(raw_dir, tmp_path):
    """A student-shaped roster exported under a non-documented name (no
    'student'/'registration'/... in the filename) with a bare 'id' column
    must be refused, not silently shipped with real IDs intact."""
    (raw_dir / "roster.csv").write_text(
        "id,class_standing,major\n900111222,SR,BIOL\n"
    )
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as exc:
        run_deid(raw_dir, out)

    assert exc.value.code == 1
    assert not (out / "roster.csv").exists(), \
        "ambiguous bare-id file must not reach the output"
    assert (out / "students.csv").exists(), "known files still processed"


def test_capitalized_students_filename_is_treated_as_students_file(tmp_path):
    """'Students.csv' (capital S) must behave exactly like 'students.csv':
    the bare id is pseudonymized and name/email are dropped."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "Students.csv").write_text(
        "id,name,email,major\n900111222,Jane Doe,jdoe@school.edu,BIOL\n"
    )
    out = tmp_path / "out"
    run_deid(raw, out)

    text = (out / "Students.csv").read_text()
    assert "900111222" not in text
    assert "Jane" not in text and "jdoe@school.edu" not in text
    header = text.splitlines()[0]
    assert "name" not in header and "email" not in header
    with open(out / "Students.csv", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["id"].startswith("S")


def test_faculty_and_rooms_bare_id_pass_through_intact(raw_dir, tmp_path):
    """faculty.csv and rooms.csv are documented bare-id files: their IDs must
    stay intact (schedule INSTRUCTOR_ID / room links depend on it)."""
    (raw_dir / "faculty.csv").write_text(
        "id,name,email,max_credits\nF1,Dr. Smith,smith@school.edu,12\n"
    )
    out = tmp_path / "out"
    run_deid(raw_dir, out)

    faculty_text = (out / "faculty.csv").read_text()
    assert "F1" in faculty_text
    assert "Dr. Smith" in faculty_text

    rooms_text = (out / "rooms.csv").read_text()
    assert "R1" in rooms_text


def test_id_columns_flag_overrides_ambiguous_bare_id(tmp_path):
    """--id-columns id explicitly pseudonymizes a bare-id file instead of
    refusing it."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "cohort.csv").write_text(
        "id,class_standing,major\n900111222,SR,BIOL\n"
    )
    out = tmp_path / "out"
    run_deid(raw, out, extra=["--id-columns", "id"])

    text = (out / "cohort.csv").read_text()
    assert "900111222" not in text
    with open(out / "cohort.csv", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["id"].startswith("S")


def test_refusal_banner_prints_non_student_guidance(raw_dir, tmp_path, capsys):
    """The refused-files banner must tell the registrar what to do with
    refused files that contain no student data (policy docs, degree maps,
    meeting-time grids)."""
    (raw_dir / "students.xlsx").write_bytes(b"binary-with-identifiers")
    out = tmp_path / "out"

    with pytest.raises(SystemExit):
        run_deid(raw_dir, out)

    captured = capsys.readouterr()
    assert "deliver it directly alongside" in captured.out
    assert "this tool only needs to process files with" in captured.out


def test_ragged_extract_survives_ingest(tmp_path, db_manager):
    """Regression: a blank catalog_year/major cell in one row crashed the whole
    students ingest with int(NaN). Real extracts have blanks; keep good rows."""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "students.csv").write_text(
        "id,name,email,class_standing,credits_earned,major,catalog_year\n"
        "900123456,Jane Doe,jdoe@school.edu,SR,98,BIOL,2023\n"
        "900654321,Bob Roe,broe@school.edu,FR,12,,\n"      # blank major + year
        "900999999,No Credits,nc@school.edu,SO,,CHEM,2024\n"  # blank credits
    )
    out = tmp_path / "out"
    run_deid(raw, out)

    with db_manager.get_session() as session:
        ing = DataIngestor(session, out)
        students = ing.load_students()
        assert len(students) == 2, "blank-credits row skipped, others kept"
        assert any("credits_earned" in e for e in ing.errors)
        kept = {s.major: s for s in students}
        assert "" in kept and kept[""].catalog_year == 2024, (
            "blank major/catalog_year default instead of crashing"
        )


def test_deidentified_extracts_import_cleanly(raw_dir, tmp_path, db_manager):
    """End-to-end: Gradimize ingests de-identified files with zero errors."""
    out = tmp_path / "out"
    run_deid(raw_dir, out)

    with db_manager.get_session() as session:
        ing = DataIngestor(session, out)
        students = ing.load_students()
        assert len(students) == 2
        assert ing.errors == []

        for st in session.exec(select(Student)).all():
            assert st.id.startswith("S")
            assert st.name == f"Student {st.id}"  # placeholder, not a real name
            assert st.email == ""
