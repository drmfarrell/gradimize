"""Tests for src/core/offerings.py: inferring Course.offer_terms/cadence
from historical schedules. Zero coverage before this file."""

from datetime import datetime

from src.core.offerings import infer_course_offerings
from src.database.models import Course, ScheduleVersion, Section, StructureType


def _course(code, offer_terms=None, cadence=None):
    kwargs = dict(code=code, title="T", credits=3, structure_type=StructureType.STANDALONE)
    if offer_terms is not None:
        kwargs["offer_terms"] = offer_terms
    if cadence is not None:
        kwargs["cadence"] = cadence
    return Course(**kwargs)


def _schedule(id, semester, name="Sched"):
    return ScheduleVersion(id=id, name=name, semester=semester, status="draft",
                           created_at=datetime.fromisoformat("2026-01-01T00:00:00"))


def _section(id, schedule_id, course_code):
    return Section(id=id, schedule_id=schedule_id, crn=str(10000 + id),
                    course_code=course_code, section_number="001", capacity=30)


def test_no_schedule_versions_returns_empty_report(db_manager):
    with db_manager.get_session() as session:
        session.add(_course("BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.schedules_examined == 0
        assert report.courses_examined == 0
        assert report.courses_with_history == 0
        assert report.courses_changed == 0
        assert report.results == []


def test_all_unparseable_semesters_short_circuits(db_manager):
    """When every schedule's semester fails to parse, the function returns
    early (before even loading courses) — schedules are recorded in
    skipped_unparseable_semesters but courses_examined stays 0."""
    with db_manager.get_session() as session:
        session.add(_schedule(1, "not-a-semester"))
        session.add(_course("BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.schedules_examined == 1
        assert report.skipped_unparseable_semesters == ["not-a-semester"]
        assert report.courses_examined == 0
        assert report.results == []


def test_unparseable_semester_among_valid_ones_is_recorded_but_others_processed(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2025FA"))
        session.add(_schedule(2, "garbage"))
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.skipped_unparseable_semesters == ["garbage"]
        assert report.courses_with_history == 1


def test_course_with_no_section_history_is_left_out_of_results(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2025FA"))
        session.add(_course("BIOL101"))   # has a section
        session.add(_course("CHEM101"))   # never offered
        session.add(_section(1, 1, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.courses_examined == 2
        assert report.courses_with_history == 1
        codes_in_results = {r.course_code for r in report.results}
        assert codes_in_results == {"BIOL101"}


def test_infers_offer_terms_from_single_semester(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2025FA"))
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        result = report.results[0]
        assert result.new_offer_terms == ["FALL"]
        assert result.new_cadence == "ANNUAL"  # single year: can't claim biennial
        assert result.changed is True
        assert "FALL" in result.note

    with db_manager.get_session() as session:
        course = session.get(Course, "BIOL101")
        assert course.offer_terms == ["FALL"]
        assert course.cadence == "ANNUAL"


def test_infers_multiple_terms_sorted(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2025FA"))
        session.add(_schedule(2, "2025SP"))
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.add(_section(2, 2, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.results[0].new_offer_terms == ["FALL", "SPRING"]


def test_infers_biennial_even_cadence(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2024FA"))
        session.add(_schedule(2, "2026FA"))
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.add(_section(2, 2, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.results[0].new_cadence == "BIENNIAL_EVEN"


def test_infers_biennial_odd_cadence(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2023FA"))
        session.add(_schedule(2, "2025FA"))
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.add(_section(2, 2, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.results[0].new_cadence == "BIENNIAL_ODD"


def test_mixed_parity_years_defaults_to_annual(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2023FA"))  # odd
        session.add(_schedule(2, "2024FA"))  # even
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.add(_section(2, 2, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.results[0].new_cadence == "ANNUAL"


def test_single_year_stays_annual_regardless_of_parity(db_manager):
    """One year of history can't distinguish biennial from annual, even if
    that lone year happens to be even."""
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2024FA"))
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        assert report.results[0].new_cadence == "ANNUAL"


def test_biennial_min_years_param_is_honored(db_manager):
    """Two even years normally trigger BIENNIAL_EVEN, but raising the
    threshold to 3 should keep it at ANNUAL."""
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2024FA"))
        session.add(_schedule(2, "2026FA"))
        session.add(_course("BIOL101"))
        session.add(_section(1, 1, "BIOL101"))
        session.add(_section(2, 2, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session, biennial_min_years=3)

        assert report.results[0].new_cadence == "ANNUAL"


def test_apply_false_computes_but_does_not_persist(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2025FA"))
        session.add(_course("BIOL101"))  # offer_terms=[], cadence=ANNUAL by default
        session.add(_section(1, 1, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session, apply=False)

        assert report.results[0].changed is True
        assert report.courses_changed == 1

    with db_manager.get_session() as session:
        course = session.get(Course, "BIOL101")
        assert course.offer_terms == [], "dry run must not write to the DB"
        assert course.cadence == "ANNUAL"


def test_no_change_when_course_already_matches_inference(db_manager):
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2025FA"))
        session.add(_course("BIOL101", offer_terms=["FALL"], cadence="ANNUAL"))
        session.add(_section(1, 1, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)

        result = report.results[0]
        assert result.changed is False
        assert result.note is None
        assert report.courses_changed == 0


def test_apply_true_overwrites_prior_hand_entered_values(db_manager):
    """Re-running inference overwrites a previously hand-set value — the
    module docstring calls this out explicitly as intentional."""
    with db_manager.get_session() as session:
        session.add(_schedule(1, "2025SP"))
        session.add(_course("BIOL101", offer_terms=["FALL"], cadence="BIENNIAL_ODD"))
        session.add(_section(1, 1, "BIOL101"))
        session.commit()

        report = infer_course_offerings(session)
        assert report.results[0].changed is True

    with db_manager.get_session() as session:
        course = session.get(Course, "BIOL101")
        assert course.offer_terms == ["SPRING"]
        assert course.cadence == "ANNUAL"
