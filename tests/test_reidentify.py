"""Tests for scripts/reidentify.py — the registrar-side companion to
scripts/deidentify.py that turns delivered pseudonymized outputs back
into identified copies, on the registrar's machine, with the registrar's
key."""

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

_deid_spec = importlib.util.spec_from_file_location(
    "deidentify", Path(__file__).parent.parent / "scripts" / "deidentify.py"
)
deid = importlib.util.module_from_spec(_deid_spec)
_deid_spec.loader.exec_module(deid)

_reid_spec = importlib.util.spec_from_file_location(
    "reidentify", Path(__file__).parent.parent / "scripts" / "reidentify.py"
)
reid = importlib.util.module_from_spec(_reid_spec)
_reid_spec.loader.exec_module(reid)


def run_deid(raw, out, extra=()):
    argv = ["deidentify.py", str(raw), str(out), *extra]
    old_argv = sys.argv
    sys.argv = argv
    try:
        deid.main()
    finally:
        sys.argv = old_argv


def run_reid(delivered, out, extra=()):
    argv = ["reidentify.py", str(delivered), str(out), *extra]
    old_argv = sys.argv
    sys.argv = argv
    try:
        reid.main()
    finally:
        sys.argv = old_argv


@pytest.fixture
def raw_dir(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "students.csv").write_text(
        "id,name,email,class_standing,credits_earned,major,catalog_year\n"
        "900123456,Jane Doe,jdoe@school.edu,SR,98,BIOL,2023\n"
        "900654321,Bob Roe,broe@school.edu,FR,12,CHEM,2026\n"
    )
    return raw


def _pseudonyms_for(raw_dir, tmp_path):
    """De-identify raw_dir and return (key_path, mapping dict, pseudonym for
    900123456)."""
    out = tmp_path / "deid_out"
    mapping_path = tmp_path / "mapping.csv"
    run_deid(raw_dir, out, extra=["--mapping", str(mapping_path)])
    key_path = raw_dir / "deidentify.key"
    with open(mapping_path, newline="") as f:
        mapping = {r["pseudonym"]: r["real_id"] for r in csv.DictReader(f)}
    pseudo = next(p for p, r in mapping.items() if r == "900123456")
    return key_path, mapping_path, mapping, pseudo


def _fabricate_delivered(tmp_path, pseudo):
    """Build a fake DELIVERED_DIR: a roster csv, a sheet .md, an .ics."""
    delivered = tmp_path / "delivered"
    (delivered / "sheets").mkdir(parents=True)
    (delivered / "ical").mkdir(parents=True)
    (delivered / "student_schedules.csv").write_text(
        "student_id,course_code,section\n" f"{pseudo},BIO101,001\n"
    )
    (delivered / "sheets" / f"{pseudo}.md").write_text(
        f"# Schedule for {pseudo}\n\nBIO101 section 001\n"
    )
    (delivered / "ical" / f"{pseudo}.ics").write_text(
        "BEGIN:VCALENDAR\n"
        f"SUMMARY:Schedule for {pseudo}\n"
        "END:VCALENDAR\n"
    )
    return delivered


def test_roundtrip_with_key_and_roster(raw_dir, tmp_path):
    key_path, mapping_path, mapping, pseudo = _pseudonyms_for(raw_dir, tmp_path)
    delivered = _fabricate_delivered(tmp_path, pseudo)
    out = tmp_path / "reid_out"

    run_reid(delivered, out, extra=[
        "--key-file", str(key_path), "--roster", str(raw_dir / "students.csv"),
    ])

    csv_text = (out / "student_schedules.csv").read_text()
    assert "900123456" in csv_text
    assert pseudo not in csv_text

    real_md = out / "sheets" / "900123456.md"
    assert real_md.exists()
    assert "900123456" in real_md.read_text()
    assert pseudo not in real_md.read_text()

    real_ics = out / "ical" / "900123456.ics"
    assert real_ics.exists()
    assert "900123456" in real_ics.read_text()


def test_roundtrip_with_mapping_file(raw_dir, tmp_path):
    key_path, mapping_path, mapping, pseudo = _pseudonyms_for(raw_dir, tmp_path)
    delivered = _fabricate_delivered(tmp_path, pseudo)
    out = tmp_path / "reid_out"

    run_reid(delivered, out, extra=["--mapping", str(mapping_path)])

    csv_text = (out / "student_schedules.csv").read_text()
    assert "900123456" in csv_text
    assert pseudo not in csv_text
    assert (out / "sheets" / "900123456.md").exists()
    assert (out / "ical" / "900123456.ics").exists()


def test_pseudonymize_parity_with_deidentify():
    key = b"a" * 32
    for real_id in ("900123456", "abc-123", "0000000001", "  900123456  "):
        assert reid.pseudonymize(real_id, key) == deid.pseudonymize(real_id, key)


def test_unmatched_pseudonym_reported_but_run_succeeds(raw_dir, tmp_path, capsys):
    key_path, mapping_path, mapping, pseudo = _pseudonyms_for(raw_dir, tmp_path)
    delivered = _fabricate_delivered(tmp_path, pseudo)
    # An extra pseudonym-shaped id in the csv that has no mapping entry.
    unmapped_pseudo = "S" + ("0" * 12)
    with open(delivered / "student_schedules.csv", "a") as f:
        f.write(f"{unmapped_pseudo},CHM101,002\n")
    out = tmp_path / "reid_out"

    run_reid(delivered, out, extra=["--mapping", str(mapping_path)])

    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert unmapped_pseudo in captured.out
    text = (out / "student_schedules.csv").read_text()
    assert unmapped_pseudo in text  # left as pseudonym, not fabricated


def test_out_dir_equal_delivered_dir_refused(raw_dir, tmp_path):
    key_path, mapping_path, mapping, pseudo = _pseudonyms_for(raw_dir, tmp_path)
    delivered = _fabricate_delivered(tmp_path, pseudo)

    with pytest.raises(SystemExit) as exc:
        run_reid(delivered, delivered, extra=["--mapping", str(mapping_path)])
    assert "differ" in str(exc.value) or "OUT_DIR" in str(exc.value)


def test_stray_key_file_not_copied(raw_dir, tmp_path):
    key_path, mapping_path, mapping, pseudo = _pseudonyms_for(raw_dir, tmp_path)
    delivered = _fabricate_delivered(tmp_path, pseudo)
    # Simulate a key file accidentally left inside the delivered directory.
    (delivered / "deidentify.key").write_bytes(key_path.read_bytes())
    out = tmp_path / "reid_out"

    run_reid(delivered, out, extra=["--mapping", str(mapping_path)])

    assert not (out / "deidentify.key").exists()
