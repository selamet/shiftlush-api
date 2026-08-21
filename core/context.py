"""Per-request tenant context.

The active company is held in a :class:`~contextvars.ContextVar`, never in a
thread-local. A thread-local survives today's synchronous request cycle but
breaks the moment any part of the stack becomes async — and it breaks silently,
by leaking one company's context into another request. That failure mode is a
data breach, so the safe primitive is used from the start.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_current_company_id: ContextVar[uuid.UUID | None] = ContextVar("current_company_id", default=None)

# Set only inside `system_context`. Kept separate from "no company" so the two
# cases can never be confused: an unset context is a bug, a system context is a
# deliberate, narrow exemption.
_system_mode: ContextVar[bool] = ContextVar("system_mode", default=False)


@dataclass(frozen=True)
class RequestActor:
    """Who made the change, and from where.

    Carried in a contextvar rather than threaded through every service call:
    the audit trail needs it at the very bottom of the stack, in a signal
    handler, and passing it down by hand would mean every function in between
    grows a parameter it does not otherwise use.
    """

    user_id: uuid.UUID | None = None
    ip_address: str | None = None
    user_agent: str = ""


# The default is a frozen dataclass and therefore safe to share, but a literal
# default on a ContextVar is a trap in general — one mutable instance shared by
# every request — so it is built once here and never handed out for mutation.
_ANONYMOUS = RequestActor()
_current_actor: ContextVar[RequestActor] = ContextVar("current_actor", default=_ANONYMOUS)


def get_current_actor() -> RequestActor:
    return _current_actor.get()


@contextmanager
def actor_context(actor: RequestActor) -> Iterator[None]:
    token = _current_actor.set(actor)
    try:
        yield
    finally:
        _current_actor.reset(token)


class TenantContextError(RuntimeError):
    """Raised when tenant-scoped work is attempted with no company in context."""


def get_current_company_id() -> uuid.UUID | None:
    return _current_company_id.get()


def in_system_context() -> bool:
    return _system_mode.get()


def require_current_company_id() -> uuid.UUID:
    company_id = _current_company_id.get()
    if company_id is None:
        raise TenantContextError(
            "No company in context. Requests carry it from the JWT; background "
            "work must open a company_context() explicitly."
        )
    return company_id


@contextmanager
def company_context(company_id: uuid.UUID | None) -> Iterator[None]:
    """Bind a company for the duration of the block."""
    token = _current_company_id.set(company_id)
    try:
        yield
    finally:
        _current_company_id.reset(token)


@contextmanager
def system_context() -> Iterator[None]:
    """Run without a tenant filter.

    Needed because a handful of flows legitimately have no company yet: company
    registration and invitation acceptance both create the very rows the filter
    would key on, and password reset and QR token resolution run before anyone
    is authenticated.

    Call this only from ``services.py``, only for those flows, and always with a
    comment saying which one. It is the single place where tenant isolation is
    deliberately off, which makes it the most dangerous code in the project —
    every caller needs a cross-tenant test of its own.
    """
    company_token = _current_company_id.set(None)
    system_token = _system_mode.set(True)
    try:
        yield
    finally:
        _system_mode.reset(system_token)
        _current_company_id.reset(company_token)
