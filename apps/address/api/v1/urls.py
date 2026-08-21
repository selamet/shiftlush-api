from rest_framework.routers import DefaultRouter

from apps.address.api.v1 import views

router = DefaultRouter()
router.register("provinces", views.ProvinceViewSet, basename="province")
router.register("districts", views.DistrictViewSet, basename="district")
router.register("neighborhoods", views.NeighborhoodViewSet, basename="neighborhood")

urlpatterns = router.urls
