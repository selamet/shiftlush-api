"""Every customer has an account, named as the customer is.

One per currency: the TRY one is made here, with the customer, and a rename
is followed by every one of them.

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
from apps.ledger.models import Account
from apps.ledger.services import account_for_customer


@receiver(post_save, sender=Customer, dispatch_uid="ledger.customer_account")
def keep_customer_account(
    sender: Any, instance: Customer, created: bool, raw: bool = False, **kwargs: Any
) -> None:
    if raw:
        # Fixture loading writes the rows as they were; the account comes with
        # the fixture, if it is wanted.
        return

    # The TRY account comes with the customer; one in another currency comes
    # with the first invoice in it (account_for_customer).
    account_for_customer(instance)
    if created:
        return
    renamed = Account.unscoped.filter(customer=instance, is_deleted=False).exclude(
        name=instance.legal_name
    )
    for account in renamed:
        # A rename. Every account of the customer follows, so each statement
        # header shows the customer's current name.
        # Saved rather than updated in bulk, so the audit log records it.
        account.name = instance.legal_name
        account.save(update_fields=["name", "updated_at"])
