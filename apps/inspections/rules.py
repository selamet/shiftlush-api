"""What each inspection label means, as data.

M5: docs/superpowers/specs/2026-10-20-m5-periodic-inspection-design.md, "The
rules, from the regulation". Source: Asansor Periyodik Kontrol Yonetmeligi,
Resmi Gazete 04.05.2018 No. 30411, as amended by Resmi Gazete 13.09.2022
No. 31952 — read from the consolidated text on 2026-10-20.

Encoded here, once, so that an amendment is an edit to this table and its
citation rather than a hunt through the code. The services derive every
deadline from it and the tests read the same table, so a changed row changes
both the behaviour and what is asserted about it.

Two rules hold across the table:

- **No extensions.** Neither the inspection body nor the municipality may
  extend a deadline; only the Ministry may, in force majeure (m.9/18). So a
  follow-up inspection never restarts a clock: its deadline counts from the
  original periodic inspection it follows.
- **Red, then yellow.** A red lift that a follow-up turns yellow must reach
  blue or green within 120 days of the original inspection date — which is the
  yellow row counted from the original, exactly as the first rule says.

The inspection itself is due at least once a year (m.8/1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from apps.inspections.models import Label


class Deadline(StrEnum):
    #: No correction is required.
    NONE = "none"
    #: A fixed number of days.
    DAYS = "days"
    #: By the next periodic inspection.
    NEXT_INSPECTION = "next_inspection"


@dataclass(frozen=True)
class LabelRule:
    label: Label
    #: Whether the lift may be used at all while it carries this label.
    usable: bool
    deadline: Deadline
    #: For Deadline.DAYS: how many, counted from the inspection that set the
    #: label — for a follow-up, from the original periodic inspection.
    days: int | None
    #: Whether missing the deadline gets the lift sealed by the municipality.
    sealed_if_missed: bool
    citation: str


_SOURCE = "Asansor Periyodik Kontrol Yonetmeligi (RG 30411, am. RG 31952)"

RULES: dict[Label, LabelRule] = {
    Label.GREEN: LabelRule(
        label=Label.GREEN,
        usable=True,
        deadline=Deadline.NONE,
        days=None,
        sealed_if_missed=False,
        citation=f"{_SOURCE}: green label (kusursuz), no defects, no correction.",
    ),
    Label.BLUE: LabelRule(
        label=Label.BLUE,
        usable=True,
        deadline=Deadline.NEXT_INSPECTION,
        days=None,
        sealed_if_missed=False,
        citation=(
            f"{_SOURCE}: blue label (hafif kusurlu), corrected by the next periodic "
            "inspection; the first follow-up is free if applied for within 60 days."
        ),
    ),
    Label.YELLOW: LabelRule(
        label=Label.YELLOW,
        usable=True,
        deadline=Deadline.DAYS,
        days=120,
        sealed_if_missed=True,
        citation=(
            f"{_SOURCE}: yellow label (kusurlu), corrected within 120 days, then a "
            "follow-up within 3 working days; sealed if missed. A red lift turned "
            "yellow by a follow-up counts the 120 days from the original inspection."
        ),
    ),
    Label.RED: LabelRule(
        label=Label.RED,
        usable=False,
        deadline=Deadline.DAYS,
        days=60,
        sealed_if_missed=True,
        citation=(
            f"{_SOURCE}: red label (guvensiz), the lift must not be used; corrected "
            "within 60 days, then a follow-up within 3 working days; sealed if missed."
        ),
    ),
}

#: How long a periodic inspection holds before the next is due (m.8/1).
INSPECTION_INTERVAL_YEARS = 1


def default_next_inspection(inspected_on: date) -> date:
    """`inspected_on` plus a year, 29 February landing on 28 February."""
    try:
        return inspected_on.replace(year=inspected_on.year + INSPECTION_INTERVAL_YEARS)
    except ValueError:
        return inspected_on.replace(year=inspected_on.year + INSPECTION_INTERVAL_YEARS, day=28)


def correction_deadline(
    *, label: str, counted_from: date, next_inspection_due: date | None
) -> date | None:
    """The day the label's defects must be corrected by, or None if there is none.

    `counted_from` is the original periodic inspection's date — the inspection's
    own for a periodic one, the one it follows for a follow-up.
    """
    rule = RULES[Label(label)]
    if rule.deadline == Deadline.DAYS:
        assert rule.days is not None
        return counted_from + timedelta(days=rule.days)
    if rule.deadline == Deadline.NEXT_INSPECTION:
        return next_inspection_due
    return None
