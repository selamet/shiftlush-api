"""Carry M7's invoices into the ledger: the data migration, and the demo after it.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md, "Migration of existing
data". Every issued or paid draft posts its debit, and every paid draft gets a
collection of what it owes, dated the day it was marked paid, into the firm's
"Devir (carried-over payments)" cash account. The accountant moves those to the
right till with a transfer.

Everything goes through the ledger's own services (post_invoice and
record_collection, so apps.ledger.posting.post): no rule is bypassed. The
closed-through date and the opening-balance rule apply as they do to any other
movement; when this first runs no firm has a closed-through date and no side
has an opening balance, so neither can refuse it. The one adjustment is the
date: a movement cannot be dated after today, so an invoice or payment date in
the future is carried over as today.

Idempotent: an invoice already posted posts nothing (post_invoice), and a paid
invoice that collections already pay gets no second one. Running it twice
changes nothing the second time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as Date
from functools import cache
from pathlib import Path
from uuid import UUID

from django.db import transaction

from apps.billing.models import InvoiceDraft, InvoiceStatus
from apps.ledger import services
from apps.ledger.models import (
    DEFAULT_CURRENCY,
    CashAccount,
    CashAccountRole,
    CashKind,
    PaymentMethod,
)
from core.context import company_context
from core.scheduling import local_today

# The Turkish names live in core/data, with the other Turkish data.
_LABELS_PATH = Path(__file__).resolve().parents[2] / "core" / "data" / "ledger.json"


@cache
def labels() -> dict[str, str]:
    data: dict[str, str] = json.loads(_LABELS_PATH.read_text("utf-8"))
    return data


@dataclass
class CarriedOver:
    """What one run did: debits posted and collections recorded."""

    posted: int = 0
    collected: int = 0


def carried_over_cash_account(company_id: UUID, currency: str = DEFAULT_CURRENCY) -> CashAccount:
    """The firm's "Devir" cash account in `currency`, found by its role and made
    on first ask. One per currency: a payment is carried into a till in the
    invoice's own currency. The TRY one keeps the plain name; another carries
    its currency in the name, so the two read apart in a list."""
    till = CashAccount.unscoped.filter(
        company_id=company_id,
        role=CashAccountRole.CARRIED_OVER,
        currency=currency,
        is_deleted=False,
    ).first()
    if till is None:
        name = labels()["carried_over_cash_account"]
        till = CashAccount.objects.create(
            company_id=company_id,
            kind=CashKind.CASH,
            role=CashAccountRole.CARRIED_OVER,
            currency=currency,
            name=name if currency == DEFAULT_CURRENCY else f"{name} {currency}",
        )
    return till


def _not_after_today(day: Date) -> Date:
    return min(day, local_today())


def _carry_over_firm(company_id: UUID, result: CarriedOver) -> None:
    # By company explicitly: in a system context (a migration, a command) the
    # scoped manager would hand back every firm's drafts.
    drafts = InvoiceDraft.unscoped.filter(
        company_id=company_id,
        is_deleted=False,
        status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PAID],
        cancelled_at__isnull=True,
    ).order_by("invoice_date", "id")
    for draft in drafts:
        # An issued draft without a date is refused by post_invoice, loudly.
        invoice_date = draft.invoice_date and _not_after_today(draft.invoice_date)
        posted = services.post_invoice(draft, by=None, date=invoice_date)
        result.posted += posted is not None
        paid_on = draft.paid_on or draft.invoice_date
        if (
            draft.status != InvoiceStatus.PAID
            or paid_on is None
            or services.invoice_collected(draft) > 0
            or services.invoice_remaining(draft) <= 0
        ):
            continue
        services.collect_invoice(
            draft,
            date=_not_after_today(paid_on),
            by=None,
            cash_account=carried_over_cash_account(company_id, draft.currency),
            method=PaymentMethod.TRANSFER,
            note=labels()["carried_over_note"],
        )
        result.collected += 1


def carry_over_firm(company_id: UUID) -> CarriedOver:
    """One firm's drafts, as carry_over_invoices does for every firm.

    The demo refresh (core.demo_activity) calls it for the demo firm alone, so
    a draft issued or marked paid there is posted the way the migration posts.
    """
    result = CarriedOver()
    with transaction.atomic(), company_context(company_id):
        _carry_over_firm(company_id, result)
    return result


def carry_over_invoices() -> CarriedOver:
    """Post every firm's issued and paid drafts, once. See the module docstring."""
    result = CarriedOver()
    company_ids = sorted(
        set(
            InvoiceDraft.unscoped.filter(
                is_deleted=False, status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PAID]
            ).values_list("company_id", flat=True)
        )
    )
    with transaction.atomic():
        for company_id in company_ids:
            with company_context(company_id):
                _carry_over_firm(company_id, result)
    return result
