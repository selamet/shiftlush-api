from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.users.api.v1 import team_views, views

router = DefaultRouter()
router.register("users", team_views.UserViewSet, basename="user")
router.register("invitations", team_views.InvitationViewSet, basename="invitation")

urlpatterns = [
    # Both public: the invitee has no account yet, which is the point of the
    # flow. They sit under /invitations rather than under /auth because that is
    # the resource they act on.
    path(
        "invitations/verify/<str:token>",
        team_views.InvitationVerifyView.as_view(),
        name="invitation-verify",
    ),
    path(
        "invitations/accept",
        views.InvitationAcceptView.as_view(),
        name="invitation-accept",
    ),
    *router.urls,
]
