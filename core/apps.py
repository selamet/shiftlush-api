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

        # Imported for its side effect: the module registers the OpenAPI
        # extension for our authentication class. Without it the generator
        # cannot resolve the class and every operation silently loses its
        # security block.
        from core import spectacular  # noqa: F401
