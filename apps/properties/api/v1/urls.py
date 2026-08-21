from rest_framework.routers import DefaultRouter

from apps.properties.api.v1 import views

router = DefaultRouter()
router.register("complexes", views.ComplexViewSet, basename="complex")
router.register("buildings", views.BuildingViewSet, basename="building")

urlpatterns = router.urls
