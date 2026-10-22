"""The day's order of stops: shortest straight-line path, never worse than the address order."""

from __future__ import annotations

import itertools

import pytest

from apps.maintenance.routing import Stop, haversine_km, path_km, plan_route

# Four stops on a line, west to east, about 1.7 km apart at Erzurum's latitude.
WEST, MID_W, MID_E, EAST = (
    Stop("w", 39.9, 41.20),
    Stop("mw", 39.9, 41.22),
    Stop("me", 39.9, 41.24),
    Stop("e", 39.9, 41.26),
)


def test_haversine_is_symmetric_and_about_right():
    a, b = (39.9, 41.2), (39.9, 41.3)
    assert haversine_km(a, b) == pytest.approx(haversine_km(b, a))
    assert haversine_km(a, b) == pytest.approx(8.53, abs=0.05)
    assert haversine_km(a, a) == 0


def test_a_shuffled_line_is_driven_end_to_end():
    route = plan_route([MID_E, WEST, EAST, MID_W], start=None)
    assert route.order in (["w", "mw", "me", "e"], ["e", "me", "mw", "w"])
    assert route.total_km == pytest.approx(haversine_km((39.9, 41.20), (39.9, 41.26)), rel=1e-6)


def test_a_start_point_decides_the_direction():
    route = plan_route([MID_E, WEST, EAST, MID_W], start=(39.9, 41.27))
    assert route.order == ["e", "me", "mw", "w"]
    # The first leg is from the start point.
    assert route.legs["e"] == pytest.approx(haversine_km((39.9, 41.27), (39.9, 41.26)))


def test_never_longer_than_the_address_order():
    stops = [
        Stop("a", 39.91, 41.27),
        Stop("b", 39.87, 41.29),
        Stop("c", 39.95, 41.11),
        Stop("d", 39.90, 41.25),
        Stop("e", 39.93, 41.30),
    ]
    for given in itertools.permutations(stops):
        route = plan_route(list(given), start=None)
        address = path_km([s.point for s in given if s.point is not None], start=None)
        assert route.total_km <= address + 1e-9


def test_the_same_input_gives_the_same_order():
    stops = [Stop(str(i), 39.9 + i * 0.001, 41.2 + (i * 7 % 5) * 0.01) for i in range(12)]
    assert plan_route(stops, start=None).order == plan_route(stops, start=None).order


def test_stops_without_a_position_come_last_in_the_order_given():
    route = plan_route([Stop("x", None, None), EAST, Stop("y", None, 41.2), WEST], start=None)
    assert route.order[2:] == ["x", "y"]
    assert route.unplaced == ["x", "y"]
    assert route.legs["x"] is None


def test_one_stop_and_none():
    assert plan_route([WEST], start=None).order == ["w"]
    assert plan_route([WEST], start=None).total_km == 0
    assert plan_route([], start=None).order == []


def test_a_long_day_tries_only_a_few_first_stops_and_is_never_worse(monkeypatch):
    # Counted, not timed: a wall-clock bound passes on a laptop and fails under
    # CI's coverage tracing. What keeps a long day quick is how many first stops
    # are tried, so that is what is held.
    import random

    from apps.maintenance import routing

    tried: list[int | None] = []
    original = routing._nearest_neighbour

    def counting(points, first, start):
        tried.append(first)
        return original(points, first, start)

    monkeypatch.setattr(routing, "_nearest_neighbour", counting)
    rng = random.Random(7)  # noqa: S311
    stops = [
        Stop(str(i), 39.85 + rng.random() * 0.1, 41.2 + rng.random() * 0.15) for i in range(150)
    ]
    route = plan_route(stops, start=None)
    assert len(tried) == routing.MAX_FIRST_STOPS
    assert sorted(route.order) == sorted(s.id for s in stops)
    assert route.total_km <= path_km([s.point for s in stops if s.point], start=None) + 1e-9
