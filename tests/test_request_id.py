"""The correlation id, from the header to the log record.

8.9 gives `X-Request-ID` two jobs at once, and they pull against each other.
It is a correlation id, so a caller may bring its own and have one trace span
both sides. It is also the support handle: the user reads the number off their
screen and one search finds their request. The second job only holds while ids
are unique and searchable, which is not a property a caller can be trusted to
supply — so the header is honoured, but only in a shape that cannot break a log
line or a search, and only from callers that are not the browser.

Where the rest of that decision lives: `CORS_EXPOSE_HEADERS` and
`CORS_ALLOW_HEADERS` in `config/settings/base.py`, asserted from the browser's
side in `tests/test_cors.py`.
"""

from __future__ import annotations

import pytest

HEADER = "X-Request-ID"

#: A plain Django view, outside DRF, that touches no dependency. The middleware
#: runs for every request, so this is the cheapest place to watch it work.
PATH = "/health"


def id_of(response) -> str:
    return response.headers.get(HEADER, "")


class TestAnIdAlwaysComesBack:
    def test_one_is_generated_when_the_caller_sends_none(self, client):
        assert id_of(client.get(PATH))

    def test_it_is_a_different_one_each_time(self, client):
        # Two requests sharing an id would make the search that finds one find
        # both, which is the whole property the id exists to provide.
        assert id_of(client.get(PATH)) != id_of(client.get(PATH))


class TestAnIdTheCallerBrings:
    """Caddy sits in front of this process and may set the header itself.

    Honouring it is what lets the proxy's access log and the application's log
    line name the same request.
    """

    @pytest.mark.parametrize(
        "supplied",
        [
            "0123456789abcdef0123456789abcdef",  # this middleware's own shape
            "a1b2c3d4-e5f6-7890-abcd-ef0123456789",  # the dashed UUID a proxy writes
            "DEADBEEF",  # the short end of the range, upper case
        ],
    )
    def test_a_well_formed_id_is_kept(self, client, supplied):
        assert id_of(client.get(PATH, headers={"x-request-id": supplied})) == supplied


class TestAnIdTheCallerShouldNotGetAwayWith:
    """Anything that would not survive being written down.

    The id reaches a log record's `extra` and, through `core.observability`, a
    Sentry tag — an indexed, searchable field on a third-party service. What
    arrives in the header is written by the caller, so the shape is checked
    before any of that happens.
    """

    @pytest.mark.parametrize(
        ("supplied", "why"),
        [
            ("abc123\nWARNING nothing to see here", "a newline forges a second log line"),
            ("abc123\r\nSet-Cookie: x=1", "carriage return and line feed, the same trick"),
            ("f" * 65, "no ceiling means an arbitrary number of bytes per record"),
            ("abcdef", "too short to be anything but a collision"),
            ("", "an empty header is not an id"),
            ("   ", "nor is whitespace"),
            ("<script>alert(1)</script>", "it is rendered somewhere eventually"),
            ("../../etc/passwd", "a path is not an id"),
            ("zzzzzzzzzzzzzzzz", "outside the alphabet"),
        ],
    )
    def test_it_is_replaced_rather_than_echoed(self, client, supplied, why):
        returned = id_of(client.get(PATH, headers={"x-request-id": supplied}))

        assert returned != supplied, why
        assert returned, "a request still gets an id"

    def test_the_replacement_is_a_generated_id(self, client):
        returned = id_of(client.get(PATH, headers={"x-request-id": "not an id at all"}))

        assert len(returned) == 32
        int(returned, 16)  # raises if it is anything but hex

    def test_the_request_is_still_served(self, client):
        # Replaced, not rejected. The caller asked for a page, not a lecture
        # about a header, and a 400 here turns a malformed trace id into a
        # failed request.
        assert client.get(PATH, headers={"x-request-id": "\n"}).status_code == 200


class TestTheIdOnTheRequestMatchesTheOneOnTheResponse:
    """The two have to be the same string or the support lookup finds nothing.

    `core.exceptions` writes `request.request_id` into the body of a 500 and
    onto the log record; the response header is what the user can actually see.
    """

    def test_they_agree_for_a_generated_id(self, client):
        response = client.get(PATH)
        assert response.wsgi_request.request_id == id_of(response)

    def test_they_agree_for_a_rejected_one(self, client):
        response = client.get(PATH, headers={"x-request-id": "no"})
        assert response.wsgi_request.request_id == id_of(response)
