.PHONY: dev migrate check test test-pg lint format spec sync-spec services

dev:
	uv run python manage.py runserver

migrate:
	uv run python manage.py migrate

check:
	uv run python -Wd manage.py check

test:
	uv run pytest

# The same suite against the engine production actually uses. Trigram search and
# JSONB operators are not covered by the SQLite run, so CI runs this one.
test-pg:
	DATABASE_URL=postgres://shiftlush:shiftlush@localhost:5432/shiftlush uv run pytest

services:
	docker compose up -d

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

# The spec is generated from the code, never edited by hand. A warning means a
# serializer's type is ambiguous, which reaches the frontend as a broken type —
# so warnings fail the build.
spec:
	uv run python manage.py spectacular --file openapi/v1.yaml --fail-on-warn

# Copies the generated spec next door and records its checksum, so the frontend
# CI can prove the two repositories hold the same contract.
sync-spec: spec
	cp openapi/v1.yaml ../shiftlush-web/openapi/v1.yaml
	shasum -a 256 openapi/v1.yaml | cut -d' ' -f1 > ../shiftlush-web/openapi/v1.sha256
