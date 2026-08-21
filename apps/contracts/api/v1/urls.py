from rest_framework.routers import DefaultRouter

from apps.contracts.api.v1 import views

router = DefaultRouter()
router.register("contracts", views.ContractViewSet, basename="contract")

urlpatterns = router.urls
