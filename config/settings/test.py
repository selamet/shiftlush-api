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
