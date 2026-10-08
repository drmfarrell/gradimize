"""Predicted student demand — the default demand source.

Answers "who needs which course next term, and how badly?" from degree
requirements, prerequisite readiness, and each student's semester guide,
scoring every (student, course) pair on the shared urgency scale in
``urgency.py``. This is one of three demand sources feeding the solver
(the other two, ``degreeworks`` and ``registrations``, observe rather than
predict); all three dispatch through ``generate.bootstrap_demands`` and
write the same ``StudentDemand`` rows.

Holds two calculators: ``DemandCalculator`` for returning students (real
records) and ``ProjectionDemandCalculator`` for a projected incoming class.
"""

import logging
from typing import Set

from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from src.core.graph import PrerequisiteGraph
from src.core.urgency import score_base_urgency
from src.database.models import (
    AttributeRequirement,
    ClassStanding,
    Course,
    DegreeRequirement,
    FreshmenProjection,
    RequirementGroup,
    RequirementGroupCourse,
    RequirementGroupType,
    SemesterGuideEntry,
    Student,
    StudentDemand,
)
from src.database.queries import get_completed_courses, get_guide_entries, get_guide_for_student

logger = logging.getLogger(__name__)


def estimate_student_semester(credits_earned: int) -> int:
    """Map credits earned to estimated semester number (1-8).

    Uses ~15 credits/semester assumption.

    Args:
        credits_earned: Total credits the student has earned.

    Returns:
        Semester number 1-8 (1=FR fall, 8=SR spring).
    """
    return min(8, (credits_earned // 15) + 1)


class DemandCalculator:
    """Calculates student course demand and urgency scores."""

    def __init__(
        self,
        session: Session,
        prereq_graph: PrerequisiteGraph,
        bottleneck_threshold: int = 3,
    ):
        """Initialize demand calculator.

        Args:
            session: Database session.
            prereq_graph: Prerequisite graph instance.
            bottleneck_threshold: Min dependents for bottleneck multiplier.
        """
        self.session = session
        self.graph = prereq_graph
        self.bottleneck_threshold = bottleneck_threshold

        # Cache for performance
        self._requirements_cache: dict[tuple[str, str | None, int], Set[str]] = {}
        self._bottleneck_cache: dict[str, int] = {}
        self._course_attributes_cache: dict[str, list[str]] = {}
        self._group_requirements_cache: dict[tuple[str, str | None, int], list[dict]] = {}
        self._req_categories_cache: dict[tuple[str, str | None, int], dict[str, set[str]]] = {}
        self._guide_entries_cache: dict[tuple[str, str | None, int], list[SemesterGuideEntry]] = {}

    def _get_req_course_categories(
        self, major: str, concentration: str | None, catalog_year: int
    ) -> dict[str, set[str]]:
        """Course -> requirement categories, cached per (major, conc, year)."""
        cache_key = (major, concentration, catalog_year)
        cached = self._req_categories_cache.get(cache_key)
        if cached is not None:
            return cached

        req_course_categories: dict[str, set[str]] = {}
        all_reqs = self.session.exec(
            select(DegreeRequirement).where(
                DegreeRequirement.major == major,
                DegreeRequirement.catalog_year == catalog_year,
            )
        ).all()
        for req in all_reqs:
            if req.concentration is None or req.concentration == concentration:
                req_course_categories.setdefault(req.course_code, set()).add(req.category)

        self._req_categories_cache[cache_key] = req_course_categories
        return req_course_categories

    def _get_guide_entries_cached(
        self, major: str, concentration: str | None, catalog_year: int
    ) -> list[SemesterGuideEntry]:
        """Semester-guide entries, cached per (major, conc, year)."""
        cache_key = (major, concentration, catalog_year)
        cached = self._guide_entries_cache.get(cache_key)
        if cached is not None:
            return cached

        guide = get_guide_for_student(self.session, major, catalog_year, concentration)
        entries: list[SemesterGuideEntry] = []
        if guide:
            entries = get_guide_entries(self.session, guide.id)

        self._guide_entries_cache[cache_key] = entries
        return entries

    def _get_major_requirements(
        self, major: str, concentration: str | None, catalog_year: int
    ) -> Set[str]:
        """Get required courses for a major/concentration/catalog year.

        Args:
            major: Major code.
            concentration: Concentration within major (None for no concentration).
            catalog_year: Student's catalog year.

        Returns:
            Set of required course codes.
        """
        cache_key = (major, concentration, catalog_year)
        if cache_key in self._requirements_cache:
            return self._requirements_cache[cache_key]

        # Get requirements that apply to all concentrations (concentration is NULL)
        base_reqs = self.session.exec(
            select(DegreeRequirement).where(
                DegreeRequirement.major == major,
                DegreeRequirement.catalog_year == catalog_year,
                DegreeRequirement.concentration == None,  # noqa: E711
                DegreeRequirement.is_required == True,  # noqa: E712
            )
        ).all()

        required_courses = {r.course_code for r in base_reqs}

        # If student has a concentration, also get concentration-specific requirements
        if concentration:
            conc_reqs = self.session.exec(
                select(DegreeRequirement).where(
                    DegreeRequirement.major == major,
                    DegreeRequirement.catalog_year == catalog_year,
                    DegreeRequirement.concentration == concentration,
                    DegreeRequirement.is_required == True,  # noqa: E712
                )
            ).all()
            required_courses.update(r.course_code for r in conc_reqs)

        self._requirements_cache[cache_key] = required_courses
        return required_courses

    def _get_bottleneck_count(self, course_code: str) -> int:
        """Get number of courses that depend on this course.

        Args:
            course_code: Course to check.

        Returns:
            Number of downstream courses.
        """
        if course_code in self._bottleneck_cache:
            return self._bottleneck_cache[course_code]

        count = len(self.graph.get_all_dependents(course_code))
        self._bottleneck_cache[course_code] = count
        return count

    def _get_course_attributes(self, course_code: str) -> list[str]:
        """Get attributes (W, A, I, G) for a course.

        Args:
            course_code: Course to check.

        Returns:
            List of attribute codes.
        """
        if course_code in self._course_attributes_cache:
            return self._course_attributes_cache[course_code]

        course = self.session.get(Course, course_code)
        attrs = course.attributes if course else []
        self._course_attributes_cache[course_code] = attrs
        return attrs

    def _get_requirement_groups(
        self, major: str, concentration: str | None, catalog_year: int
    ) -> list[dict]:
        """Get requirement groups for a major/concentration.

        Args:
            major: Major code.
            concentration: Concentration (or None).
            catalog_year: Catalog year.

        Returns:
            List of dicts with group info and course options.
        """
        cache_key = (major, concentration, catalog_year)
        if cache_key in self._group_requirements_cache:
            return self._group_requirements_cache[cache_key]

        groups = []

        # Get groups that apply to all concentrations
        base_groups = self.session.exec(
            select(RequirementGroup).where(
                RequirementGroup.major == major,
                RequirementGroup.catalog_year == catalog_year,
                RequirementGroup.concentration == None,  # noqa: E711
            )
        ).all()

        # Get concentration-specific groups
        conc_groups = []
        if concentration:
            conc_groups = self.session.exec(
                select(RequirementGroup).where(
                    RequirementGroup.major == major,
                    RequirementGroup.catalog_year == catalog_year,
                    RequirementGroup.concentration == concentration,
                )
            ).all()

        for group in list(base_groups) + list(conc_groups):
            # Get courses in this group
            group_courses = self.session.exec(
                select(RequirementGroupCourse).where(
                    RequirementGroupCourse.group_id == group.id
                )
            ).all()

            course_options = []
            for gc in group_courses:
                option = {"course_code": gc.course_code}
                if gc.paired_with_code:
                    option["paired_with"] = gc.paired_with_code
                course_options.append(option)

            groups.append({
                "id": group.id,
                "name": group.group_name,
                "type": group.group_type,
                "category": group.category,
                "required_count": group.required_count,
                "min_credits": group.min_credits,
                "courses": course_options,
            })

        self._group_requirements_cache[cache_key] = groups
        return groups

    def _get_attribute_requirements(
        self, major: str, concentration: str | None, catalog_year: int
    ) -> list[dict]:
        """Get attribute requirements (W, A, I, G) for a major.

        Args:
            major: Major code.
            concentration: Concentration (or None).
            catalog_year: Catalog year.

        Returns:
            List of dicts with attribute and required_count.
        """
        # Get requirements for this major or universal (*)
        reqs = self.session.exec(
            select(AttributeRequirement).where(
                AttributeRequirement.catalog_year == catalog_year,
                (AttributeRequirement.major == major) | (AttributeRequirement.major == "*"),
            )
        ).all()

        # Filter by concentration
        result = []
        for req in reqs:
            if req.concentration is None or req.concentration == concentration:
                result.append({
                    "attribute": req.attribute,
                    "attribute_name": req.attribute_name,
                    "required_count": req.required_count,
                })

        return result

    def _check_group_satisfaction(
        self, group: dict, completed: Set[str]
    ) -> tuple[bool, Set[str]]:
        """Check if a requirement group is satisfied and get needed courses.

        Args:
            group: Group info dict from _get_requirement_groups.
            completed: Set of completed course codes.

        Returns:
            Tuple of (is_satisfied, set of needed course codes).
        """
        group_type = group["type"]
        courses = group["courses"]
        required_count = group["required_count"]
        min_credits = group["min_credits"]

        # Get completed courses from this group
        completed_in_group = []
        for opt in courses:
            code = opt["course_code"]
            paired = opt.get("paired_with")

            if paired:
                # Both must be completed for paired requirement
                if code in completed and paired in completed:
                    completed_in_group.append(code)
            else:
                if code in completed:
                    completed_in_group.append(code)

        # Get uncompleted courses from this group
        needed_courses = set()
        for opt in courses:
            code = opt["course_code"]
            paired = opt.get("paired_with")
            if code not in completed:
                needed_courses.add(code)
            if paired and paired not in completed:
                needed_courses.add(paired)

        # Check satisfaction based on group type
        if group_type == RequirementGroupType.OR:
            # Need at least one
            is_satisfied = len(completed_in_group) >= 1
        elif group_type == RequirementGroupType.AND:
            # Need all
            is_satisfied = len(needed_courses) == 0
        elif group_type == RequirementGroupType.PICK_N:
            # Need N courses
            is_satisfied = len(completed_in_group) >= required_count
        elif group_type == RequirementGroupType.MIN_CREDITS:
            # Need minimum credits - calculate completed credits
            completed_credits = 0
            for code in completed_in_group:
                course = self.session.get(Course, code)
                if course:
                    completed_credits += course.credits
            is_satisfied = completed_credits >= (min_credits or 0)
        else:
            is_satisfied = False

        return is_satisfied, needed_courses

    def _check_attribute_satisfaction(
        self, attr_req: dict, completed: Set[str]
    ) -> tuple[bool, int]:
        """Check if an attribute requirement is satisfied.

        Args:
            attr_req: Attribute requirement dict.
            completed: Set of completed course codes.

        Returns:
            Tuple of (is_satisfied, count still needed).
        """
        attribute = attr_req["attribute"]
        required = attr_req["required_count"]

        # Count completed courses with this attribute
        count = 0
        for code in completed:
            attrs = self._get_course_attributes(code)
            if attribute in attrs:
                count += 1

        is_satisfied = count >= required
        still_needed = max(0, required - count)

        return is_satisfied, still_needed

    def calculate_student_demand(self, student: Student) -> list[StudentDemand]:
        """Calculate which courses a student needs and their urgency.

        Considers:
        - Simple DegreeRequirement records
        - RequirementGroup (OR, PICK_N, MIN_CREDITS)
        - AttributeRequirement (W, A, I, G intensive courses)

        Urgency formula:
            base_score = STANDING_BASE_SCORES[standing]

            for each needed_course:
                urgency = base_score

                # Bottleneck multiplier: courses that unlock many others
                if downstream_count > threshold:
                    urgency *= 2.0

                # Graduation proximity (seniors need courses more urgently)
                if standing == SR:
                    urgency *= 1.5

                # Required vs elective
                if course in major_requirements:
                    urgency *= 1.5

        Args:
            student: Student to calculate demand for.

        Returns:
            List of StudentDemand objects (not yet persisted).
        """
        demands = []
        demand_map: dict[str, StudentDemand] = {}  # Track demands by course code

        # Get courses already completed
        completed = get_completed_courses(self.session, student.id)

        # Get simple required courses for this major/concentration
        required_courses = self._get_major_requirements(
            student.major, student.concentration, student.catalog_year
        )

        # Get requirement groups
        req_groups = self._get_requirement_groups(
            student.major, student.concentration, student.catalog_year
        )

        # Get attribute requirements
        attr_reqs = self._get_attribute_requirements(
            student.major, student.concentration, student.catalog_year
        )

        # Track courses needed from unsatisfied groups (and their categories)
        group_needed_courses: dict[str, list[str]] = {}  # course_code -> [group_names]
        group_course_categories: dict[str, set[str]] = {}  # course_code -> {categories}
        unsatisfied_group_courses: Set[str] = set()

        for group in req_groups:
            is_satisfied, needed = self._check_group_satisfaction(group, completed)
            if not is_satisfied:
                for code in needed:
                    unsatisfied_group_courses.add(code)
                    if code not in group_needed_courses:
                        group_needed_courses[code] = []
                    group_needed_courses[code].append(group["name"])
                    if code not in group_course_categories:
                        group_course_categories[code] = set()
                    group_course_categories[code].add(group["category"])

        # Track attributes still needed
        attrs_still_needed: dict[str, int] = {}  # attribute -> count needed
        for attr_req in attr_reqs:
            is_satisfied, still_needed = self._check_attribute_satisfaction(attr_req, completed)
            if not is_satisfied:
                attrs_still_needed[attr_req["attribute"]] = still_needed

        # Get all courses available to take (prerequisites satisfied)
        available = self.graph.get_available_courses(completed)

        # Also consider corequisites of available courses
        to_consider = set(available)
        for course in available:
            coreqs = self.graph.get_corequisites(course)
            to_consider.update(coreqs - completed)

        # Course -> categories map and guide entries only depend on
        # (major, concentration, catalog_year) — cached like the other
        # requirement lookups, instead of re-querying per student.
        req_course_categories = self._get_req_course_categories(
            student.major, student.concentration, student.catalog_year
        )
        guide_entries = self._get_guide_entries_cached(
            student.major, student.concentration, student.catalog_year
        )

        for course_code in to_consider:
            # Check if it's a simple required course
            is_simple_required = course_code in required_courses

            # Check if it's needed for an unsatisfied group
            is_group_needed = course_code in unsatisfied_group_courses
            group_names = group_needed_courses.get(course_code, [])

            # Check if it has needed attributes
            course_attrs = self._get_course_attributes(course_code)
            needed_attrs = [a for a in course_attrs if a in attrs_still_needed]

            # Determine if required (simple req OR group req)
            is_required = is_simple_required or is_group_needed

            # Shared formula: base by standing, bottleneck, senior, required
            urgency, reasons, is_graduation_critical = score_base_urgency(
                student.class_standing,
                is_required,
                self._get_bottleneck_count(course_code),
                self.bottleneck_threshold,
            )

            # Attribute bonus - courses that fulfill attribute requirements
            if needed_attrs:
                urgency *= 1.2  # Slight boost for attribute-satisfying courses
                attr_names = ", ".join(needed_attrs)
                reasons.append(f"Fulfills {attr_names} requirement")

            # Semester guide urgency boosting
            if guide_entries:
                current_sem = estimate_student_semester(student.credits_earned)

                # Collect all categories this course belongs to
                course_categories = set()
                course_categories.update(req_course_categories.get(course_code, set()))
                course_categories.update(group_course_categories.get(course_code, set()))

                # A course can match several guide entries (especially
                # category-only slots that recur every semester). Apply only
                # the single BEST boost, not one per match — compounding
                # inflated generic-category courses without bound.
                best_boost = 1.0
                best_reason: str | None = None
                for entry in guide_entries:
                    if entry.semester_number > current_sem + 1:
                        continue

                    boost, reason = 1.0, None
                    if entry.semester_number <= current_sem:
                        # Current or behind-schedule entries
                        if entry.course_code and entry.course_code == course_code:
                            boost = 1.3 if entry.semester_number == current_sem else 1.4
                            reason = f"Recommended semester {entry.semester_number}"
                        elif (
                            entry.category
                            and not entry.course_code
                            and entry.category in course_categories
                        ):
                            boost = 1.15 if entry.semester_number == current_sem else 1.2
                            reason = (
                                f"Fills {entry.label} slot "
                                f"(semester {entry.semester_number})"
                            )
                    elif entry.semester_number == current_sem + 1:
                        # Next semester look-ahead
                        if entry.course_code and entry.course_code == course_code:
                            boost = 1.1
                            reason = f"Upcoming (semester {entry.semester_number})"

                    if boost > best_boost:
                        best_boost, best_reason = boost, reason

                if best_boost > 1.0:
                    urgency *= best_boost
                    if best_reason:
                        reasons.append(best_reason)

            # Build reason string (grad-critical rule computed by
            # score_base_urgency: senior + required)
            if is_graduation_critical:
                reasons.insert(0, "GRADUATION-CRITICAL")

            if is_simple_required:
                if student.concentration:
                    reasons.append(f"Required for {student.major}/{student.concentration}")
                else:
                    reasons.append("Major requirement")

            if is_group_needed:
                for gn in group_names:
                    reasons.append(f"Option for '{gn}'")

            if student.class_standing == ClassStanding.SR and not is_graduation_critical:
                reasons.append("Senior priority")

            if not reasons:
                reasons.append("Available elective")

            demand = StudentDemand(
                student_id=student.id,
                course_code=course_code,
                urgency_score=urgency,
                is_graduation_critical=is_graduation_critical,
                is_required=is_required,
                reason="; ".join(reasons),
            )
            demands.append(demand)
            demand_map[course_code] = demand

        return demands

    def calculate_all_demands(self, clear_existing: bool = True) -> int:
        """Calculate demands for all real (non-projected) students.

        Skips projected students and preserves projected demand records.

        Args:
            clear_existing: If True, clear existing non-projected demands first.

        Returns:
            Total number of demand records created.
        """
        if clear_existing:
            # Bulk-delete only non-projected demands (row-by-row ORM deletes
            # load and delete ~150k+ objects individually at real scale)
            self.session.execute(
                sa_delete(StudentDemand).where(
                    StudentDemand.is_projected == False  # noqa: E712
                )
            )
            self.session.commit()

        # Get only non-projected students
        students = self.session.exec(
            select(Student).where(Student.is_projected == False)  # noqa: E712
        ).all()
        total_demands = 0

        for i, student in enumerate(students):
            demands = self.calculate_student_demand(student)
            for demand in demands:
                self.session.add(demand)
            total_demands += len(demands)

            # Commit in batches for performance
            if (i + 1) % 100 == 0:
                self.session.commit()
                logger.info(f"Processed {i + 1}/{len(students)} students")

        self.session.commit()
        logger.info(f"Created {total_demands} demand records for {len(students)} students")
        return total_demands

    def get_demand_summary(self) -> dict:
        """Get summary statistics of calculated demands.

        Returns:
            Dictionary with demand statistics.
        """
        demands = self.session.exec(select(StudentDemand)).all()

        if not demands:
            return {"total_demands": 0}

        # Group by course
        by_course: dict[str, list[float]] = {}
        for d in demands:
            if d.course_code not in by_course:
                by_course[d.course_code] = []
            by_course[d.course_code].append(d.urgency_score)

        # Calculate stats per course
        course_stats = {}
        for code, scores in by_course.items():
            course_stats[code] = {
                "student_count": len(scores),
                "total_urgency": sum(scores),
                "avg_urgency": sum(scores) / len(scores),
                "max_urgency": max(scores),
            }

        # Sort by total urgency
        sorted_courses = sorted(
            course_stats.items(), key=lambda x: x[1]["total_urgency"], reverse=True
        )

        return {
            "total_demands": len(demands),
            "unique_students": len(set(d.student_id for d in demands)),
            "unique_courses": len(by_course),
            "top_demand_courses": sorted_courses[:10],
            "total_urgency": sum(d.urgency_score for d in demands),
        }


class ProjectionDemandCalculator:
    """Generates synthetic student demand from freshmen enrollment projections.

    For each active FreshmenProjection, creates synthetic Student records
    (PROJ_ prefix) and replicates demand computed from a single representative.
    """

    def __init__(self, session: Session, prereq_graph: PrerequisiteGraph):
        self.session = session
        self.graph = prereq_graph

    def clear_projected_data(self) -> dict[str, int]:
        """Delete all projected StudentDemand and Student records.

        Returns:
            Counts of deleted demands and students.
        """
        # Delete projected demands first (foreign key on student) — bulk
        # deletes; the rowcounts feed the returned stats.
        n_demands = self.session.execute(
            sa_delete(StudentDemand).where(
                StudentDemand.is_projected == True  # noqa: E712
            )
        ).rowcount

        n_students = self.session.execute(
            sa_delete(Student).where(Student.is_projected == True)  # noqa: E712
        ).rowcount

        self.session.commit()
        return {
            "demands_deleted": n_demands,
            "students_deleted": n_students,
        }

    def generate_projected_demand(self) -> dict[str, int]:
        """Generate synthetic students and demand from active projections.

        Clears existing projected data first (idempotent).

        Returns:
            Counts of created students and demands.
        """
        # Clear any existing projected data
        self.clear_projected_data()

        projections = self.session.exec(
            select(FreshmenProjection).where(
                FreshmenProjection.is_active == True  # noqa: E712
            )
        ).all()

        if not projections:
            logger.info("No active freshmen projections found")
            return {"students_created": 0, "demands_created": 0}

        demand_calc = DemandCalculator(self.session, self.graph)
        total_students = 0
        total_demands = 0
        total_netted_real_admits = 0

        for projection in projections:
            major = projection.major
            catalog_year = projection.catalog_year

            # Net out real admits already imported for this major/catalog
            # year so we don't double-count them alongside the synthetic
            # projection cohort. admit_term may be "202610" or "2026FA" —
            # match by substring containment of the catalog year.
            candidate_admits = self.session.exec(
                select(Student).where(
                    Student.is_projected == False,  # noqa: E712
                    Student.major == major,
                    Student.class_standing == ClassStanding.FR,
                    Student.admit_term.is_not(None),
                )
            ).all()
            real_admits = sum(
                1
                for s in candidate_admits
                if s.admit_term and str(catalog_year) in s.admit_term
            )

            count = max(0, projection.estimated_count - real_admits)
            if real_admits > 0:
                logger.info(
                    f"{major} {catalog_year}: netting {real_admits} real admits "
                    f"against projection of {projection.estimated_count} "
                    f"({count} synthetics remain)"
                )
                total_netted_real_admits += real_admits

            # Create a temporary representative student
            rep_id = f"_REP_{major}"
            rep_student = Student(
                id=rep_id,
                name=f"Representative {major} Freshman",
                email="projected@gradimize.local",
                class_standing=ClassStanding.FR,
                credits_earned=0,
                major=major,
                catalog_year=catalog_year,
                is_projected=True,
            )
            self.session.add(rep_student)
            self.session.commit()

            # Calculate demand for the representative
            rep_demands = demand_calc.calculate_student_demand(rep_student)

            # Delete the representative
            self.session.delete(rep_student)
            self.session.commit()

            if not rep_demands:
                logger.info(f"No demand computed for {major} freshmen, skipping")
                continue

            # Create N synthetic students and replicate demands
            for i in range(1, count + 1):
                syn_id = f"PROJ_{major}_{i:04d}"
                syn_student = Student(
                    id=syn_id,
                    name=f"Projected {major} #{i}",
                    email="projected@gradimize.local",
                    class_standing=ClassStanding.FR,
                    credits_earned=0,
                    major=major,
                    catalog_year=catalog_year,
                    is_projected=True,
                )
                self.session.add(syn_student)

                for rep_d in rep_demands:
                    demand = StudentDemand(
                        student_id=syn_id,
                        course_code=rep_d.course_code,
                        urgency_score=rep_d.urgency_score,
                        is_required=rep_d.is_required,
                        is_graduation_critical=False,
                        is_projected=True,
                        reason=rep_d.reason,
                    )
                    self.session.add(demand)
                    total_demands += 1

                total_students += 1

                # Commit in batches
                if total_students % 100 == 0:
                    self.session.commit()
                    logger.info(f"Created {total_students} projected students so far")

        self.session.commit()
        logger.info(
            f"Generated {total_students} projected students with "
            f"{total_demands} demand records"
        )
        return {
            "students_created": total_students,
            "demands_created": total_demands,
            "netted_real_admits": total_netted_real_admits,
        }

