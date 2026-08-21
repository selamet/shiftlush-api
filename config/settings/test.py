"""Test settings.

Tests run on SQLite by default so the suite needs nothing installed. Set
DATABASE_URL to a PostgreSQL instance to run the same suite against the engine
production uses — CI should do exactly that, because trigram search and JSONB
operators are not covered by the SQLite run.
"""

from .base import *

DEBUG = False
ALLOWED_HOSTS = ["testserver"]

# The slow hasher is deliberate in production and pointless in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
