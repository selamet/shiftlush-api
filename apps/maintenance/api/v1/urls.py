from rest_framework.routers import DefaultRouter

from apps.maintenance.api.v1 import views

router = DefaultRouter()
router.register("maintenance-visits", views.MaintenanceVisitViewSet, basename="maintenance-visit")
router.register("planned-visits", views.PlannedVisitViewSet, basename="planned-visit")

urlpatterns = router.urls
