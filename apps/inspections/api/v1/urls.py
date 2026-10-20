from rest_framework.routers import DefaultRouter

from apps.inspections.api.v1 import views

router = DefaultRouter()
router.register("inspections", views.InspectionViewSet, basename="inspection")
router.register("nonconformities", views.NonConformityViewSet, basename="nonconformity")

urlpatterns = router.urls
