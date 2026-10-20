from __future__ import annotations

from django.tasks import task

from apps.attachments.services import purge_detached_objects
from core.context import system_context


@task
def purge_attachments() -> int:
    # Across every company, as the management command does: there is no request
    # here to take a tenant from.
    with system_context():
        return purge_detached_objects()
