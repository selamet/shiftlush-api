from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.address.api.v1 import views

router = DefaultRouter()
router.register("provinces", views.ProvinceViewSet, basename="province")
router.register("districts", views.DistrictViewSet, basename="district")
router.register("neighborhoods", views.NeighborhoodViewSet, basename="neighborhood")

# Spelled out rather than registered on the router: it is not a collection, the
# specification writes the path as `/geocode/reverse` (8.6), and APPEND_SLASH is
# off — so a router-style trailing slash here would be a path no client asks for.
urlpatterns = [
    path("geocode/reverse", views.ReverseGeocodeView.as_view(), name="geocode-reverse"),
    *router.urls,
]
