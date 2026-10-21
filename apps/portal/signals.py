"""A deleted contact's portal account closes with it.

A receiver rather than a call in the contact viewset: a contact can be deleted
from more than one place (the API, the admin, a management command), and every
one of them must take the portal access with it.
"""

from __future__ import annotations

from typing import Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.customers.models import CustomerContact


@receiver(post_save, sender=CustomerContact, dispatch_uid="portal.close_on_contact_delete")
def close_portal_account(sender: Any, instance: CustomerContact, **kwargs: Any) -> None:
    if instance.is_deleted:
        from apps.portal.services import close_accounts_of

        close_accounts_of(instance)
