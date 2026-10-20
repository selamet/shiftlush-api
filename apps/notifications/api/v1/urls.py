from rest_framework.routers import DefaultRouter

from apps.notifications.api.v1 import views

router = DefaultRouter()
router.register("notifications", views.NotificationViewSet, basename="notification")

urlpatterns = router.urls
