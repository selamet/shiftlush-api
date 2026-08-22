"""Test settings.

Tests run on SQLite by default so the suite needs nothing installed. Set
DATABASE_URL to a PostgreSQL instance to run the same suite against the engine
production uses — CI should do exactly that, because trigram search and JSONB
operators are not covered by the SQLite run.
"""

import base64
import os

from .base import *

DEBUG = False
ALLOWED_HOSTS = ["testserver"]

# The slow hasher is deliberate in production and pointless in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# A throwaway key so the suite needs no environment setup. Generated per run,
# which also proves nothing in the tests depends on a fixed key. Built inline
# rather than imported from core.crypto, so settings stay free of app imports.
FIELD_ENCRYPTION_KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()

# In memory even when REDIS_URL is exported in the shell. The suite must not
# need a service running, and a shared instance would carry state from one run
# into the next.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# The two default limits are off for the suite, and switched on one test at a
# time by tests/test_rate_limit.py.
#
# They cannot simply be left on. The counter lives in the cache, the cache is
# not cleared between tests, and the anonymous bucket is keyed on an address —
# which is `127.0.0.1` for every test in the run. The twenty-first anonymous
# request of the whole session would be refused, so the suite would fail in
# whichever test happened to make it, move when tests are reordered, and pass
# again when run individually. The authenticated bucket is keyed per user and
# would not bite, but leaving one of the pair on and calling that coverage would
# be worse than being explicit.
#
# `None` rather than a missing key: DRF resolves a rate from the scope name and
# raises ImproperlyConfigured when the scope is absent, while a None rate means
# "not throttled" and exercises the same code path production takes for a view
# with no limit. The geocode scope keeps its rate — it is keyed per user, and
# its own tests assert on it.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_THROTTLE_RATES": {
        **REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
        "anon": None,
        "user": None,
    },
}
