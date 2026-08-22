"""Settings shared by every environment.

Environment-specific modules import this with `from .base import *` and then
override. Nothing here reads `os.environ` directly — everything goes through
django-environ so types and defaults live in one place.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-development-key")
DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    # Local
    "core",
    "apps.address",
    "apps.companies",
    "apps.users",
    "apps.customers",
    "apps.properties",
    "apps.elevators",
    "apps.contracts",
    "apps.attachments",
    "apps.audit",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Immediately after SecurityMiddleware, which is where it has to be: it
    # serves static files before anything else in the chain gets to run.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.RequestIDMiddleware",
    # Must run after authentication: it reads the company off the user.
    "core.middleware.CompanyContextMiddleware",
    "core.middleware.APIVersionHeaderMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

# Local development and tests run on SQLite so nothing has to be installed or
# started first. Production is PostgreSQL, set through DATABASE_URL.
#
# Two things genuinely cannot be exercised on SQLite and have to be verified
# against PostgreSQL before release:
#   - the pg_trgm index behind neighbourhood typeahead has no SQLite equivalent,
#     so that search falls back to a prefix match locally;
#   - JSONB query operators used on audit_log are emulated by SQLite's JSON1 and
#     do not have the same semantics.
# Everything else the schema relies on — partial unique indexes and check
# constraints — is supported by both.
DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}

# Every model declares a UUIDv7 primary key explicitly. This is set anyway so a
# model that forgets fails loudly at `makemigrations` rather than silently
# getting a sequential integer that would then leak in URLs.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

AUTH_USER_MODEL = "users.User"

# Argon2 first: the list order decides what new passwords are hashed with.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

# Read lazily by core.crypto; a missing key raises at first use rather than at
# import, so migrations and `manage.py` still work without it.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")

# --------------------------------------------------------------------------
# Object storage
# --------------------------------------------------------------------------

# Configured per backend rather than once, which is the whole point of
# attachment.storage_backend: a signed URL is produced from the backend the row
# was written to, not from today's default. That is what lets the categories
# holding personal data move to a Turkey-resident provider later without a
# migration and without breaking every file already uploaded.
STORAGE_BACKENDS: dict[str, dict[str, str]] = {
    # MinIO locally. R2 has no local emulator, and writing to the real bucket
    # from a developer machine pollutes production data.
    "local": {
        "endpoint_url": env("S3_ENDPOINT_URL", default="http://localhost:9000"),
        "access_key_id": env("S3_ACCESS_KEY_ID", default="shiftlush"),
        "secret_access_key": env("S3_SECRET_ACCESS_KEY", default="shiftlush123"),
        "bucket": env("S3_BUCKET_NAME", default="shiftlush-dev"),
        # MinIO ignores the region; S3-compatible clients still require one.
        "region": "us-east-1",
    },
    "r2": {
        "endpoint_url": env("R2_ENDPOINT_URL", default=""),
        "access_key_id": env("R2_ACCESS_KEY_ID", default=""),
        "secret_access_key": env("R2_SECRET_ACCESS_KEY", default=""),
        "bucket": env("R2_BUCKET_NAME", default=""),
        # R2 wants the literal string "auto"; the jurisdiction is fixed on the
        # bucket at creation time and cannot be changed afterwards.
        "region": "auto",
    },
    # Reserved. Nothing is written here in phase 1 — the entry exists so the
    # move is a configuration change rather than a code change.
    "tr_provider": {
        "endpoint_url": env("TR_ENDPOINT_URL", default=""),
        "access_key_id": env("TR_ACCESS_KEY_ID", default=""),
        "secret_access_key": env("TR_SECRET_ACCESS_KEY", default=""),
        "bucket": env("TR_BUCKET_NAME", default=""),
        "region": env("TR_REGION", default="auto"),
    },
}

#: Where new uploads go. Existing rows keep their own backend.
DEFAULT_STORAGE_BACKEND = env("STORAGE_BACKEND", default="local")

#: Per-category override, for moving one class of file without moving the rest.
#: Empty in phase 1; `{"signed_contract": "tr_provider"}` is the shape.
STORAGE_BACKEND_BY_CATEGORY: dict[str, str] = {}

# Short enough that a leaked URL is worthless within the hour, long enough that
# a 10 MB upload over a poor mobile connection still finishes.
UPLOAD_URL_TTL_SECONDS = 15 * 60
DOWNLOAD_URL_TTL_SECONDS = 5 * 60

#: Days a soft-deleted attachment keeps its bytes before the sweeper removes
#: them. The row itself stays forever — the audit trail refers to it.
ATTACHMENT_PURGE_AFTER_DAYS = 30

AUTH_PASSWORD_VALIDATORS = [
    # Length beats composition rules, so there is no upper/lower/digit/symbol
    # requirement — only a floor of 10 and a common-password blocklist.
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
]

# --------------------------------------------------------------------------
# E-mail
# --------------------------------------------------------------------------

# Every e-mail this system sends carries a single-use link into the frontend, so
# the frontend's public address is part of the mail configuration rather than a
# CORS detail.
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173").rstrip("/")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="ShiftLush <noreply@shiftlush.local>")

# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------

# The API never returns Turkish text; this is for invitation e-mails and the
# QR label template, whose strings live in locale/tr, not in code.
LANGUAGE_CODE = "tr"
LOCALE_PATHS = [BASE_DIR / "locale"]

# Timestamps are stored and served in UTC without exception. Localisation to
# Europe/Istanbul happens in the browser; the backend never produces local time.
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Hashed filenames and a manifest, so a stale browser cache cannot serve last
# release's stylesheet against this release's markup.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # simplejwt's class, plus a join onto the company — see core.authentication
        # for why several endpoints would otherwise run an extra query each.
        "core.authentication.CompanyAwareJWTAuthentication",
    ),
    # A view that forgets to declare permissions is closed, not open.
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    # A version outside this list 404s rather than quietly falling back to v1.
    "ALLOWED_VERSIONS": ["v1"],
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Money crosses the wire as a string so JavaScript's float arithmetic never
    # touches it.
    "COERCE_DECIMAL_TO_STRING": True,
    "EXCEPTION_HANDLER": "core.exceptions.exception_handler",
    "DEFAULT_PAGINATION_CLASS": "core.pagination.StandardPagination",
    "PAGE_SIZE": 25,
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "ShiftLush API",
    "VERSION": "v1",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v[0-9]",
    # The contract is generated in English even though the application runs in
    # Turkish — see core.spectacular for why that is not a contradiction.
    "PREPROCESSING_HOOKS": ["core.spectacular.use_english"],
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        # The error vocabulary cannot be inferred from the views, so it is
        # added here from the enum rather than maintained by hand.
        "core.spectacular.add_error_codes",
        "core.spectacular.restore_language",
    ],
    "COMPONENT_SPLIT_REQUEST": True,
    # Two models can both call a field "role" or "type" and mean different
    # things. Left alone, the generator invents names like RoleE29Enum — which
    # then appear verbatim in the frontend's types and change whenever the hash
    # does. Naming them here keeps the generated client stable and readable.
    "ENUM_NAME_OVERRIDES": {
        "UserRole": "apps.users.models.Role.choices",
        "ContactRole": "apps.customers.models.ContactRole.choices",
        "CustomerType": "apps.customers.models.CustomerType.choices",
        "BuildingType": "apps.properties.models.BuildingType.choices",
        "NeighborhoodType": "apps.address.models.NeighborhoodType.choices",
        "ElevatorStatus": "apps.elevators.models.ElevatorStatus.choices",
        "ElevatorCategory": "apps.elevators.models.Category.choices",
        "ContractStatus": "apps.contracts.models.ContractStatus.choices",
        "AttachmentObjectType": "apps.attachments.models.ObjectType.choices",
        "AttachmentCategory": "apps.attachments.models.AttachmentCategory.choices",
        "ContractScope": "apps.contracts.models.Scope.choices",
    },
}

# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
# The refresh token travels in an httpOnly cookie, so credentials must be
# allowed; the origin list is therefore never a wildcard.
CORS_ALLOW_CREDENTIALS = True
