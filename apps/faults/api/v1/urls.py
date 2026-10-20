from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.faults.api.v1 import public_views, views

router = DefaultRouter()
router.register("faults", views.FaultViewSet, basename="fault")

urlpatterns = [
    path(
        "public/elevators/<str:qr_token>/faults/",
        public_views.PublicFaultReportView.as_view(),
        name="public-fault-report",
    ),
    *router.urls,
]
