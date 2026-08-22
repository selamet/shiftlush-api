#!/bin/sh
# Migrations run here rather than in a separate deploy step because there is one
# container: a step that can be skipped is a step that eventually is, and the
# failure mode — a running application against an old schema — is worse than a
# container that refuses to start.
set -e

echo "Applying migrations..."
python manage.py migrate --noinput

echo "Starting application..."
exec "$@"
