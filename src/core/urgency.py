"""Shared urgency-scoring core.

Both demand sources — the internal DemandCalculator and the DegreeWorks
importer — must rank courses on the same scale, or solver behavior changes
with the demand source. This module owns the common formula; callers apply
their source-specific extras (attribute/guide boosts, block labels) on top.
"""

from src.database.models import ClassStanding

# Base urgency scores by class standing
STANDING_BASE_SCORES = {
    ClassStanding.SR: 100.0,
    ClassStanding.JR: 50.0,
    ClassStanding.SO: 25.0,
    ClassStanding.FR: 10.0,
}

BOTTLENECK_MULTIPLIER = 2.0
SENIOR_MULTIPLIER = 1.5
REQUIRED_MULTIPLIER = 1.5


def score_base_urgency(
    class_standing: ClassStanding,
    is_required: bool,
    downstream_count: int,
    bottleneck_threshold: int = 3,
) -> tuple[float, list[str], bool]:
    """Compute the shared part of a demand's urgency.

    Returns:
        (urgency, reasons, is_graduation_critical) — urgency after the
        base/bottleneck/senior/required multipliers; reasons collected so
        far; and the graduation-critical rule (senior + required).
    """
    urgency = STANDING_BASE_SCORES.get(class_standing, 10.0)
    reasons: list[str] = []

    if downstream_count >= bottleneck_threshold:
        urgency *= BOTTLENECK_MULTIPLIER
        reasons.append(f"Unlocks {downstream_count} courses")

    if class_standing == ClassStanding.SR:
        urgency *= SENIOR_MULTIPLIER

    if is_required:
        urgency *= REQUIRED_MULTIPLIER

    is_graduation_critical = class_standing == ClassStanding.SR and is_required
    return urgency, reasons, is_graduation_critical
