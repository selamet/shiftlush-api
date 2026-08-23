"""Fixtures the whole suite gets.

There are two, and the bar for a third is that leaving it out breaks tests which
have nothing to do with each other. A shared fixture is convenient and it is
also how a test comes to depend on something nobody reading it can see.

**An empty cache.** The database is rolled back between tests and the cache is
not: locmem outlives the session. That was survivable while the cache held rate
limit windows and geocoding results, both keyed per test in practice. It stopped
being survivable when the sign-in lockout moved in — the counter is keyed on
(e-mail, address), and every test signs in as a handful of fixture addresses
from 127.0.0.1. Without this, five failed sign-ins anywhere lock the sixth,
whichever test that turns out to be: passing alone, failing in the full run, and
moving on reorder — the most expensive shape a failure comes in.

**The N+1 guard specification 8.8 asks for.** The joins are already right; every
list endpoint declares its `select_related` and `prefetch_related`. What was
missing was anything that would notice when one is removed — deleting a
`select_related` breaks no test, changes no response, and surfaces months later
as a slow screen with nothing pointing at the commit. So the rule is enforced
rather than measured: while a list is serialised, the database is closed. The
rows are fetched first, with whatever the view declared; a query after that means
the serialiser is reaching for something the queryset did not bring, which is the
definition of the N+1. Test-run only — in production a slow page still beats a
500. `tests/test_n_plus_one.py` proves the guard fires, so it cannot rot into a
check that passes because it inspects nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache
from django.db.models import Manager, QuerySet
from rest_framework.serializers import ListSerializer
from zen_queries import fetch, queries_disabled


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


@pytest.fixture(autouse=True)
def no_queries_while_serialising_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    original = ListSerializer.to_representation

    def to_representation(self: ListSerializer[Any], data: Any) -> Any:
        rows = data.all() if isinstance(data, Manager) else data
        if self.parent is not None:
            # A nested list inside a single record costs one query, not N of
            # them, so it is not the thing being guarded against. Nested inside
            # a *list*, the block the root opened is still in force — which is
            # exactly where that one query becomes one per row.
            return original(self, rows)
        if isinstance(rows, QuerySet):
            # Outside the block on purpose: fetching the page is the query the
            # view is entitled to, and it is what runs the prefetches too.
            fetch(rows)
        with queries_disabled():
            return original(self, rows)

    monkeypatch.setattr(ListSerializer, "to_representation", to_representation)
