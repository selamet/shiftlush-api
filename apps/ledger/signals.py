"""Every customer has an account, named as the customer is.

A receiver rather than a call in the customer service: customers are created by
the viewset, the demo generator and imports alike, and every one of them must
come with an account. A rename is followed the same way, so the statement
header always shows the customer's current name.
"""

from __future__ import annotations

from typing import Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.customers.models import Customer
from apps.ledger.services import account_for_customer


@receiver(post_save, sender=Customer, dispatch_uid="ledger.customer_account")
def keep_customer_account(
    sender: Any, instance: Customer, created: bool, raw: bool = False, **kwargs: Any
) -> None:
    if raw:
        # Fixture loading writes the rows as they were; the account comes with
        # the fixture, if it is wanted.
        return

    account = account_for_customer(instance)
    if not created and account.name != instance.legal_name:
        # A rename. The account follows, so the statement header shows the
        # customer's current name.
        # Saved rather than updated in bulk, so the audit log records it.
        account.name = instance.legal_name
        account.save(update_fields=["name", "updated_at"])
