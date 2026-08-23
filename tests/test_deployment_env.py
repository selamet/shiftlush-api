"""Every setting the deployment reads must be one the deployment can set.

Compose substitutes the names it is given and nothing else, so a variable the
settings read and `docker-compose.prod.yml` does not name is unreachable: it can
be written into `.env`, the file will look right, and nothing will change. There
is no error and no warning — the wrong value simply keeps being used.

That has now happened three times in this project.

- `REDIS_URL` had no default, so the omission was at least loud: the container
  refused to boot and took production down for twenty-five minutes.
- `SENTRY_DSN` had one, so it was silent. The DSN went into `.env`, the deploy
  reported success, and error reporting stayed off.
- `ADDRESS_PROVINCES` would have been the third, on a branch whose whole purpose
  was to make the address scope a property of the deployment.

Checking the four Sentry names by hand caught none of the other twenty-one that
were missing when this was written, because a hand-kept list only covers what
somebody remembered. So this derives the list from the settings themselves: the
next variable is covered by the act of reading it, which is the only version of
this check that stays true.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.prod.yml"
SETTINGS = (REPO / "config/settings/base.py", REPO / "config/settings/production.py")

#: `env("NAME"` and `env.int("NAME"`, `env.float(...)`, `env.bool(...)`.
READS_ENV = re.compile(r'\benv(?:\.\w+)?\(\s*"([A-Z][A-Z0-9_]*)"')

#: Names the container is given some other way, so their absence from the
#: environment list is correct rather than an oversight.
#:
#: Listed rather than pattern-matched, and checked below for staleness: an
#: exemption that no longer applies is how a list like this stops meaning
#: anything.
NOT_FROM_ENV_FILE: frozenset[str] = frozenset()


def settings_variables() -> set[str]:
    names: set[str] = set()
    for path in SETTINGS:
        names |= set(READS_ENV.findall(path.read_text()))
    return names


def test_every_setting_read_from_the_environment_is_forwarded():
    compose = COMPOSE.read_text()
    names = settings_variables()

    # A pattern that stops matching would report a clean run over nothing, which
    # is the failure this file exists to prevent, one level up.
    assert len(names) > 20, (
        f"only {len(names)} settings variables found — the pattern has stopped matching"
    )

    missing = sorted(
        name for name in names - NOT_FROM_ENV_FILE if f"- {name}=${{{name}" not in compose
    )
    assert not missing, (
        "these settings are read from the environment but not forwarded to the "
        "container, so setting them in .env does nothing:\n  " + "\n  ".join(missing)
    )


def test_no_exemption_outlives_its_reason():
    names = settings_variables()
    stale = sorted(NOT_FROM_ENV_FILE - names)
    assert not stale, (
        "these names are exempted but no setting reads them any more; delete the "
        "exemption:\n  " + "\n  ".join(stale)
    )


def test_a_required_setting_is_named_in_the_example_file():
    """Anything without a default has to be discoverable before the first boot.

    `production.py` deliberately gives no default to the values whose fallback
    would be worse than a crash. Somebody bringing up an environment finds those
    by reading `.env.example`, and a required variable missing from it is found
    instead by the container refusing to start.
    """
    example = (REPO / ".env.example").read_text()
    production = (REPO / "config/settings/production.py").read_text()

    required = {
        match.group(1)
        for match in re.finditer(r'\benv(?:\.\w+)?\(\s*"([A-Z][A-Z0-9_]*)"\s*\)', production)
    }
    assert required, "no no-default settings found — the pattern has stopped matching"

    missing = sorted(name for name in required if name not in example)
    assert not missing, (
        "production refuses to boot without these and .env.example does not "
        "mention them:\n  " + "\n  ".join(missing)
    )
