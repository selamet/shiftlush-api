from django.apps import AppConfig


class PortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.portal"

    def ready(self) -> None:
        # The schema extension for the portal's own bearer scheme, and the
        # receiver that closes an account when its contact is deleted. Both
        # register themselves on import.
        from apps.portal import schema, signals  # noqa: F401
