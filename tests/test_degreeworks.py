"""Tests for the DegreeWorks audit importer (src/core/degreeworks.py).

This is the DEFAULT demand source (SchedulerConfig.demand_source ==
"degreeworks"), so its column-detection tolerance, demand-row generation,
urgency-scoring integration, and clearing semantics matter a lot in
production even though it had zero test coverage before this file.
"""

from sqlmodel import select

from src.core.degreeworks import DegreeWorksImporter
from src.core.graph import PrerequisiteGraph
from src.database.models import (
    ClassStanding,
    Course,
    Prerequisite,
    StructureType,
    Student,
    StudentDemand,
)


def _student(id="S1", major="BIOL", standing=ClassStanding.SO, credits=45):
    return Student(
        id=id, name="", email="", major=major, class_standing=standing,
        credits_earned=credits, catalog_year=2024,
    )


def _course(code="BIOL101"):
    return Course(code=code, title="T", credits=3, structure_type=StructureType.STANDALONE)


# ─────────────────────────────────────────────────────────────────
# Column detection (_detect_columns) — the tolerated header vocabulary
# ─────────────────────────────────────────────────────────────────


def test_detect_columns_ods_native_names():
    """Primary DegreeWorks ODS view: AUDIT_COURSE_NEEDED columns."""
    mapping = DegreeWorksImporter._detect_columns(
        ["STUDENT_ID", "SUBJECT", "COURSE_NUMBER", "BLOCK_TYPE", "BLOCK_VALUE",
         "REQUIREMENT_TEXT"]
    )
    assert mapping["student_id"] == "STUDENT_ID"
    assert mapping["subject"] == "SUBJECT"
    assert mapping["course_number"] == "COURSE_NUMBER"
    assert mapping["block_type"] == "BLOCK_TYPE"
    assert mapping["block_value"] == "BLOCK_VALUE"
    assert "course_code" not in mapping


def test_detect_columns_simplified_course_code():
    """The simplified Gradimize format: single COURSE_CODE column."""
    mapping = DegreeWorksImporter._detect_columns(
        ["STUDENT_ID", "COURSE_CODE", "BLOCK_TYPE", "IS_REQUIRED"]
    )
    assert mapping["course_code"] == "COURSE_CODE"
    assert mapping["is_required"] == "IS_REQUIRED"
    assert "subject" not in mapping


def test_detect_columns_raw_dap_result_dtl_value1_value2():
    """Raw DAP_RESULT_DTL export uses generic VALUE1/VALUE2 columns."""
    mapping = DegreeWorksImporter._detect_columns(["STUDENT_ID", "VALUE1", "VALUE2"])
    assert mapping["subject"] == "VALUE1"
    assert mapping["course_number"] == "VALUE2"


def test_detect_columns_discipline_alias():
    mapping = DegreeWorksImporter._detect_columns(
        ["STUDENT_ID", "DISCIPLINE", "COURSE_NUMBER"]
    )
    assert mapping["subject"] == "DISCIPLINE"
    assert mapping["course_number"] == "COURSE_NUMBER"


def test_detect_columns_alternate_student_id_names():
    """Banner-style ID aliases: ID, BANNER_ID, PIDM, STU_ID."""
    for candidate in ("ID", "BANNER_ID", "PIDM", "STU_ID"):
        mapping = DegreeWorksImporter._detect_columns([candidate, "COURSE_CODE"])
        assert mapping["student_id"] == candidate, candidate


def test_detect_columns_prefers_student_id_over_aliases():
    mapping = DegreeWorksImporter._detect_columns(["ID", "STUDENT_ID", "COURSE_CODE"])
    assert mapping["student_id"] == "STUDENT_ID"


# ─────────────────────────────────────────────────────────────────
# import_audit_needs — end-to-end parsing/normalizing + demand generation
# ─────────────────────────────────────────────────────────────────


def test_import_ods_native_format_builds_course_code_from_subject_and_number(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text(
            "STUDENT_ID,SUBJECT,COURSE_NUMBER,BLOCK_TYPE,BLOCK_VALUE\n"
            "S1,BIOL,101,MAJOR,BIOL\n"
        )
        importer = DegreeWorksImporter(session)
        result = importer.import_audit_needs(csv_path)

        assert result == {
            "students": 1, "demands_created": 1, "rows_read": 1,
            "skipped_no_student": 0, "skipped_no_course": 0, "skipped_duplicate": 0,
        }
        demand = session.exec(select(StudentDemand)).one()
        assert demand.course_code == "BIOL101"
        assert demand.is_required is True
        assert demand.reason.startswith("[DegreeWorks]")
        assert "Major requirement (BIOL)" in demand.reason


def test_import_raw_value1_value2_format(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,VALUE1,VALUE2,BLOCK_TYPE\nS1,BIOL,101,MAJOR\n")
        result = DegreeWorksImporter(session).import_audit_needs(csv_path)

        assert result["demands_created"] == 1
        demand = session.exec(select(StudentDemand)).one()
        assert demand.course_code == "BIOL101"


def test_import_simplified_course_code_strips_embedded_space(db_manager, tmp_path):
    """course_code normalization: spaces stripped ("BIOL 101" -> "BIOL101")."""
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE,BLOCK_TYPE\nS1,BIOL 101,MAJOR\n")
        result = DegreeWorksImporter(session).import_audit_needs(csv_path)

        assert result["demands_created"] == 1
        demand = session.exec(select(StudentDemand)).one()
        assert demand.course_code == "BIOL101"


def test_import_alternate_id_column_end_to_end(db_manager, tmp_path):
    """PIDM (Banner-style alias) works as the student-id column, not just
    STUDENT_ID."""
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("PIDM,COURSE_CODE,BLOCK_TYPE\nS1,BIOL101,MAJOR\n")
        result = DegreeWorksImporter(session).import_audit_needs(csv_path)

        assert result["demands_created"] == 1
        assert result["students"] == 1


def test_import_block_type_defaults_to_major_when_column_absent(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student(major="CHEM"))
        session.add(_course("CHEM201"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE\nS1,CHEM201\n")
        DegreeWorksImporter(session).import_audit_needs(csv_path)

        demand = session.exec(select(StudentDemand)).one()
        assert demand.is_required is True  # MAJOR is a required block type
        assert "Major requirement (CHEM)" in demand.reason


def test_import_other_block_type_without_is_required_column_is_elective(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE,BLOCK_TYPE\nS1,BIOL101,OTHER\n")
        DegreeWorksImporter(session).import_audit_needs(csv_path)

        demand = session.exec(select(StudentDemand)).one()
        assert demand.is_required is False
        assert "General education / other" in demand.reason


def test_import_explicit_is_required_column_overrides_block_type(db_manager, tmp_path):
    """An explicit IS_REQUIRED=Y wins even for a normally-elective block type."""
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text(
            "STUDENT_ID,COURSE_CODE,BLOCK_TYPE,IS_REQUIRED\nS1,BIOL101,OTHER,Y\n"
        )
        DegreeWorksImporter(session).import_audit_needs(csv_path)

        demand = session.exec(select(StudentDemand)).one()
        assert demand.is_required is True


def test_import_skips_unknown_student_and_unknown_course(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student(id="S1"))
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text(
            "STUDENT_ID,COURSE_CODE,BLOCK_TYPE\n"
            "GHOST,BIOL101,MAJOR\n"
            "S1,NOTACOURSE,MAJOR\n"
            "S1,BIOL101,MAJOR\n"
        )
        result = DegreeWorksImporter(session).import_audit_needs(csv_path)

        assert result["rows_read"] == 3
        assert result["skipped_no_student"] == 1
        assert result["skipped_no_course"] == 1
        assert result["demands_created"] == 1
        assert result["students"] == 1
        assert result["skipped_duplicate"] == 0


def test_import_dedups_repeated_student_course_pairs_within_file(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text(
            "STUDENT_ID,COURSE_CODE,BLOCK_TYPE\n"
            "S1,BIOL101,MAJOR\n"
            "S1,BIOL101,MAJOR\n"
        )
        result = DegreeWorksImporter(session).import_audit_needs(csv_path)

        assert result["rows_read"] == 2
        assert result["demands_created"] == 1
        assert result["skipped_duplicate"] == 1
        assert len(session.exec(select(StudentDemand)).all()) == 1


def test_import_missing_student_id_column_raises(db_manager, tmp_path):
    with db_manager.get_session() as session:
        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("FOO,COURSE_CODE\nx,BIOL101\n")
        try:
            DegreeWorksImporter(session).import_audit_needs(csv_path)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "student ID column" in str(e)


def test_import_missing_course_code_columns_raises(db_manager, tmp_path):
    with db_manager.get_session() as session:
        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,FOO\nS1,x\n")
        try:
            DegreeWorksImporter(session).import_audit_needs(csv_path)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "course code columns" in str(e)


def test_import_file_not_found_raises(db_manager, tmp_path):
    with db_manager.get_session() as session:
        try:
            DegreeWorksImporter(session).import_audit_needs(tmp_path / "nope.csv")
            raise AssertionError("expected FileNotFoundError")
        except FileNotFoundError:
            pass


def test_import_header_only_csv_returns_zero_stats_no_exception(db_manager, tmp_path):
    with db_manager.get_session() as session:
        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE\n")
        result = DegreeWorksImporter(session).import_audit_needs(csv_path)

        assert result == {
            "students": 0, "demands_created": 0, "rows_read": 0,
            "skipped_no_student": 0, "skipped_no_course": 0, "skipped_duplicate": 0,
        }


# ─────────────────────────────────────────────────────────────────
# Urgency-scoring integration (shared src/core/urgency.py formula)
# ─────────────────────────────────────────────────────────────────


def test_import_urgency_senior_required_is_graduation_critical(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student(standing=ClassStanding.SR, credits=100))
        session.add(_course("BIOL101"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE,BLOCK_TYPE\nS1,BIOL101,MAJOR\n")
        DegreeWorksImporter(session).import_audit_needs(csv_path)

        demand = session.exec(select(StudentDemand)).one()
        # base(SR)=100 * senior(1.5) * required(1.5) = 225, no bottleneck graph
        assert demand.urgency_score == 225.0
        assert demand.is_graduation_critical is True
        assert demand.reason.startswith("[DegreeWorks] GRADUATION-CRITICAL")


def test_import_urgency_bottleneck_multiplier_via_prereq_graph(db_manager, tmp_path):
    """A course that unlocks >= bottleneck_threshold downstream courses gets
    the ×2 bottleneck multiplier, sourced from the shared PrerequisiteGraph."""
    with db_manager.get_session() as session:
        session.add(_student(standing=ClassStanding.FR, credits=0))
        session.add(_course("BIOL101"))
        for code in ("BIOL201", "BIOL301", "BIOL401"):
            session.add(_course(code))
            session.add(Prerequisite(course_code=code, prereq_code="BIOL101"))
        session.commit()

        graph = PrerequisiteGraph(session)
        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE,BLOCK_TYPE\nS1,BIOL101,MAJOR\n")
        DegreeWorksImporter(session, prereq_graph=graph, bottleneck_threshold=3).import_audit_needs(
            csv_path
        )

        demand = session.exec(
            select(StudentDemand).where(StudentDemand.course_code == "BIOL101")
        ).one()
        # base(FR)=10 * bottleneck(2.0) * required(1.5) = 30; not senior.
        assert demand.urgency_score == 30.0
        assert demand.is_graduation_critical is False
        assert "Unlocks 3 courses" in demand.reason


# ─────────────────────────────────────────────────────────────────
# Bulk-delete clearing path (clear_existing)
# ─────────────────────────────────────────────────────────────────


def test_clear_existing_true_wipes_stale_non_projected_but_keeps_projected(db_manager, tmp_path):
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.add(StudentDemand(student_id="S1", course_code="BIOL101", urgency_score=1.0,
                                   is_required=False, is_projected=False, reason="stale"))
        session.add(StudentDemand(student_id="S1", course_code="BIOL101", urgency_score=2.0,
                                   is_required=False, is_projected=True,
                                   reason="keep-me projected"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text("STUDENT_ID,COURSE_CODE,BLOCK_TYPE\nS1,BIOL101,MAJOR\n")
        DegreeWorksImporter(session).import_audit_needs(csv_path, clear_existing=True)

        demands = session.exec(select(StudentDemand)).all()
        reasons = {d.reason for d in demands}
        assert "keep-me projected" in reasons, "projected demand must survive the bulk delete"
        assert "stale" not in reasons, "stale non-projected demand must be cleared"
        assert any(r.startswith("[DegreeWorks]") for r in reasons)
        assert len(demands) == 2


def test_clear_existing_false_dedupes_against_prior_db_rows(db_manager, tmp_path):
    """With clear_existing=False, dedup must consider rows already committed
    to the DB — re-importing the same (student, course) pair keeps the
    existing demand row instead of creating a duplicate. A genuinely new
    pair from the same file still imports."""
    with db_manager.get_session() as session:
        session.add(_student())
        session.add(_course("BIOL101"))
        session.add(_course("CHEM101"))
        session.add(StudentDemand(student_id="S1", course_code="BIOL101", urgency_score=1.0,
                                   is_required=False, is_projected=False, reason="old-real"))
        session.commit()

        csv_path = tmp_path / "audit.csv"
        csv_path.write_text(
            "STUDENT_ID,COURSE_CODE,BLOCK_TYPE\n"
            "S1,BIOL101,MAJOR\n"
            "S1,CHEM101,MAJOR\n"
        )
        DegreeWorksImporter(session).import_audit_needs(csv_path, clear_existing=False)

        demands = session.exec(select(StudentDemand)).all()
        by_course = {d.course_code: d for d in demands}
        assert len(demands) == 2, "BIOL101 must not duplicate; CHEM101 must import"
        assert by_course["BIOL101"].reason == "old-real", "existing row kept as-is"
        assert by_course["CHEM101"].reason.startswith("[DegreeWorks]")
