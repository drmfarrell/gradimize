#!/usr/bin/env python3
"""De-identify Banner/DegreeWorks extracts before they reach Gradimize.

Run by the DATA OWNER (registrar/IT) on the raw extracts. The output
directory contains pseudonymized copies safe to hand to the scheduling
server; the secret key file (and optional re-identification mapping)
STAYS WITH THE DATA OWNER. The Gradimize server never receives a student
name, student email, or real student ID — and no email/phone/address for
anyone, faculty included (faculty NAMES are kept: they appear on the
printed schedule).

What it does, per file:
  students.csv        id -> pseudonym; name/email columns DROPPED
  course_history.csv  student_id -> pseudonym
  registrations.csv   student_id -> pseudonym
  faculty.csv         names KEPT; email/phone/address columns DROPPED
  audit_course_needed.csv (DegreeWorks)
                      STUDENT_ID -> pseudonym; STUDENT_NAME dropped if present
  any other .csv      copied unchanged unless --id-columns names a column
  *.yaml / *.yml      copied through (config files, never student data)
  anything else       REFUSED (.xlsx, .json, ...) — this tool can only
                      de-identify CSV; convert the export to CSV and re-run

Pseudonyms are deterministic: HMAC-SHA256(key, real_id), truncated. The
same student gets the same pseudonym in every file and every refresh (as
long as the same key file is reused), so records still link up — but
without the key the pseudonym cannot be reversed.

Usage:
  python scripts/deidentify.py RAW_DIR OUT_DIR [--key-file KEY]
                               [--mapping MAPPING.csv] [--id-columns col1,col2]

  --key-file   Secret key (created with 0600 perms on first run if absent).
               Keep it with the raw data; NEVER copy it to the Gradimize server.
  --mapping    Optionally write a pseudonym->real_id table for the data owner's
               records (e.g. to answer "which student is S3f9a2c1d0?").
  --id-columns Extra column names to pseudonymize in any file.
"""

import argparse
import csv
import hashlib
import hmac
import os
import re
import secrets
import sys
from pathlib import Path

# Columns holding a student identifier, by normalized (lowercase) header name.
# PIDM is Banner's internal person key — just as identifying as the visible ID;
# SPRIDEN_ID is the Banner source field named in the data request.
DEFAULT_ID_COLUMNS = {"student_id", "studentid", "banner_id", "pidm",
                      "student_pidm", "spriden_id", "sid", "stu_id", "person_id"}
# In students.csv specifically, the primary key column is the student id.
STUDENTS_FILE = "students.csv"
# Direct identifiers that must not leave the data owner.
DROP_COLUMNS = {"name", "email", "student_name", "first_name", "last_name",
                "middle_name", "preferred_name", "preferred_first_name",
                "phone", "phone_number", "email_address", "email_addr", "e-mail",
                "address", "home_address", "mailing_address",
                "address1", "address2", "street", "city", "state",
                "zip", "zip_code", "postal_code",
                "dob", "date_of_birth", "birth_date", "birthdate", "ssn",
                "netid", "username", "user_name"}
# faculty.csv is directory data: NAMES are kept (they appear on the
# printed schedule and load_faculty needs them) but contact details are
# not requested (July 2026 amendment) and are dropped even if delivered.
CONTACT_COLUMNS = {"email", "email_address", "email_addr", "e-mail",
                   "phone", "phone_number",
                   "address", "home_address", "mailing_address",
                   "address1", "address2", "street", "city", "state",
                   "zip", "zip_code", "postal_code"}
KEEP_NAME_FILES = {"faculty.csv"}
# Filenames that look like per-student data. If one of these produces ZERO
# pseudonymized columns (unrecognized headers, renamed file, odd delimiter),
# passing it through unchanged would leak IDs — refuse instead.
STUDENT_FILE_PATTERN = re.compile(
    r"student|registration|enroll|history|audit|transcript|grade|demand|spriden",
    re.IGNORECASE,
)
# Files where a bare "id" column is a KNOWN, documented identifier (student
# id in students.csv, faculty id in faculty.csv, room id in rooms.csv). Any
# other file with a bare "id" column is ambiguous: it could be a renamed
# student roster (leak) or a legitimate non-student file — refuse rather
# than guess.
BARE_ID_OK_FILES = {"students.csv", "faculty.csv", "rooms.csv"}

# Non-CSV files that are safe to pass through (config, never student data).
SAFE_PASSTHROUGH_SUFFIXES = {".yaml", ".yml"}
# Spreadsheet/data formats this tool cannot de-identify — must be refused,
# never silently copied with identifiers intact.
REFUSED_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".json", ".txt", ".tsv", ".dat"}

PSEUDONYM_PREFIX = "S"
PSEUDONYM_HEX_CHARS = 12  # 48 bits — collision-safe for any campus-size cohort


def load_or_create_key(key_path: Path) -> bytes:
    if key_path.exists():
        key = key_path.read_bytes().strip()
        if len(key) < 32:
            sys.exit(f"Key file {key_path} is too short — refusing to run.")
        return key
    key = secrets.token_hex(32).encode()
    # O_EXCL + 0600 at creation: no instant where the key is world-readable.
    fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key + b"\n")
    print(f"Created new secret key: {key_path}")
    print("KEEP THIS FILE with the raw data. Do NOT copy it to the Gradimize server.")
    return key


def pseudonymize(real_id: str, key: bytes) -> str:
    digest = hmac.new(key, real_id.strip().encode(), hashlib.sha256).hexdigest()
    return PSEUDONYM_PREFIX + digest[:PSEUDONYM_HEX_CHARS]


def process_csv(
    src: Path,
    dst: Path,
    key: bytes,
    id_columns: set[str],
    treat_id_as_student: bool,
    mapping: dict[str, str],
    drop_columns: set[str] = DROP_COLUMNS,
) -> tuple[int, list[str], int, bool]:
    """Rewrite one CSV.

    Returns (rows_processed, transformed_column_names, n_pseudonymized_cols,
    bare_id_unhandled) where bare_id_unhandled is True if the header has a
    bare "id" column that was NOT pseudonymized.
    """
    try:
        with open(src, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except UnicodeDecodeError:
        sys.exit(
            f"{src.name} is not UTF-8 text (likely a cp1252/Excel export). "
            "Nothing was leaked. In Excel use File > Save As > 'CSV UTF-8', "
            "then re-run."
        )
    if not rows:
        dst.write_text("")
        return 0, [], 0, False

    header = rows[0]
    norm = [h.strip().lower().replace(" ", "_") for h in header]

    pseudo_idx = [
        i for i, h in enumerate(norm)
        if h in id_columns or (treat_id_as_student and h == "id")
    ]
    drop_idx = [i for i, h in enumerate(norm) if h in drop_columns]
    keep = [i for i in range(len(header)) if i not in drop_idx]

    out_rows = [[header[i] for i in keep]]
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        row = row + [""] * (len(header) - len(row))  # ragged-row safety
        new_row = list(row)
        for i in pseudo_idx:
            real = row[i]
            if real.strip():
                pseudo = pseudonymize(real, key)
                prev = mapping.get(pseudo)
                if prev is not None and prev != real.strip():
                    sys.exit(
                        f"Pseudonym collision: {prev!r} and {real.strip()!r} "
                        f"both map to {pseudo}. Increase PSEUDONYM_HEX_CHARS "
                        "and re-run (astronomically unlikely; do not ignore)."
                    )
                mapping[pseudo] = real.strip()
                new_row[i] = pseudo
        out_rows.append([new_row[i] for i in keep])

    with open(dst, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out_rows)

    transformed = [header[i] for i in pseudo_idx] + [f"-{header[i]}" for i in drop_idx]
    bare_id_unhandled = "id" in norm and norm.index("id") not in pseudo_idx
    return len(out_rows) - 1, transformed, len(pseudo_idx), bare_id_unhandled


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw_dir", type=Path, help="Directory of raw extracts (read-only)")
    ap.add_argument("out_dir", type=Path, help="Output directory for de-identified copies")
    ap.add_argument("--key-file", type=Path, default=None,
                    help="Secret key file (default: RAW_DIR/deidentify.key)")
    ap.add_argument("--mapping", type=Path, default=None,
                    help="Optionally write pseudonym->real_id mapping CSV (data owner only)")
    ap.add_argument("--id-columns", default="",
                    help="Comma-separated extra column names to pseudonymize")
    args = ap.parse_args()

    if not args.raw_dir.is_dir():
        sys.exit(f"Not a directory: {args.raw_dir}")
    if args.out_dir.resolve() == args.raw_dir.resolve():
        sys.exit("OUT_DIR must differ from RAW_DIR — refusing to overwrite raw extracts.")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.mapping and args.mapping.resolve().is_relative_to(args.out_dir.resolve()):
        sys.exit("--mapping must not be inside OUT_DIR — it would be delivered "
                 "with the de-identified data.")
    key_path = args.key_file or (args.raw_dir / "deidentify.key")
    key = load_or_create_key(key_path)

    id_columns = set(DEFAULT_ID_COLUMNS)
    id_columns.update(c.strip().lower() for c in args.id_columns.split(",") if c.strip())

    mapping: dict[str, str] = {}
    refused: list[str] = []
    print(f"\nDe-identifying {args.raw_dir} -> {args.out_dir}\n")
    for src in sorted(args.raw_dir.glob("*.csv")):
        name_l = src.name.lower()
        dst = args.out_dir / src.name
        n, transformed, n_pseudo, bare_id_unhandled = process_csv(
            src, dst, key, id_columns,
            treat_id_as_student=(name_l == STUDENTS_FILE),
            mapping=mapping,
            drop_columns=(CONTACT_COLUMNS if name_l in KEEP_NAME_FILES
                          else DROP_COLUMNS),
        )
        # Fail safe, not silent: a file that LOOKS like per-student data but
        # yielded no pseudonymized column (renamed file, unrecognized header,
        # pipe-delimited export) must not pass through with raw IDs intact.
        if n_pseudo == 0 and STUDENT_FILE_PATTERN.search(src.name):
            dst.unlink(missing_ok=True)
            refused.append(
                f"{src.name} (no student-ID column recognized — rename the "
                "file/columns to the documented layout or use --id-columns)"
            )
            print(f"  {src.name}: REFUSED — looks like student data but no "
                  "ID column was recognized")
            continue
        # A bare "id" column outside the known-safe files is ambiguous: it
        # could be a renamed student roster (real IDs leaking) or a
        # legitimate non-student file. Refuse rather than guess.
        if bare_id_unhandled and name_l not in BARE_ID_OK_FILES:
            dst.unlink(missing_ok=True)
            refused.append(
                f"{src.name} (ambiguous bare 'id' column — rename the file "
                "to its documented layout name, or pass --id-columns id)"
            )
            print(f"  {src.name}: REFUSED — ambiguous bare 'id' column")
            continue
        note = f" ({', '.join(transformed)})" if transformed else " (copied unchanged)"
        print(f"  {src.name}: {n} rows{note}")

    # Non-CSV files: only known-safe config formats pass through. Anything that
    # could carry student data (.xlsx etc.) is REFUSED — copying it through
    # unmodified would leak identifiers straight past the whole pipeline.
    for src in sorted(args.raw_dir.iterdir()):
        if src == key_path or src.suffix == ".csv":
            continue
        if src.is_dir():
            print(f"  {src.name}/: subdirectory ignored — place extracts at "
                  "the top level of RAW_DIR")
            continue
        if not src.is_file():
            continue
        if src.suffix.lower() in SAFE_PASSTHROUGH_SUFFIXES:
            (args.out_dir / src.name).write_bytes(src.read_bytes())
            print(f"  {src.name}: copied unchanged (config file)")
        elif src.suffix.lower() == ".csv":
            refused.append(f"{src.name} (rename with a lowercase .csv extension)")
            print(f"  {src.name}: REFUSED — rename to lowercase .csv and re-run")
        else:
            refused.append(src.name)
            print(f"  {src.name}: REFUSED — cannot de-identify this format")

    if args.mapping:
        args.mapping.unlink(missing_ok=True)
        fd = os.open(args.mapping, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["pseudonym", "real_id"])
            for pseudo, real in sorted(mapping.items()):
                w.writerow([pseudo, real])
        print(f"\nRe-identification mapping written to {args.mapping} "
              f"({len(mapping)} students) — data owner only.")

    print(f"\nDone. {len(mapping)} distinct student IDs pseudonymized.")
    print("Deliver OUT_DIR to Gradimize. Keep RAW_DIR, the key file, and any "
          "mapping file with the data owner.")

    if refused:
        print("\n" + "!" * 70)
        print("! WARNING: the following files were NOT copied to the output")
        print("! directory because this tool cannot de-identify them:")
        for name in refused:
            print(f"!   - {name}")
        print("! Export/convert them to .csv and run this tool again.")
        print("! Do NOT deliver these files to the Gradimize server as-is.")
        print("! If a refused file contains NO student data (a policy document,")
        print("! degree map, or meeting-time grid), deliver it directly alongside")
        print("! the output folder — this tool only needs to process files with")
        print("! student records.")
        print("!" * 70)
        sys.exit(1)


if __name__ == "__main__":
    main()
