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

# The frontend pulls the contract; it is no longer pushed from here.
#
# This used to also write ../shiftlush-web/openapi/v1.sha256, and the frontend
# CI compared that checksum against the spec sitting beside it. Both files were
# written by this one command, so the pair agreed with itself no matter how far
# behind it was — the check could catch a hand-edited spec and could never catch
# this repository moving on without a sync, which is what it was for. It didn't:
# the two copies drifted by 214 lines and four /auth endpoints with both
# pipelines green.
#
# The frontend now compares against openapi/v1.yaml on this repository's main
# branch, which is public, so `npm run api:sync` over there needs nothing from
# here and no side-by-side clone. This target survives only as a shortcut for
# someone who does have both repositories open and wants the spec across before
# it is merged; it can no longer write anything a check will read.
sync-spec: spec
	cp openapi/v1.yaml ../shiftlush-web/openapi/v1.yaml
	@echo "Copied to ../shiftlush-web/openapi/v1.yaml (uncommitted, and not the"
	@echo "published contract until this repository's main branch has it)."
	@echo "The supported path is 'npm run api:sync' in shiftlush-web."
