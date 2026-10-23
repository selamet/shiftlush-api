from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.portal.api.v1 import views

router = DefaultRouter()
router.register("portal/elevators", views.PortalElevatorViewSet, basename="portal-elevator")
router.register("portal/visits", views.PortalVisitViewSet, basename="portal-visit")
router.register("portal/inspections", views.PortalInspectionViewSet, basename="portal-inspection")
router.register("portal/faults", views.PortalFaultViewSet, basename="portal-fault")
router.register("portal/files", views.PortalFileViewSet, basename="portal-file")
router.register("portal/reports", views.PortalReportViewSet, basename="portal-report")
# The office's side, on staff authentication.
router.register("portal-accounts", views.PortalAccountViewSet, basename="portal-account")

urlpatterns = [
    # Anonymous. Without a trailing slash, like the staff /auth endpoints.
    path("portal/auth/login", views.PortalLoginView.as_view(), name="portal-login"),
    path("portal/auth/refresh", views.PortalRefreshView.as_view(), name="portal-refresh"),
    path("portal/auth/logout", views.PortalLogoutView.as_view(), name="portal-logout"),
    path("portal/auth/accept", views.PortalAcceptView.as_view(), name="portal-accept"),
    path(
        "portal/auth/password-reset",
        views.PortalPasswordResetView.as_view(),
        name="portal-password-reset",
    ),
    path(
        "portal/auth/password-reset/confirm",
        views.PortalPasswordResetConfirmView.as_view(),
        name="portal-password-reset-confirm",
    ),
    path("portal/me", views.PortalMeView.as_view(), name="portal-me"),
    path(
        "customers/<uuid:customer_id>/portal-invitations/",
        views.PortalInvitationView.as_view(),
        name="customer-portal-invitations",
    ),
    path(
        "customers/<uuid:customer_id>/portal-accounts/",
        views.PortalAccountListView.as_view(),
        name="customer-portal-accounts",
    ),
    *router.urls,
]
