from typing import Any

from django.core.management.base import BaseCommand

from core.crypto import generate_key


class Command(BaseCommand):
    help = "Print a fresh FIELD_ENCRYPTION_KEY."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write(generate_key())
        self.stderr.write(
            "Store this in a secret manager, not in a file. Losing it makes every "
            "encrypted national ID unrecoverable; leaking it makes them readable."
        )
