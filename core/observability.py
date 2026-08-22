"""Reporting errors without reporting the data that caused them.

Until now a production exception was visible only in `docker logs`, which means
it was visible only to somebody already looking. This sends them somewhere that
notices.

The whole difficulty is that an error report is most useful when it carries the
request that produced it, and this request is the one thing that must not leave
the building. A validation failure on the customer form carries a national
identity number. A failed sign-in carries a password. An invitation carries a
token that is a live credential until it is used. The database encrypts the
first of those at rest; shipping it to a third party in an error report would
undo that with none of the effort.

So the request body is never sent — not scrubbed, not truncated, never
collected — and the fields that identify a person are replaced by the ids that
identify the *record*. `user_id` and `company_id` are enough to find anybody in
the admin; their name and address add nothing to a stack trace.

What is deliberately kept is `request_id`. It is already on every log line and
in the body of every 500, so the number a user reads off their screen leads
straight to the event.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Keys whose value never leaves the process, wherever it turns up.
#:
#: This is defence in depth rather than the defence: bodies are not collected at
#: all, so nothing here should ever match. It exists because "should never
#: match" and "does not match" are different claims, and one of them is testable
#: only by leaving the net in place.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "current_password",
        "new_password",
        "national_id",
        "token",
        "refresh",
        "access",
        "token_hash",
        "qr_token",
        "authorization",
        "cookie",
        "set-cookie",
        "x-csrftoken",
        "idempotency-key",
    }
)

REDACTED = "[redacted]"


def _prune(value: Any, depth: int = 0) -> Any:
    """Replace anything sensitive, however deeply it is nested."""
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: (REDACTED if str(key).lower() in SENSITIVE_KEYS else _prune(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_prune(item, depth + 1) for item in value]
    return value


def before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Strip the request down to what is safe, then say who it belonged to."""
    request = event.get("request")
    if isinstance(request, dict):
        # Belt and braces: `max_request_body_size="never"` already means this
        # key is absent. Removing it unconditionally means a future change to
        # that setting cannot quietly start sending bodies.
        request.pop("data", None)
        request.pop("cookies", None)
        request.pop("env", None)
        if isinstance(request.get("headers"), dict):
            request["headers"] = _prune(request["headers"])
        # The query string is part of the URL and can carry a reset token.
        request.pop("query_string", None)

    extra = _prune(event.get("extra") or {})
    event["extra"] = extra

    # Imported here rather than at module scope: this module is imported from
    # the settings, and reaching into the app layer while settings are still
    # being read is how a circular import starts.
    from core.context import get_current_actor, get_current_company_id

    tags = event.setdefault("tags", {})
    company_id = get_current_company_id()
    if company_id is not None:
        tags["company_id"] = str(company_id)

    actor = get_current_actor()
    user_id = getattr(actor, "user_id", None) or getattr(actor, "id", None)
    if user_id is not None:
        # An id, not a name and not an address. Enough to find the person in
        # the admin, nothing that identifies them to a reader of the report.
        event["user"] = {"id": str(user_id)}
    else:
        event.pop("user", None)

    # The id the user can read off their own error screen. It arrives on the
    # log record -- `core.exceptions` puts it there for every 500 -- so it is
    # lifted out of `extra` and made a tag, which is what Sentry can search on.
    request_id = extra.get("request_id")
    if request_id:
        tags["request_id"] = str(request_id)

    return event


def init_sentry(
    *,
    dsn: str,
    environment: str,
    release: str = "",
    traces_sample_rate: float = 0.0,
) -> None:
    """Start reporting, if a destination has been configured.

    Values are passed in rather than read from `django.conf.settings`, because
    this is called from the settings module itself — at that moment the settings
    are still being assembled and reading them back would either fail or, worse,
    quietly return a default.

    Silent when the DSN is empty, which is local development and the test suite:
    a developer's mistakes are not production incidents, and a test run that
    reported its own deliberate failures would bury the real ones.
    """
    if not dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:  # pragma: no cover - the dependency is declared
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed.")
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release or None,
        integrations=[
            DjangoIntegration(),
            # `logger.exception(...)` already marks the places this codebase
            # considers worth reporting; without this they would stay in stdout
            # while only uncaught errors travelled.
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        # The setting this module exists for. Sentry's own example turns it on,
        # and on an application holding national identity numbers that would
        # send them, in clear, in the body of a validation error.
        send_default_pii=False,
        # And the one that actually stops bodies being collected. Without it
        # `send_default_pii=False` still permits a request body of medium size.
        max_request_body_size="never",
        before_send=before_send,
        traces_sample_rate=traces_sample_rate,
    )
