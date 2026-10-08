#!/usr/bin/env python3
"""Re-identify Gradimize deliverables — the registrar-side mirror of
scripts/deidentify.py.

Run by the DATA OWNER (registrar/IT) on the delivered outputs (per-student
schedule sheets, .ics calendars, the student_schedules.csv roster, etc.),
on THEIR machine, with THEIR key. It turns pseudonyms (e.g. S3f9a2c1d0ab)
back into real student IDs so the registrar can match a printed schedule
to a name. Gradimize's server never sees this tool run, and the output of
this tool must never be sent back to the scheduling server.

You need EITHER:
  --mapping MAPPING.csv    the pseudonym,real_id table deidentify.py can
                           write with --mapping (fastest, no recompute)
OR
  --key-file KEY --roster ROSTER.csv
                           regenerate the mapping on the fly from the same
                           secret key and the raw student roster (the same
                           file deidentify.py reads students.csv from, or
                           any CSV with a student-ID column)

What it does, per file in DELIVERED_DIR (recursively, sheets/ and ical/
included):
  *.csv                cells matching a pseudonym AND present in the
                        mapping are rewritten to the real ID; matches
                        found in the file but absent from the mapping are
                        counted and reported (not treated as an error)
  *.md / *.ics / *.txt  copied through; pseudonym occurrences inside the
                        text are replaced; a file whose STEM is a mapped
                        pseudonym is renamed to the real ID
  anything else         copied through unchanged (structure preserved)
  *.key / the mapping file, if found inside DELIVERED_DIR
                        SKIPPED — never copied to OUT_DIR

Usage:
  python scripts/reidentify.py DELIVERED_DIR OUT_DIR --mapping MAPPING.csv
  python scripts/reidentify.py DELIVERED_DIR OUT_DIR --key-file KEY --roster ROSTER.csv

The output directory contains identified student records. Handle it as
you would any other student record — it must not be sent back to the
Gradimize scheduling server.
"""

import argparse
import csv
import hashlib
import hmac
import re
import sys
from pathlib import Path

# --- Kept in sync with scripts/deidentify.py -------------------------------
# This tool must run standalone on the registrar's machine (it ships next to
# deidentify.py in the outgoing data-request package, not imported from it),
# so the pseudonym algorithm and constants are DUPLICATED here. If
# deidentify.py's pseudonymize()/PSEUDONYM_PREFIX/PSEUDONYM_HEX_CHARS ever
# change, this file MUST change to match, or re-identification will silently
# fail to find any pseudonyms. tests/test_reidentify.py has a parity test
# that imports both modules and checks they agree on sample IDs.
PSEUDONYM_PREFIX = "S"
PSEUDONYM_HEX_CHARS = 12  # 48 bits


def pseudonymize(real_id: str, key: bytes) -> str:
    digest = hmac.new(key, real_id.strip().encode(), hashlib.sha256).hexdigest()
    return PSEUDONYM_PREFIX + digest[:PSEUDONYM_HEX_CHARS]


# --------------------------------------------------------------------------

PSEUDONYM_RE = re.compile(
    rf"{re.escape(PSEUDONYM_PREFIX)}[0-9a-f]{{{PSEUDONYM_HEX_CHARS}}}"
)
PSEUDONYM_FULL_RE = re.compile(rf"^{PSEUDONYM_RE.pattern}$")

# Same recognized student-ID column names as deidentify.py, so --roster can
# be pointed at students.csv (or any raw extract with a student ID column).
DEFAULT_ID_COLUMNS = {"student_id", "studentid", "banner_id", "pidm",
                      "student_pidm", "spriden_id", "sid", "stu_id", "person_id"}

# Text file suffixes whose CONTENTS (not just filename) get pseudonym
# occurrences replaced — schedule sheets and calendars print the id in body.
TEXT_SUFFIXES = {".md", ".ics", ".txt"}


def load_key(key_path: Path) -> bytes:
    if not key_path.exists():
        sys.exit(f"Key file not found: {key_path}")
    key = key_path.read_bytes().strip()
    if len(key) < 32:
        sys.exit(f"Key file {key_path} is too short — refusing to run.")
    return key


def build_mapping_from_roster(roster_path: Path, key: bytes) -> dict[str, str]:
    """Read a raw roster CSV and compute pseudonym -> real_id for every row,
    using the same student-ID column recognition as deidentify.py."""
    try:
        with open(roster_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except UnicodeDecodeError:
        sys.exit(
            f"{roster_path.name} is not UTF-8 text (likely a cp1252/Excel "
            "export). In Excel use File > Save As > 'CSV UTF-8', then re-run."
        )
    if not rows:
        sys.exit(f"{roster_path} is empty.")

    header = rows[0]
    norm = [h.strip().lower().replace(" ", "_") for h in header]
    id_idx = next(
        (i for i, h in enumerate(norm) if h in DEFAULT_ID_COLUMNS or h == "id"),
        None,
    )
    if id_idx is None:
        sys.exit(
            f"{roster_path.name}: no student-ID column recognized "
            f"(looked for 'id' or one of {sorted(DEFAULT_ID_COLUMNS)})."
        )

    mapping: dict[str, str] = {}
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        if id_idx >= len(row):
            continue
        real = row[id_idx].strip()
        if real:
            mapping[pseudonymize(real, key)] = real
    return mapping


def load_mapping_file(mapping_path: Path) -> dict[str, str]:
    try:
        with open(mapping_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            return {r["pseudonym"]: r["real_id"] for r in reader}
    except UnicodeDecodeError:
        sys.exit(f"{mapping_path.name} is not UTF-8 text.")
    except KeyError:
        sys.exit(
            f"{mapping_path.name} must have a header row 'pseudonym,real_id' "
            "(the format deidentify.py --mapping writes)."
        )


def process_csv(src: Path, dst: Path, mapping: dict[str, str]) -> tuple[int, set[str]]:
    """Rewrite one CSV, replacing mapped pseudonym cells with real IDs.

    Returns (cells_replaced, unmatched_pseudonyms_seen).
    """
    try:
        with open(src, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except UnicodeDecodeError:
        sys.exit(
            f"{src.name} is not UTF-8 text (likely a cp1252/Excel export). "
            "In Excel use File > Save As > 'CSV UTF-8', then re-run."
        )

    replaced = 0
    unmatched: set[str] = set()
    out_rows = []
    for row in rows:
        new_row = []
        for cell in row:
            value = cell
            if PSEUDONYM_FULL_RE.match(cell.strip()):
                pseudo = cell.strip()
                if pseudo in mapping:
                    value = mapping[pseudo]
                    replaced += 1
                else:
                    unmatched.add(pseudo)
            new_row.append(value)
        out_rows.append(new_row)

    with open(dst, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out_rows)

    return replaced, unmatched


def replace_in_text(text: str, mapping: dict[str, str]) -> tuple[str, int, set[str]]:
    """Replace pseudonym occurrences inside free text. Returns
    (new_text, replacements_made, unmatched_pseudonyms_seen)."""
    replacements = 0
    unmatched: set[str] = set()

    def _sub(m: re.Match) -> str:
        nonlocal replacements
        pseudo = m.group(0)
        if pseudo in mapping:
            replacements += 1
            return mapping[pseudo]
        unmatched.add(pseudo)
        return pseudo

    new_text = PSEUDONYM_RE.sub(_sub, text)
    return new_text, replacements, unmatched


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("delivered_dir", type=Path,
                    help="Directory of Gradimize deliverables (read-only)")
    ap.add_argument("out_dir", type=Path,
                    help="Output directory for re-identified copies")
    ap.add_argument("--mapping", type=Path, default=None,
                    help="pseudonym,real_id CSV written by deidentify.py --mapping")
    ap.add_argument("--key-file", type=Path, default=None,
                    help="Secret key file (used with --roster to regenerate the mapping)")
    ap.add_argument("--roster", type=Path, default=None,
                    help="Raw roster CSV with a student-ID column (used with --key-file)")
    args = ap.parse_args()

    if not args.delivered_dir.is_dir():
        sys.exit(f"Not a directory: {args.delivered_dir}")
    if args.out_dir.resolve() == args.delivered_dir.resolve():
        sys.exit("OUT_DIR must differ from DELIVERED_DIR — refusing to overwrite "
                 "delivered output.")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.mapping and not (args.key_file or args.roster):
        if args.mapping.resolve().is_relative_to(args.out_dir.resolve()):
            sys.exit("--mapping must not be inside OUT_DIR.")
        mapping = load_mapping_file(args.mapping)
        mapping_path_to_skip = args.mapping.resolve()
    elif args.key_file and args.roster and not args.mapping:
        if args.key_file.resolve().is_relative_to(args.out_dir.resolve()):
            sys.exit("--key-file must not be inside OUT_DIR.")
        if args.roster.resolve().is_relative_to(args.out_dir.resolve()):
            sys.exit("--roster must not be inside OUT_DIR.")
        key = load_key(args.key_file)
        mapping = build_mapping_from_roster(args.roster, key)
        mapping_path_to_skip = None
    else:
        sys.exit(
            "Provide either --mapping MAPPING.csv, or both --key-file KEY "
            "and --roster ROSTER.csv."
        )

    if not mapping:
        sys.exit("No pseudonym->real_id entries found — nothing to re-identify.")

    # Filenames to never copy out of DELIVERED_DIR, even if they happen to be
    # sitting in there by mistake (a key file or the mapping file itself).
    skip_names = {"deidentify.key"}
    if mapping_path_to_skip is not None:
        skip_names.add(args.mapping.name)
    if args.key_file is not None:
        skip_names.add(args.key_file.name)

    print(f"\nRe-identifying {args.delivered_dir} -> {args.out_dir}\n")
    print(f"Loaded {len(mapping)} pseudonym -> real_id mappings.\n")

    files_processed = 0
    cells_replaced = 0
    files_renamed = 0
    content_replacements = 0
    unmatched_pseudonyms: set[str] = set()

    for src in sorted(args.delivered_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(args.delivered_dir)
        if src.name.endswith(".key") or src.name in skip_names:
            print(f"  {rel}: SKIPPED (key/mapping file, not copied)")
            continue

        dst_dir = args.out_dir / rel.parent
        dst_dir.mkdir(parents=True, exist_ok=True)

        dst_name = src.name
        if PSEUDONYM_FULL_RE.match(src.stem) and src.stem in mapping:
            dst_name = mapping[src.stem] + src.suffix
            files_renamed += 1

        if src.suffix.lower() == ".csv":
            dst = dst_dir / dst_name
            replaced, unmatched = process_csv(src, dst, mapping)
            cells_replaced += replaced
            unmatched_pseudonyms |= unmatched
            print(f"  {rel}: {replaced} cells replaced")
        elif src.suffix.lower() in TEXT_SUFFIXES:
            try:
                text = src.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                sys.exit(f"{src.name} is not UTF-8 text.")
            new_text, n, unmatched = replace_in_text(text, mapping)
            content_replacements += n
            unmatched_pseudonyms |= unmatched
            (dst_dir / dst_name).write_text(new_text, encoding="utf-8")
            note = f" (renamed, {n} content replacements)" if dst_name != src.name \
                else f" ({n} content replacements)"
            print(f"  {rel}: copied{note}")
        else:
            (dst_dir / dst_name).write_bytes(src.read_bytes())
            note = " (renamed)" if dst_name != src.name else ""
            print(f"  {rel}: copied unchanged{note}")

        files_processed += 1

    print(f"\nDone. {files_processed} files processed, {cells_replaced} CSV cells "
          f"replaced, {files_renamed} files renamed, {content_replacements} "
          "in-content replacements.")
    if unmatched_pseudonyms:
        examples = ", ".join(sorted(unmatched_pseudonyms)[:5])
        print(f"WARNING: {len(unmatched_pseudonyms)} distinct pseudonym(s) found in "
              f"the delivered data with no entry in the mapping (examples: "
              f"{examples}). They were left as pseudonyms in the output.")

    print("\nIdentified output — handle as student records; do not send back "
          "to the scheduling server.")


if __name__ == "__main__":
    main()
