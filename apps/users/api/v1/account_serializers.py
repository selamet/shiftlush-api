"""Serializers for the account's own self-service: password and sessions.

Kept beside the authentication serializers rather than inside them: these two
are what a signed-in person does to their *own* account, and nothing here is
reachable without a token. `PasswordField` and `StrictSerializer` come from
there, so the password policy has one home.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.users.api.v1.serializers import PasswordField, StrictSerializer
from apps.users.models import RefreshSession, User


class PasswordChangeSerializer(StrictSerializer):
    """The current password and the wanted one.

    The new one runs through `PasswordField`, which is the same field
    registration uses — length from `settings.MIN_PASSWORD_LENGTH`, the common
    password blocklist, and the similarity check. There is no second policy
    here and there must never be one: a floor written twice is a floor that
    only moves in one of the two places.
    """

    # Nothing here checks that the new password differs from the old. That check
    # reads as an obvious courtesy and is a password oracle: field validation
    # runs before the current password is verified, so `{"current_password":
    # "x", "new_password": <guess>}` would answer "same as your current
    # password" to anybody holding a stolen access token and no password at all.
    current_password = serializers.CharField(
        write_only=True, style={"input_type": "password"}, trim_whitespace=False
    )
    new_password = PasswordField()

    def password_owner(self) -> User:
        """Who the new password must not resemble.

        `UserAttributeSimilarityValidator` is configured project-wide but is
        inert unless it is handed a user, and here there is a real one — the
        caller is signed in, so the name and address to compare against are not
        guesses assembled from the request body.

        The reset and invitation flows still validate with no owner and so run
        that validator against nothing. Both are fixable the same way: the token
        they carry identifies a user before the password is validated, so each
        could resolve one and return it here. Left alone in this change because
        those serializers are not what this change is about.
        """
        user: User = self.context["request"].user
        return user


class SessionSerializer(serializers.Serializer[Any]):
    """One signed-in device, not one refresh token.

    `id` is the chain rather than the row's primary key. The row is replaced
    every time the access token is refreshed, so a client that listed sessions,
    let the user read the screen, and then revoked a row id would hit a session
    that no longer exists roughly as often as not. The chain id is stable for
    as long as the session is.
    """

    id = serializers.UUIDField(source="chain_id", read_only=True)
    # When the person signed in. Deliberately not `created_at`: on every row
    # after the first that is the time of the last rotation, which would show a
    # month-old session as minutes old.
    signed_in_at = serializers.DateTimeField(read_only=True)
    # The last rotation, which is the last time this device used the account.
    last_used_at = serializers.DateTimeField(source="created_at", read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    # As sent at the last rotation, so a device that was renamed or upgraded
    # shows what it is now rather than what it was at sign-in.
    user_agent = serializers.CharField(read_only=True)
    ip_address = serializers.IPAddressField(read_only=True, allow_null=True)
    is_current = serializers.SerializerMethodField()

    def get_is_current(self, session: RefreshSession) -> bool:
        """Whether this is the session the request itself arrived on.

        Answered from the refresh cookie, which is the only thing that names a
        session — the access token says who the caller is, never which of their
        devices is asking.
        """
        return session.chain_id == self.context.get("current_chain_id")
