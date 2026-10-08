"""Unit tests for the retrospective-sim CSV helpers.

The actual-schedule CSV is the data-request schedule_<term>.csv layout
(exports/Gradimize_Data_Request.md §6) — these tests pin the column names
that layout promises (ACTUAL_ENROLLMENT, SUBJECT + COURSE_NUMBER, building
CODE) so the real Fall 2026 delivery imports without silent zeros.
"""

from scripts.retrospective_sim import compute_actual_metrics, match_room
from src.database.models import Room, RoomType, TimeSlot


def _slot() -> TimeSlot:
    return TimeSlot(
        id=1, name="MWF_0900", pattern="MWF", days=["M", "W", "F"],
        start_time="09:00", end_time="09:50", duration_minutes=50,
        slot_type="MWF_50",
    )


def _room() -> Room:
    return Room(
        id="SCI-104", building="Science Building", number="104",
        capacity=30, room_type=RoomType.LECTURE,
    )


def test_actual_enrollment_column_counted():
    """The data-request layout's ACTUAL_ENROLLMENT column must be read —
    not silently defaulted to 0."""
    rows = [{
        "CRN": "20451", "SUBJECT": "BIOL", "COURSE_NUMBER": "201",
        "MEETING_DAYS": "MWF", "START_TIME": "09:00", "END_TIME": "09:50",
        "BUILDING": "SCI", "ROOM": "104", "ACTUAL_ENROLLMENT": "28",
    }]
    metrics = compute_actual_metrics(rows, [], [_slot()], [_room()], {})
    assert metrics["total_enrollment"] == 28
    assert metrics["sections"] == 1
    # Room linked via the building-code -> room-id fallback too.
    assert metrics["room_slots_used"] == 1


def test_legacy_enrollment_column_still_accepted():
    rows = [{"CRN": "1", "COURSE_CODE": "BIOL201", "ENROLLMENT": "17"}]
    metrics = compute_actual_metrics(rows, [], [_slot()], [_room()], {})
    assert metrics["total_enrollment"] == 17


def test_match_room_falls_back_to_room_id():
    """Building CODE + number matches the SCI-104 room-id convention even
    when Room.building holds the long description."""
    assert match_room({"BUILDING": "SCI", "ROOM": "104"}, [_room()]) == "SCI-104"
    assert match_room({"BUILDING": "Science Building", "ROOM": "104"},
                      [_room()]) == "SCI-104"
    assert match_room({"BUILDING": "SCI", "ROOM": "999"}, [_room()]) is None
