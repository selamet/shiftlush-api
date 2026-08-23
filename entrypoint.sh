#!/bin/sh
# Migrations run here rather than in a separate deploy step because there is one
# container: a step that can be skipped is a step that eventually is, and the
# failure mode — a running application against an old schema — is worse than a
# container that refuses to start.
set -e

echo "Applying migrations..."
python manage.py migrate --noinput

# Reference data belongs here for the same reason, and fails the same way: with
# the province, district and neighbourhood tables empty, no building, complex or
# customer can be created, so an environment missing them is not a degraded
# deployment but a broken one. As a separate deploy step it would eventually be
# skipped; as a line in DEPLOYMENT.md it already was.
#
# --if-missing is what keeps this off the restart path. When the data is there
# it is three existence queries and nothing else — no CSV parsing, no fifty
# thousand upserts, no write locks on a table the outgoing container is still
# serving reads from. When it is not, the load is a few seconds, once, before
# the port opens.
#
# The yearly data refresh is deliberately NOT covered by this: new CSVs in the
# image change nothing until someone runs the command without the flag. See
# DEPLOYMENT.md.
#
# How much of the country gets loaded is ADDRESS_PROVINCES, read by the command
# itself. Narrowing it is the other thing this line deliberately does not do:
# --if-missing loads what is newly in scope and never deletes what has fallen
# out of it, because fifty thousand rows and a possible refusal is not something
# to discover during a container start. It warns instead, and DEPLOYMENT.md has
# the one-line command that acts on the warning.
echo "Loading address reference data if absent..."
python manage.py load_address_data --if-missing

echo "Starting application..."
exec "$@"
