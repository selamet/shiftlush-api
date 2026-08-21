from django.urls import path

from apps.users.api.v1 import views

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
    path("invitations/accept", views.InvitationAcceptView.as_view(), name="invitation-accept"),
    path("me", views.MeView.as_view(), name="me"),
]
