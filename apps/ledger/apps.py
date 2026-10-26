from django.apps import AppConfig


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ledger"

    def ready(self) -> None:
        # The receiver that gives every customer its account, and keeps the
        # account's name in step with the customer's. Registers itself on import.
        from apps.ledger import signals  # noqa: F401
