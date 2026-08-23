"""Reading the parts of a request that the permission layer already vetted."""

from __future__ import annotations

from uuid import UUID

from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.request import Request

from apps.companies.models import Company
from apps.users.models import User


def authenticated_user(request: Request) -> User:
    """The signed-in user behind an endpoint that requires one.

    DRF types ``request.user`` as "a user *or* AnonymousUser", because a view is
    free to be public. Every caller of this function sits behind a permission
    class that has already rejected the anonymous case, so the union is noise at
    the call site — but reading ``company_id`` off an AnonymousUser would be an
    AttributeError, which reaches the client as a 500.

    The check is therefore not a formality. It costs an isinstance and turns the
    case that should be impossible into the status code it deserves.
    """
    user = request.user
    if not isinstance(user, User):
        raise NotAuthenticated
    return user


def authenticated_company_id(request: Request) -> UUID:
    """The company the signed-in user belongs to.

    ``User.company`` is nullable, and for one reason: a superuser created with
    ``createsuperuser`` has no firm. Such an account is refused by every
    company-scoped endpoint's permission class, so this is the same 403 arriving
    a few frames earlier — and, unlike an AttributeError on ``company_id``, it
    is the answer the client should have been given.
    """
    company_id = authenticated_user(request).company_id
    if company_id is None:
        raise PermissionDenied
    return company_id


def authenticated_company(request: Request) -> Company:
    """The firm itself, for the two places that need more than its id.

    Same rule as ``authenticated_company_id``, and the row is already loaded:
    the JWT authenticator joins it in, so this costs no query.
    """
    company = authenticated_user(request).company
    if company is None:
        raise PermissionDenied
    return company
