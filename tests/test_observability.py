"""What leaves the process when something goes wrong.

The point of these is not that a scrubbing function exists. It is that a
national identity number typed into a form, and a password typed into a login,
cannot reach a third party by way of an error report — which is a claim about
the whole path, not about one function.

So the transport is replaced and the events it would have sent are read back.
That is the only version of this test worth having: a test that asserted
`send_default_pii=False` would pass while a body sailed out through a setting
nobody thought about.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

# Imported for its side effect: the transport is replaced by patching
# make_transport on sentry_sdk.client, and that attribute exists only once the
# submodule has been loaded.
import sentry_sdk.client  # noqa: F401

from core.context import RequestActor, actor_context, company_context
from core.observability import SENSITIVE_KEYS, before_send, init_sentry, reporting_status

NATIONAL_ID = "12345678901"
PASSWORD = "correct-horse-battery"


class TestNothingSensitiveLeaves:
    def test_a_request_body_is_dropped_whole(self):
        event = before_send(
            {
                "request": {
                    "url": "https://api.example/api/v1/customers/",
                    "method": "POST",
                    "data": {"national_id": NATIONAL_ID, "legal_name": "Örnek A.Ş."},
                }
            },
            {},
        )
        assert "data" not in event["request"]
        assert NATIONAL_ID not in repr(event)

    def test_cookies_and_the_query_string_go_too(self):
        # The reset token travels in a query string, and the refresh token in a
        # cookie. Both are live credentials.
        event = before_send(
            {
                "request": {
                    "url": "https://api.example/api/v1/auth/password-reset/confirm",
                    "cookies": {"refresh": "live-token"},
                    "query_string": "token=live-token",
                }
            },
            {},
        )
        assert "cookies" not in event["request"]
        assert "query_string" not in event["request"]
        assert "live-token" not in repr(event)

    def test_an_authorization_header_is_redacted_not_forwarded(self):
        event = before_send(
            {"request": {"headers": {"Authorization": "Bearer abc.def.ghi", "Accept": "*/*"}}},
            {},
        )
        assert event["request"]["headers"]["Authorization"] == "[redacted]"
        # Ordinary headers are worth keeping; they are how a client is identified.
        assert event["request"]["headers"]["Accept"] == "*/*"

    @pytest.mark.parametrize("key", sorted(SENSITIVE_KEYS))
    def test_every_named_key_is_redacted_wherever_it_appears(self, key):
        event = before_send({"extra": {"payload": {key: "secret"}}}, {})
        assert event["extra"]["payload"][key] == "[redacted]"

    def test_redaction_reaches_a_nested_list(self):
        event = before_send({"extra": {"rows": [{"national_id": NATIONAL_ID}]}}, {})
        assert NATIONAL_ID not in repr(event)


class TestWhatIsKept:
    def test_the_company_and_the_user_are_named_by_id_only(self):
        company_id = uuid.uuid4()
        user_id = uuid.uuid4()
        with company_context(company_id), actor_context(RequestActor(user_id=user_id)):
            event = before_send({}, {})

        assert event["tags"]["company_id"] == str(company_id)
        assert event["user"] == {"id": str(user_id)}

    def test_an_anonymous_request_carries_no_user_at_all(self):
        # Rather than a placeholder, which would group every signed-out error
        # under one imaginary person.
        with actor_context(RequestActor()):
            event = before_send({"user": {"id": "left over", "email": "a@b.c"}}, {})
        assert "user" not in event

    def test_the_ip_address_is_not_promoted_out_of_the_actor(self):
        # It is on the actor because the audit trail needs it. That is a
        # different question from whether it should go to a third party.
        with actor_context(RequestActor(user_id=uuid.uuid4(), ip_address="203.0.113.9")):
            event = before_send({}, {})
        assert "203.0.113.9" not in repr(event)

    def test_the_request_id_becomes_a_tag(self):
        # The number the user reads off their own error screen.
        event = before_send({"extra": {"request_id": "abc123"}}, {})
        assert event["tags"]["request_id"] == "abc123"


class TestTheWholePath:
    """Through a real client, with the transport replaced."""

    @pytest.fixture
    def sent(self, monkeypatch):
        sentry_sdk = pytest.importorskip("sentry_sdk")
        captured: list[dict] = []

        # Subclassed rather than duck-typed: the client reaches for
        # `parsed_dsn` on the way to sending, and a hand-rolled stand-in that
        # lacks it drops every event without a word — which looks exactly like
        # "nothing was reported", the thing under test.
        class Collecting(sentry_sdk.Transport):
            def capture_envelope(self, envelope):
                for item in envelope.items:
                    payload = item.payload.json
                    if payload:
                        captured.append(payload)

            def flush(self, *_args, **_kwargs):
                pass

            def kill(self):
                pass

        # Patched where the client looks it up, not where it is defined: the
        # client imported the name at module load, so replacing it on
        # `sentry_sdk.transport` would leave the client holding the original.
        monkeypatch.setattr(
            sentry_sdk.client, "make_transport", lambda options: Collecting(options)
        )
        init_sentry(dsn="https://key@example.ingest.de.sentry.io/1", environment="test")
        yield captured
        sentry_sdk.get_client().close()

    def test_a_logged_failure_arrives_without_its_payload(self, sent):
        logging.getLogger("tests.observability").error(
            "Unhandled exception",
            extra={"request_id": "req-1", "payload": {"national_id": NATIONAL_ID}},
        )
        import sentry_sdk

        sentry_sdk.get_client().flush()

        assert sent, "the logged error never reached the transport"
        body = repr(sent)
        assert NATIONAL_ID not in body
        assert "req-1" in body

    def test_a_password_never_reaches_the_transport(self, sent):
        logging.getLogger("tests.observability").error(
            "Sign-in failed", extra={"payload": {"password": PASSWORD}}
        )
        import sentry_sdk

        sentry_sdk.get_client().flush()

        assert sent
        assert PASSWORD not in repr(sent)


@pytest.mark.django_db
class TestARealRequest:
    """The claim that matters: a body typed into a form does not travel."""

    def test_a_five_hundred_does_not_carry_the_request_body(self, client, monkeypatch):
        sentry_sdk = pytest.importorskip("sentry_sdk")
        captured: list[dict] = []

        class Collecting(sentry_sdk.Transport):
            def capture_envelope(self, envelope):
                for item in envelope.items:
                    if item.payload.json:
                        captured.append(item.payload.json)

            def flush(self, *_args, **_kwargs):
                pass

            def kill(self):
                pass

        monkeypatch.setattr(
            sentry_sdk.client, "make_transport", lambda options: Collecting(options)
        )
        init_sentry(dsn="https://key@example.ingest.de.sentry.io/1", environment="test")

        # A real endpoint, made to fail the way a bug does, with a body of
        # exactly the sort this application must not leak.
        from apps.users.api.v1 import views

        def explode(*_args, **_kwargs):
            raise RuntimeError("something went wrong deep inside")

        monkeypatch.setattr(views.LoginView, "post", explode)

        try:
            client.post(
                "/api/v1/auth/login",
                data={"email": "a@b.c", "password": PASSWORD, "national_id": NATIONAL_ID},
                content_type="application/json",
            )
        except RuntimeError:
            # Django re-raises in tests; the report has already been made.
            pass

        sentry_sdk.get_client().flush()
        body = repr(captured)
        sentry_sdk.get_client().close()

        assert captured, "the failure was never reported at all"
        assert PASSWORD not in body
        assert NATIONAL_ID not in body


def test_no_dsn_means_nothing_is_started(monkeypatch):
    sentry_sdk = pytest.importorskip("sentry_sdk")

    # Asserting that `init` was never reached, rather than inspecting the
    # client afterwards: a client left active by an earlier test would make
    # that inspection a statement about test ordering instead of about this
    # function.
    started: list[dict] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: started.append(kwargs))

    init_sentry(dsn="", environment="test")

    assert started == []


class TestReportingIsObservable:
    """Off is allowed. Off and unknowable is not.

    `SENTRY_DSN` was absent from the production `.env`, so nothing was being
    reported — which is what the optional setting is for, and was indisting-
    uishable from a deployment with nothing to report. The only way to tell was
    to read a 0600 file on the server. This is the answer /ready gives instead.
    """

    @pytest.fixture(autouse=True)
    def _isolated_client(self):
        """Whatever this leaves behind, put the suite back to reporting nothing.

        Several tests in this module start a real client. Without this the
        assertions below would be about which of them ran first.
        """
        sentry_sdk = pytest.importorskip("sentry_sdk")
        yield
        client = sentry_sdk.get_client()
        if getattr(client, "transport", None) is not None:
            client.close()

    def test_a_deployment_without_a_dsn_says_so(self):
        assert reporting_status() == "disabled"

    def test_a_dsn_that_is_set_but_empty_is_still_disabled(self, monkeypatch):
        """The state a blank line in `.env` produces.

        `init_sentry` returns early, so nothing is started — but the SDK's own
        `is_active()` answers True for a client initialised with an empty DSN,
        which is why the check asks about the transport instead.
        """
        sentry_sdk = pytest.importorskip("sentry_sdk")

        sentry_sdk.init(dsn="")

        assert sentry_sdk.get_client().is_active(), "the SDK's own answer, for contrast"
        assert reporting_status() == "disabled"

    def test_a_configured_deployment_says_ok(self, monkeypatch):
        sentry_sdk = pytest.importorskip("sentry_sdk")

        class Collecting(sentry_sdk.Transport):
            def capture_envelope(self, envelope):
                pass

            def flush(self, *_args, **_kwargs):
                pass

            def kill(self):
                pass

        monkeypatch.setattr(
            sentry_sdk.client, "make_transport", lambda options: Collecting(options)
        )
        init_sentry(dsn="https://key@example.ingest.de.sentry.io/1", environment="test")

        assert reporting_status() == "ok"

    def test_a_closed_client_reports_disabled(self, monkeypatch):
        """Because it is: a closed client has no transport and sends nothing."""
        sentry_sdk = pytest.importorskip("sentry_sdk")

        class Collecting(sentry_sdk.Transport):
            def capture_envelope(self, envelope):
                pass

            def flush(self, *_args, **_kwargs):
                pass

            def kill(self):
                pass

        monkeypatch.setattr(
            sentry_sdk.client, "make_transport", lambda options: Collecting(options)
        )
        init_sentry(dsn="https://key@example.ingest.de.sentry.io/1", environment="test")
        sentry_sdk.get_client().close()

        assert reporting_status() == "disabled"


def test_the_compose_file_forwards_every_sentry_variable():
    """A name the compose file does not carry never reaches the container.

    This is the REDIS_URL incident, one variable along: production could not see
    a value that was correctly written into `.env`, because compose substitutes
    the names it is given and nothing else. Setting `SENTRY_DSN` on the server
    would have looked like switching reporting on and changed nothing at all.
    """
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.prod.yml").read_text()

    for name in (
        "SENTRY_DSN",
        "SENTRY_ENVIRONMENT",
        "SENTRY_RELEASE",
        "SENTRY_TRACES_SAMPLE_RATE",
    ):
        assert f"- {name}=${{{name}" in compose, f"{name} is not forwarded to the container"
