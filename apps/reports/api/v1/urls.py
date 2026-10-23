from rest_framework.routers import DefaultRouter

from apps.reports.api.v1 import views

router = DefaultRouter()
router.register("customer-reports", views.CustomerReportViewSet, basename="customer-report")

urlpatterns = router.urls
