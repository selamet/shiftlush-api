"""Fixtures every test in the suite gets.

Deliberately almost empty. Shared fixtures are convenient and they are also how
a test comes to depend on something no one reading it can see, so the bar for
adding one here is that leaving it out breaks tests that have nothing to do with
each other.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    """Start every test with an empty cache.

    The database is rolled back between tests and the cache is not: locmem
    outlives the whole session. That was survivable while the cache held rate
    limit windows and geocoding results, both of which are keyed per test in
    practice. It stopped being survivable when the sign-in lockout moved in —
    the counter is keyed on (e-mail, address), and every test in the suite signs
    in as a handful of fixture addresses from 127.0.0.1.

    Without this, five failed sign-ins anywhere in the suite lock the sixth,
    whichever test that turns out to be. It would pass on its own, fail in the
    full run, and move whenever tests are reordered — the most expensive shape a
    test failure comes in.
    """
    cache.clear()
