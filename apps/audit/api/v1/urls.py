from rest_framework.routers import DefaultRouter

from apps.audit.api.v1 import views

router = DefaultRouter()
router.register("audit-logs", views.AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls
