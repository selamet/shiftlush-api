from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    label = "core"

    def ready(self) -> None:
        # Connected here rather than at import time: signals need the model
        # registry to be populated, and importing core.audit earlier would pull
        # in apps that are still loading.
        from core.audit import connect

        connect()
