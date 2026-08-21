from rest_framework.routers import DefaultRouter

from apps.elevators.api.v1 import views

router = DefaultRouter()
router.register("elevators", views.ElevatorViewSet, basename="elevator")

urlpatterns = router.urls
