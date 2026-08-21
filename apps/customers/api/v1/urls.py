from rest_framework.routers import DefaultRouter

from apps.customers.api.v1 import views

router = DefaultRouter()
router.register("customers", views.CustomerViewSet, basename="customer")
router.register("customer-contacts", views.CustomerContactViewSet, basename="customer-contact")

urlpatterns = router.urls
