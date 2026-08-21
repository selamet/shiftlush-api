"""Production settings.

Required values have no defaults on purpose: a missing secret should stop the
process at boot, not silently fall back to something insecure that nobody
notices until it is exploited.
"""

from .base import *
from .base import env

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
DATABASES = {"default": env.db("DATABASE_URL")}
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS")
# No default: booting without it would silently write national IDs in plaintext.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY")

# Files live in R2 in production, in a bucket whose jurisdiction is pinned to
# the EU at creation time. Falling back to the local MinIO credentials here
# would produce an instance that signs URLs nobody can use, and only the first
# user to upload something would find out.
DEFAULT_STORAGE_BACKEND = "r2"
STORAGE_BACKENDS = {
    **STORAGE_BACKENDS,
    "r2": {
        "endpoint_url": env("R2_ENDPOINT_URL"),
        "access_key_id": env("R2_ACCESS_KEY_ID"),
        "secret_access_key": env("R2_SECRET_ACCESS_KEY"),
        "bucket": env("R2_BUCKET_NAME"),
        "region": "auto",
    },
}

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
REFERRER_POLICY = "same-origin"

# The schema names every field and business rule in the system, so the docs
# endpoint is closed here and served only behind an IP restriction.
SPECTACULAR_SETTINGS = {**SPECTACULAR_SETTINGS, "SERVE_PUBLIC": False}
