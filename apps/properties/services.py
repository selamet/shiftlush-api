"""Rules about where a building is, shared by everything that reads a position."""

from __future__ import annotations

from apps.properties.models import Building


def position_of(building: Building) -> tuple[str | None, str | None]:
    """A building's position as (latitude, longitude) strings, or (None, None).

    Its own when both coordinates are set; otherwise its estate's when that
    one is whole. A half-entered position is no position: a route cannot
    drive to a latitude alone. The `has_location` filter on the building list
    applies the same rule in SQL, so the two never disagree about what is
    missing. Reads `building.complex`, so callers select it with the building.
    """
    for place in (building, building.complex):
        if place is not None and place.latitude is not None and place.longitude is not None:
            return str(place.latitude), str(place.longitude)
    return None, None
