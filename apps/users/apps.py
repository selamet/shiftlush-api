from django.apps import AppConfig


class UsersConfig(AppConfig):
    name = "apps.users"
    label = "users"

    def ready(self) -> None:
        # The receiver that seeds a new company's roles. Registers itself on import.
        from apps.users import signals  # noqa: F401
