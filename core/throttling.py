"""How many requests a caller gets, and how the answer is reported.

Specification 8.13: twenty a minute per IP without a token, three hundred a
minute per user with one. 8.9: every response carries `RateLimit-Limit`,
`RateLimit-Remaining` and `RateLimit-Reset`, and a refusal carries `Retry-After`
as well.

**One counter, not two.** DRF's usual arrangement stacks `AnonRateThrottle` and
`UserRateThrottle` and lets each ignore the requests that are not its business.
That works, but it leaves an anonymous request counted in two buckets and the
headers with two limits to choose between. A single class that picks its scope
per request has one bucket, one limit, and one obvious thing to report.

**Where the counter lives.** In the Django cache, which is Redis in production
and therefore shared by every gunicorn worker. That matters more than it looks:
a per-process counter does not enforce the configured limit at all, it enforces
the limit times the number of workers, because each worker refuses only the
requests it happened to be handed. With three workers and a limit of twenty, a
client that keeps a connection pool open gets sixty. Locally and in the test
suite the cache is in memory and single-process, so the arithmetic is the same
by accident rather than by design — see `tests/test_rate_limit.py` for the
measurement, and the deviations table in the specification for why Redis is
here at all.

**Anonymous requests to a closed endpoint are not counted.** DRF authenticates,
then checks permissions, then checks throttles, so a request with no token to an
endpoint that requires one is refused with 401 before it reaches this code. That
is the specification's reading too — 8.13 sets the anonymous limit on the
endpoints that admit anonymous callers — and those 401s cost a token parse and
no query. What it means in practice is that the twenty a minute applies to
login, refresh, registration and password reset, which are the endpoints where
an unauthenticated caller can actually spend something.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rest_framework import throttling

from core.client_ip import client_ip

if TYPE_CHECKING:
    # Imported for annotations only. Settings name this module in
    # DEFAULT_THROTTLE_CLASSES, which DRF resolves while `rest_framework.views`
    # is still executing its own module body — so importing that module here at
    # run time is a circular import, and the error it raises names the setting
    # rather than the cycle.
    from rest_framework.request import Request
    from rest_framework.views import APIView

#: Where the throttle leaves the quota for the middleware to report. Set on the
#: underlying `HttpRequest` rather than DRF's wrapper, because that is the
#: object middleware is handed on the way back out.
STATE_ATTRIBUTE = "rate_limit_state"

ANON_SCOPE = "anon"
USER_SCOPE = "user"


@dataclass(frozen=True)
class RateLimitState:
    """What the headers in 8.9 report."""

    limit: int
    remaining: int
    reset: int


class ReportsQuota:
    """Records what a throttle decided, so the response can carry it.

    A mixin rather than a base class: it has to sit in front of whichever DRF
    throttle is doing the counting, and there are two of those.

    `RateLimit-Reset` is the number of seconds until the caller may send one
    more request — the moment the oldest request in the window falls out of it.
    A sliding window has no single instant when the whole quota returns, so the
    alternative would be the moment the *newest* request expires, which is a
    minute away no matter how much quota is left and tells a client nothing.
    Defined this way the header agrees with `Retry-After` exactly when it
    matters: at zero remaining they are the same number.
    """

    def allow_request(self, request: Request, view: APIView) -> bool:
        allowed: bool = super().allow_request(request, view)  # type: ignore[misc]
        self._record(request)
        return allowed

    def get_ident(self, request: Request) -> str:
        """The address the counter is keyed on for an anonymous caller.

        DRF's own version reads `X-Forwarded-For` from the left, which a caller
        can set. That is the whole limit: one forged header per request and
        every request lands in a bucket of its own.
        """
        # "unknown" rather than None: a caller whose address cannot be
        # determined shares one bucket with the others, instead of dropping out
        # of the count entirely, which is what a null key would do.
        return client_ip(request) or "unknown"

    def _record(self, request: Request) -> None:
        rate = getattr(self, "rate", None)
        history = getattr(self, "history", None)
        if rate is None or history is None:
            # No rate configured, or no cache key for this caller. Nothing was
            # counted, so there is no quota to report.
            return

        num_requests: int = self.num_requests  # type: ignore[attr-defined]
        duration: int = self.duration  # type: ignore[attr-defined]
        now: float = self.now  # type: ignore[attr-defined]

        # On a refusal DRF does not append, so the window holds exactly the
        # limit and this is zero. On success the current request is already in
        # it, so the number is what the caller has left after this one.
        remaining = max(num_requests - len(history), 0)
        # History is newest-first; the last entry is the one that expires next.
        oldest = history[-1] if history else None
        reset = duration if oldest is None else math.ceil(duration - (now - oldest))

        state = RateLimitState(limit=num_requests, remaining=remaining, reset=max(reset, 0))

        # A view with more than one throttle reports the tightest of them: that
        # is the number that decides whether the next request is answered.
        target: Any = getattr(request, "_request", request)
        current: RateLimitState | None = getattr(target, STATE_ATTRIBUTE, None)
        if current is None or state.remaining < current.remaining:
            setattr(target, STATE_ATTRIBUTE, state)


class DefaultRateThrottle(ReportsQuota, throttling.SimpleRateThrottle):
    """The limit that applies to everything that does not declare its own.

    The scope is chosen per request, which is why the rate is resolved in
    `allow_request` rather than in `__init__` the way `SimpleRateThrottle` does
    it: at construction time nobody has authenticated yet, so there is no way to
    know which of the two limits this request is subject to.
    """

    def __init__(self) -> None:
        # Deliberately not calling super().__init__(), which resolves a rate
        # from a scope this class does not have until a request arrives.
        self.scope = ANON_SCOPE

    def allow_request(self, request: Request, view: APIView) -> bool:
        user = getattr(request, "user", None)
        self.scope = USER_SCOPE if user is not None and user.is_authenticated else ANON_SCOPE
        self.rate = self.get_rate()
        # parse_rate(None) is (None, None), and the parent returns True on a
        # None rate, which is how the suite switches the limit off wholesale.
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        if self.scope == USER_SCOPE:
            # The user's id, not their address: a technician on a train changes
            # IP several times an hour and their allowance should not reset
            # each time, nor should an office of twenty share one.
            ident: object = request.user.pk
        else:
            ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class ScopedRateThrottle(ReportsQuota, throttling.ScopedRateThrottle):
    """DRF's scoped throttle, reporting its quota like everything else.

    Used by the endpoints that spend something we do not own — today the one
    calling a third-party geocoder. Declaring `throttle_classes` on a view
    replaces the default rather than adding to it, which is what keeps the
    geocoder's own budget from being counted twice.
    """
