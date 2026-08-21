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

# --------------------------------------------------------------------------
# REST framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
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
        "ContractScope": "apps.contracts.models.Scope.choices",
        "AttachmentCategory": "apps.attachments.models.AttachmentCategory.choices",
    },
}

# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
# The refresh token travels in an httpOnly cookie, so credentials must be
# allowed; the origin list is therefore never a wildcard.
CORS_ALLOW_CREDENTIALS = True
