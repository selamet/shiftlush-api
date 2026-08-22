"""Where a request actually came from.

One answer for the whole project. The audit trail records an address and the
rate limit counts against one, and two implementations of the same question
drift: the day somebody puts a second proxy in front of the application, one of
them gets fixed and the other keeps writing the wrong address into a record
that exists to be trusted.

**The right-most entry, not the left-most.** `X-Forwarded-For` is a list that
each proxy appends its peer to, so it reads client, proxy, proxy — and the
client half is written by the client. A caller can send
`X-Forwarded-For: 1.2.3.4` and, if we read the left-most entry, get a fresh rate
limit bucket for every request and an audit row naming an address it invented.
Only the entries our own proxies appended mean anything, and those are at the
right-hand end.

`TRUSTED_PROXY_COUNT` says how many of them there are. With one — Caddy, which
is the deployment — the right-most entry is the address Caddy accepted the
connection from, and everything to its left is hearsay. With zero, which is
local development and the test suite, the header is ignored altogether and
`REMOTE_ADDR` is the client, because nothing is in front to have set it.

Getting the count wrong fails in one direction only, which is the safe one: too
few trusted proxies attributes the request to our own proxy, so a whole
deployment shares one bucket and the limit bites early. Too many reads a
client-supplied entry, which is the failure this module exists to prevent — so
the count is configuration with a conservative default rather than something
inferred at runtime.

DRF has `NUM_PROXIES`, which does the same arithmetic for throttling. It is not
used here because it stops at throttling: the audit trail would still need its
own copy, and this module exists precisely so there is only one.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from django.conf import settings

if TYPE_CHECKING:
    # Annotations only: this module is reached from the throttle, which DRF
    # imports while `rest_framework.views` is still being defined.
    from django.http import HttpRequest
    from rest_framework.request import Request


def client_ip(request: HttpRequest | Request) -> str | None:
    """The caller's address, or None when nothing usable can be determined."""
    remote_addr = _usable(request.META.get("REMOTE_ADDR"))
    trusted = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0))

    if trusted <= 0:
        # Nothing in front of the application, so the header — if one arrived at
        # all — was written by the caller and says nothing about the caller.
        return remote_addr

    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")]
    hops = [part for part in forwarded if part]
    if not hops:
        return remote_addr

    # The last `trusted` entries were appended by proxies we run; the first of
    # those is the one that saw the real peer. A list shorter than the count
    # means a proxy did not append, so take the oldest entry there is rather
    # than indexing off the front of the list.
    return _usable(hops[-min(trusted, len(hops))]) or remote_addr


def _usable(value: str | None) -> str | None:
    """Filter out anything that is not an IP address.

    `audit_log.ip_address` is a `GenericIPAddressField`, so a forged header
    carrying `not-an-address` would otherwise fail the insert rather than the
    request — turning a header a stranger controls into a way to break the
    record of what they did.
    """
    if not value:
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value
