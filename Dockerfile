# Python 3.13 to match .python-version and the wheels the lock file resolved.
FROM python:3.13-slim AS base

# WeasyPrint renders the QR label sheet through Pango and Cairo, which are
# system libraries rather than wheels. Without them label printing returns 503
# in production — the code reports that honestly, but the cause would be a
# half-built image. libpq is for psycopg, ca-certificates for the database's
# sslmode=verify-full and for R2.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libpq5 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# The production database requires sslmode=verify-full; point libpq at the
# system CA bundle explicitly so DATABASE_URL does not have to carry a
# filesystem path.
ENV PGSSLROOTCERT=/etc/ssl/certs/ca-certificates.crt \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /srv

# Dependencies first, from the lock file, so a code change does not reinstall
# them. `--frozen` refuses to resolve: the image gets exactly what CI tested,
# or the build fails.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group pdf --no-install-project

COPY . .
RUN uv sync --frozen --no-dev --group pdf

# Collected at build time rather than at boot: it is identical for every
# container from this image, and doing it in the entrypoint would repeat the
# work on every restart.
#
# Against the base settings, not production. Collecting static files needs
# STATIC_ROOT and the storage backend and nothing else — running it under
# production settings would mean feeding the build a dozen fake secrets, and
# every new required setting would then break the image build rather than the
# thing it is actually required for. That already happened once, to
# CSRF_TRUSTED_ORIGINS.
RUN DJANGO_SETTINGS_MODULE=config.settings.base \
    python manage.py collectstatic --noinput --clear

ENV DJANGO_SETTINGS_MODULE=config.settings.production

RUN useradd --create-home --uid 10001 app && chown -R app:app /srv
USER app

EXPOSE 8000
ENTRYPOINT ["/srv/entrypoint.sh"]
# Two workers per core is the usual starting point; this box runs five apps, so
# it starts conservative and is raised from the compose file if it needs to be.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--forwarded-allow-ips", "*"]
