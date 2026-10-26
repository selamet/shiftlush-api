from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.ledger.api.v1 import views

router = DefaultRouter()
router.register("accounts", views.AccountViewSet, basename="ledger-account")
router.register("collections", views.CollectionViewSet, basename="ledger-collection")
router.register("payments-out", views.PaymentOutViewSet, basename="ledger-payment-out")
router.register("manual-entries", views.ManualEntryViewSet, basename="ledger-manual-entry")
router.register("transfers", views.TransferViewSet, basename="ledger-transfer")
router.register("opening-balances", views.OpeningBalanceViewSet, basename="ledger-opening")
router.register("cash-accounts", views.CashAccountViewSet, basename="ledger-cash-account")
router.register("suppliers", views.SupplierViewSet, basename="ledger-supplier")
router.register("finance", views.FinanceViewSet, basename="ledger-finance")

urlpatterns = [
    # Singular, and no id: the firm's own ledger settings.
    path("settings/", views.LedgerSettingsView.as_view(), name="ledger-settings"),
    *router.urls,
]
