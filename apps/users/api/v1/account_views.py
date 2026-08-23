"""What a signed-in person can do to their own account.

Two things the settings screen had to leave out because nothing served them:
changing a password without going through the forgotten-password flow, and
seeing — or ending — the sessions on the account.

Everything here is scoped to `request.user` and there is no path that widens
that. An owner administering their firm can deactivate a colleague, which ends
that colleague's sessions; they still cannot *read* them. A session list says
which devices somebody carries, from which addresses, at what hours, and that
is a fact about a person rather than a fact about the company employing them.

Rules live in apps.users.services, as everywhere else in this app.
"""

from __future__ import annotations

from uuid import UUID

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.users import services
from apps.users.api.v1.account_serializers import PasswordChangeSerializer, SessionSerializer
from apps.users.api.v1.serializers import TokenResponseSerializer

# The cookie policy is defined once, next to the endpoints that mint sessions.
# A second copy of the path and the flags here is a second thing to get wrong.
from apps.users.api.v1.views import REFRESH_COOKIE, _token_response
from apps.users.models import RefreshSession
from core.client_ip import client_ip


def _current_session(request: Request) -> RefreshSession | None:
    """The session this request arrived on, read from the refresh cookie.

    The cookie is path-scoped to `/api/v1/auth`, which is why these endpoints
    live under it: moved to `/api/v1/account/...` the browser would stop sending
    the cookie, every session would report `is_current: false`, and "sign out
    everywhere else" would quietly mean everywhere.
    """
    return services.session_for_refresh_token(request.user, request.COOKIES.get(REFRESH_COOKIE))


class PasswordChangeView(APIView):
    """`POST /auth/password` — change a password from inside a live session.

    Answers with a fresh token pair, the same shape sign-in does, because that
    is what happened: every session was revoked and the caller's was re-opened.
    The client swaps the access token it holds in memory and the new refresh
    cookie replaces the old one; nothing else on the screen has to change.

    Throttled per user. Without it this is an offline-quality guessing oracle
    that happens to be online: an attacker with a stolen access token but no
    password could walk a wordlist through `current_password` and read the
    difference between 422 INVALID_CREDENTIALS and 200. The login lockout does
    not cover this path — it counts failures against a sign-in, and nobody is
    signing in here.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_change"

    @extend_schema(
        request=PasswordChangeSerializer,
        responses={200: TokenResponseSerializer},
        summary="Change the signed-in user's password",
        description=(
            "Verifies the current password, applies the same password policy as "
            "registration, and ends every other session on the account. The "
            "session making the call survives: a new refresh cookie is set and a "
            "new access token is returned. A wrong current password answers 422 "
            "INVALID_CREDENTIALS, distinct from the 400 a policy failure gives."
        ),
    )
    def post(self, request: Request) -> Response:
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        pair = services.change_password(
            user=request.user,
            current_password=serializer.validated_data["current_password"],
            new_password=serializer.validated_data["new_password"],
            current_session=_current_session(request),
            user_agent=request.headers.get("User-Agent", ""),
            ip=client_ip(request),
        )
        return _token_response(request, pair, request.user)


class SessionListView(APIView):
    """`GET /auth/sessions` — the devices this account is signed in on.

    Unpaginated, and it is the one list endpoint in this API that is. The
    collection is bounded by the number of devices one person is signed in on,
    which is single digits; an envelope with `total_pages` around it would
    describe a page that never has a second one. A person with more sessions
    than fit on a screen has a security problem, and the answer to that is the
    revoke-others button rather than pagination.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: SessionSerializer(many=True)},
        summary="List the caller's own sessions",
        description=(
            "One entry per signed-in device, not per refresh token: refresh "
            "rotation replaces the stored token roughly every fifteen minutes "
            "and every replacement stays in the same session. `id` is stable "
            "for the life of the session and is what the revoke endpoint takes. "
            "Exactly one entry has `is_current: true` — the session this request "
            "arrived on — unless the request carried no refresh cookie, in which "
            "case none does."
        ),
    )
    def get(self, request: Request) -> Response:
        current = _current_session(request)
        sessions = services.live_sessions(request.user)
        return Response(
            SessionSerializer(
                sessions,
                many=True,
                context={"current_chain_id": current.chain_id if current else None},
            ).data
        )


class SessionRevokeView(APIView):
    """`DELETE /auth/sessions/{id}` — end one session.

    404 for a session id that is not the caller's own, exactly as for one that
    never existed. A 403 would confirm the id names a real session belonging to
    somebody, which is the whole of what an attacker wants from this endpoint.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={204: None},
        summary="End one of the caller's sessions",
        description=(
            "Ends the session and every refresh token in it. The refresh token "
            "that device holds is refused from that moment, and replaying it "
            "trips the existing reuse detection. Revoking the current session is "
            "allowed and is equivalent to signing out here — the refresh cookie "
            "is cleared in the response. A session id belonging to another user, "
            "or one that has already ended, answers 404."
        ),
    )
    def delete(self, request: Request, session_id: UUID) -> Response:
        current = _current_session(request)
        ending_own = current is not None and current.chain_id == session_id

        if not services.revoke_session(user=request.user, chain_id=session_id):
            raise NotFound()

        response = Response(status=status.HTTP_204_NO_CONTENT)
        if ending_own:
            # Leaving the cookie behind would hand the browser a token that is
            # already dead, and the next refresh would look like a replay.
            response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
        return response


class SessionRevokeOthersView(APIView):
    """`POST /auth/sessions/revoke-others` — end every session but this one.

    Its own endpoint rather than a bulk DELETE, for the same reason contract
    termination is: what it does is not "delete the collection", and a client
    should not have to know that leaving one member behind is implied.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses={204: None},
        summary="End every session except the caller's own",
        description=(
            "The session this request arrives on is kept; every other session on "
            "the account ends immediately. If the request carries no usable "
            "refresh cookie there is no session to keep, and all of them end — "
            "including the caller's, who has to sign in again."
        ),
    )
    def post(self, request: Request) -> Response:
        services.revoke_other_sessions(user=request.user, keep=_current_session(request))
        return Response(status=status.HTTP_204_NO_CONTENT)
