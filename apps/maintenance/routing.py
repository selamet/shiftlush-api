"""A day's stops in the order that drives least, by straight-line distance.

Pure functions over plain values: no database, no external service. The
calendar spec (2026-10-22, part B) asks for a suggestion, not a solver — a
technician's day is rarely more than fifteen stops — so the order is built by
nearest neighbour and then improved by 2-opt until no swap helps, on an open
path (nobody drives back to the start). Two promises the tests hold it to:

- the same stops always give the same order, ties broken by the order given
  (the address order the plan already uses);
- the suggestion is never longer than that address order. If it would be, the
  address order is returned.

Stops with no position cannot be placed; they follow the placed ones, in the
order given, so nobody's visit silently drops out of the day.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

EARTH_RADIUS_KM = 6371.0088
#: A guard, not a tuning knob: 2-opt on a day's stops settles in a few passes.
MAX_IMPROVEMENT_PASSES = 200
#: Without a start point, how many stops are tried as the first. Every one of
#: a normal day; a backlog of a hundred undated stops stays quick.
MAX_FIRST_STOPS = 12

Point = tuple[float, float]


@dataclass(frozen=True)
class Stop:
    id: str
    latitude: float | None
    longitude: float | None

    @property
    def point(self) -> Point | None:
        if self.latitude is None or self.longitude is None:
            return None
        return (float(self.latitude), float(self.longitude))


@dataclass
class Route:
    #: Every stop's id: the placed ones in driving order, then the unplaced.
    order: list[str]
    #: Straight-line kilometres over the placed stops, from the start if given.
    total_km: float
    #: Each stop's distance from the one before it (from the start for the
    #: first, when there is one); None where there is nothing to measure.
    legs: dict[str, float | None] = field(default_factory=dict)
    unplaced: list[str] = field(default_factory=list)


def haversine_km(a: Point, b: Point) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def path_km(points: Sequence[Point], *, start: Point | None) -> float:
    """The length of driving `points` in order, from `start` when there is one."""
    route = [start, *points] if start is not None else list(points)
    return sum(haversine_km(a, b) for a, b in itertools.pairwise(route))


def _nearest_neighbour(points: list[Point], first: int | None, start: Point | None) -> list[int]:
    remaining = list(range(len(points)))
    order: list[int] = []
    here = start
    if first is not None:
        order.append(remaining.pop(first))
        here = points[order[0]]
    while remaining:
        origin = here
        assert origin is not None
        # min() keeps the first of equals, and `remaining` is in the given
        # order, so a tie goes to the stop the address order puts first.
        nearest = min(remaining, key=lambda i: haversine_km(origin, points[i]))
        remaining.remove(nearest)
        order.append(nearest)
        here = points[nearest]
    return order


def _two_opt(order: list[int], points: list[Point], start: Point | None) -> list[int]:
    """Reverse any stretch that shortens the path, until none does.

    The gain of a reversal is worked out from the two edges it replaces, not
    by measuring the whole path again, so a day of thirty stops tried from
    every first stop still answers at once.
    """
    distance = [[haversine_km(a, b) for b in points] for a in points]
    from_start = [haversine_km(start, p) for p in points] if start is not None else None

    def edge(a: int | None, b: int | None) -> float:
        if b is None:
            return 0.0
        if a is None:
            return from_start[b] if from_start is not None else 0.0
        return distance[a][b]

    best = list(order)
    for _ in range(MAX_IMPROVEMENT_PASSES):
        improved = False
        for i in range(len(best) - 1):
            before = best[i - 1] if i > 0 else None
            for j in range(i + 1, len(best)):
                after = best[j + 1] if j + 1 < len(best) else None
                gain = (
                    edge(before, best[i])
                    + edge(best[j], after)
                    - edge(before, best[j])
                    - edge(best[i], after)
                )
                if gain > 1e-9:
                    best[i : j + 1] = best[i : j + 1][::-1]
                    improved = True
        if not improved:
            break
    return best


def plan_route(stops: Sequence[Stop], *, start: Point | None) -> Route:
    """Order `stops`, given in address order, to drive the least."""
    placed = [stop for stop in stops if stop.point is not None]
    unplaced = [stop.id for stop in stops if stop.point is None]
    points = [stop.point for stop in placed if stop.point is not None]

    given = list(range(len(placed)))
    best, best_km = given, path_km(points, start=start)
    if len(placed) > 1:
        # With a start point the path is anchored there; without one, every
        # stop is tried as the first and the shortest result kept.
        firsts: list[int | None] = (
            [None] if start is not None else list(range(min(len(placed), MAX_FIRST_STOPS)))
        )
        for first in firsts:
            order = _two_opt(_nearest_neighbour(points, first, start), points, start)
            km = path_km([points[i] for i in order], start=start)
            if km < best_km - 1e-9:
                best, best_km = order, km

    legs: dict[str, float | None] = {}
    previous = start
    for index in best:
        here = points[index]
        legs[placed[index].id] = haversine_km(previous, here) if previous is not None else None
        previous = here
    for stop_id in unplaced:
        legs[stop_id] = None
    return Route(
        order=[placed[i].id for i in best] + unplaced,
        total_km=best_km,
        legs=legs,
        unplaced=unplaced,
    )
