from rest_framework.routers import DefaultRouter

from apps.billing.api.v1 import views

router = DefaultRouter()
router.register("invoice-drafts", views.InvoiceDraftViewSet, basename="invoice-draft")
router.register("invoice-lines", views.InvoiceLineViewSet, basename="invoice-line")

urlpatterns = router.urls
