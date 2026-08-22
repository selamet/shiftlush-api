"""Customer rules that more than one caller has to apply the same way."""

from __future__ import annotations

from apps.customers.models import Customer, CustomerContact


def demote_other_primaries(customer: Customer, keep_pk: object | None = None) -> None:
    """Clear the primary flag on every other live contact of this customer.

    Marking a contact primary is an instruction, not a clash: the person doing
    it means "this one now", and refusing would make them unset the old one
    first for no reason they could see. The database constraint stays — it is
    what guarantees the rule when this is forgotten — but it is no longer the
    thing the user meets.

    Row by row rather than `.update()`, because a queryset update issues SQL
    directly and no audit entry would be written for the demotion. There is at
    most one row.
    """
    queryset = CustomerContact.objects.filter(customer=customer, is_primary=True)
    if keep_pk is not None:
        queryset = queryset.exclude(pk=keep_pk)
    for contact in queryset:
        contact.is_primary = False
        contact.save(update_fields=["is_primary", "updated_at"])
