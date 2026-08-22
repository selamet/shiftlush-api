"""How the cache is wired to the shared Redis.

The Redis at redis.selamet.dev serves several applications from one process.
Redis ACLs are not database-aware — a key pattern matches in all sixteen
numbered databases — so the boundary between applications is the key prefix,
and the credential is refused every key outside `shiftlush:`. That makes
KEY_PREFIX load-bearing rather than cosmetic: get it wrong and the connection
authenticates, reports itself healthy, and then fails on the first write with
NOPERM.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import caches

REDIS_CACHE = {
    "BACKEND": "django.core.cache.backends.redis.RedisCache",
    "LOCATION": "rediss://shiftlush:password@redis.selamet.dev:6379/0",
    "KEY_PREFIX": settings.REDIS_KEY_PREFIX,
}


def test_every_key_stays_inside_the_namespace_the_acl_allows(settings) -> None:
    settings.CACHES = {"default": REDIS_CACHE}
    cache = caches.create_connection("default")

    for key in ("neighbourhoods:34", "throttle:login:1.2.3.4"):
        assert cache.make_key(key).startswith(f"{settings.REDIS_KEY_PREFIX}:")


def test_the_suite_never_reaches_for_a_running_redis() -> None:
    # Not a tautology: base.py switches to Redis the moment REDIS_URL is set,
    # and a developer with it exported in their shell would otherwise run the
    # whole suite against the shared production cache.
    assert settings.CACHES["default"]["BACKEND"].endswith("locmem.LocMemCache")
