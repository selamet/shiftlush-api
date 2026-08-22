from django.urls import path

from apps.users.api.v1 import account_views, views

app_name = "auth"

urlpatterns = [
    path("register", views.RegisterView.as_view(), name="register"),
    path("login", views.LoginView.as_view(), name="login"),
    path("refresh", views.RefreshView.as_view(), name="refresh"),
    path("logout", views.LogoutView.as_view(), name="logout"),
    path("password-reset", views.PasswordResetRequestView.as_view(), name="password-reset"),
    path(
        "password-reset/confirm",
        views.PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path("email/verify", views.EmailVerifyView.as_view(), name="email-verify"),
    path("email/resend", views.EmailVerifyResendView.as_view(), name="email-resend"),
    path("me", views.MeView.as_view(), name="me"),
    # Under /auth rather than /account: the refresh cookie is path-scoped to
    # /api/v1/auth, and these three need it to know which session is the
    # caller's own. See account_views._current_session.
    path("password", account_views.PasswordChangeView.as_view(), name="password-change"),
    path("sessions", account_views.SessionListView.as_view(), name="sessions"),
    path(
        "sessions/revoke-others",
        account_views.SessionRevokeOthersView.as_view(),
        name="sessions-revoke-others",
    ),
    path(
        "sessions/<uuid:session_id>",
        account_views.SessionRevokeView.as_view(),
        name="session-revoke",
    ),
]
