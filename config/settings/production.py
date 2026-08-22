"""Production settings.

Required values have no defaults on purpose: a missing secret should stop the
process at boot, not silently fall back to something insecure that nobody
notices until it is exploited.
"""

from django.core.exceptions import ImproperlyConfigured

from core.observability import init_sentry

from .base import *
from .base import env

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
DATABASES = {"default": env.db("DATABASE_URL")}
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS")
# The frontend is a different origin on the same registrable domain, so the
# refresh cookie is same-site and Lax works. CSRF still needs the origin listed
# explicitly, because Django checks it against the Origin header for unsafe
# methods regardless of CORS.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS")
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

# The cache is the shared Redis at redis.selamet.dev. No default: the in-memory
# fallback would boot happily and then quietly diverge between gunicorn workers,
# which is exactly the class of bug nobody finds until it matters.
REDIS_URL = env("REDIS_URL")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": REDIS_KEY_PREFIX,
    }
}

# Optional, unlike everything else here, because nothing reads it yet. Making it
# required now would stop a deployment over a value that has no effect.
REDIS_QUEUE_URL = env("REDIS_QUEUE_URL", default="")

# Invitations and password resets are the only way anyone but the founder gets
# into the system, so a deployment without a working mail route is not a
# deployment. No default: it fails at boot rather than at the first invitation.
EMAIL_CONFIG = env.email_url("EMAIL_URL")
vars().update(EMAIL_CONFIG)

# django-environ selects TLS from the *scheme* — `smtp+tls://` — and silently
# ignores a `?tls=True` query parameter, which is the form most documentation
# suggests and the one that looks obviously correct. Getting it wrong does not
# merely break mail: SMTP AUTH would send the provider's API key across the
# internet in clear text. Resend refuses the unencrypted session, so the failure
# surfaced at the first send; a provider that accepted it would have leaked the
# credential on every message.
#
# Refused at boot rather than at the first invitation, because the first
# invitation is somebody's real account.
if not (EMAIL_CONFIG.get("EMAIL_USE_TLS") or EMAIL_CONFIG.get("EMAIL_USE_SSL")):
    raise ImproperlyConfigured(
        "EMAIL_URL must enable TLS. Use the scheme smtp+tls:// (STARTTLS, port "
        "587) or smtp+ssl:// (implicit TLS, port 465). A `?tls=True` query "
        "parameter is ignored, and without encryption the SMTP password is "
        "sent in clear text."
    )
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL")
FRONTEND_URL = env("FRONTEND_URL").rstrip("/")

# Caddy terminates TLS and proxies to 127.0.0.1, so Django sees plain HTTP and
# would otherwise redirect forever. The header is set by the proxy in front of
# it; trusting it is only safe because nothing else can reach the port.
SECURE_SSL_REDIRECT = True

# One proxy — Caddy — so the right-most X-Forwarded-For entry is the address it
# accepted the connection from, and everything to its left is whatever the
# caller chose to send. Getting this wrong is not cosmetic: at zero, REMOTE_ADDR
# is the Docker gateway and every anonymous caller in the world shares a single
# twenty-a-minute bucket; at two, a forged entry buys a bucket per request. It
# is a fact about the deployment, so it is settable, but the default is the
# deployment we have.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=1)

# The two infrastructure endpoints are exempt, because the things that call
# them are inside the host and speak plain HTTP on the loopback: Docker's own
# healthcheck and anything a load balancer runs against the container directly.
# Neither sets X-Forwarded-Proto, so both were redirected to an https port that
# does not exist — the container reported itself unhealthy for its entire life
# while serving traffic perfectly well through Caddy.
SECURE_REDIRECT_EXEMPT = [r"^health$", r"^ready$"]
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

# Reporting starts here rather than in `base`, so that importing the settings in
# a test or a shell never opens a connection to a third party.
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", default="production")
init_sentry(
    dsn=SENTRY_DSN,
    environment=SENTRY_ENVIRONMENT,
    release=SENTRY_RELEASE,
    traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
)
