"""Carry M7's invoices into the ledger: the data migration, the daily job, and
the demo.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md, "Migration of existing
data". Every issued or paid draft posts its debit, to its customer's account in
the draft's currency, and every paid draft gets a collection of what it owes,
dated the day it was marked paid, into the firm's "Devir (carried-over
payments)" cash account in that currency. The accountant moves those to the
right till with a transfer. Every draft carried is then given the status its
collections say (services.sync_invoice_status), so a legacy issued draft for a
zero total reads paid, as one issued today does.

The migration (0008) runs it once. The old container keeps serving for a
moment after that migration commits, and an invoice it issues or marks paid
then is written the M7 way, with no posting; the daily job (apps.ledger.tasks)
runs it again and posts those. Each run is one query for the drafts still to
carry, then one transaction per draft, firm by firm: a draft that fails is
logged and counted and left as it was, whole, for the next run, and the others
go ahead.

Everything goes through the ledger's own services (post_invoice and
record_collection, so apps.ledger.posting.post): no rule is bypassed. The
closed-through date and the opening-balance rule apply as they do to any other
movement; when the migration runs no firm has a closed-through date and no side
has an opening balance, so neither can refuse it. The one adjustment is the
date: a movement cannot be dated after today, so an invoice or payment date in
the future is carried over as today.

Idempotent: an invoice already posted posts nothing (post_invoice), and a paid
invoice that collections already cover gets no second one. Running it twice
changes nothing the second time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as Date
from functools import cache
from pathlib import Path
from uuid import UUID

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet

from apps.billing.models import InvoiceDraft, InvoiceStatus
from apps.ledger import services
from apps.ledger.balances import with_collected
from apps.ledger.models import (
    DEFAULT_CURRENCY,
    CashAccount,
    CashAccountRole,
    CashKind,
    Movement,
    MovementSource,
    PaymentMethod,
)
from core.context import company_context, system_context
from core.scheduling import local_today

logger = logging.getLogger(__name__)

# The Turkish names live in core/data, with the other Turkish data.
_LABELS_PATH = Path(__file__).resolve().parents[2] / "core" / "data" / "ledger.json"


@cache
def labels() -> dict[str, str]:
    data: dict[str, str] = json.loads(_LABELS_PATH.read_text("utf-8"))
    return data


@dataclass
class CarriedOver:
    """What one run did: debits posted, collections recorded, drafts that failed."""

    posted: int = 0
    collected: int = 0
    failed: int = 0


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


def _to_carry() -> QuerySet[InvoiceDraft]:
    """The drafts a run has work for, in one query.

    - an issued or paid draft with no invoice posting, but for a paid one for
      a zero total, which owes nothing and is not a movement;
    - a paid draft that collections do not cover: marked paid the M7 way.

    Ordered by firm, so a run goes firm by firm.
    """
    posted = Movement.unscoped.filter(
        source=MovementSource.INVOICE,
        document_type=_DRAFT_TYPE,
        document_id=OuterRef("pk"),
    )
    paid = Q(status=InvoiceStatus.PAID)
    live = with_collected(
        InvoiceDraft.unscoped.filter(
            is_deleted=False,
            cancelled_at__isnull=True,
            status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PAID],
        )
    )
    return live.filter(
        (~Exists(posted) & ~(paid & Q(total=0))) | (paid & Q(total__gt=0, remaining__gt=0))
    ).order_by("company_id", "invoice_date", "id")


#: How a movement names an invoice draft (apps.ledger.posting.document_type).
_DRAFT_TYPE = f"{InvoiceDraft._meta.app_label}.{InvoiceDraft._meta.model_name}"


def _carry_over_draft(draft_id: UUID) -> tuple[bool, bool]:
    """One draft, whole or not at all: whether it posted, and whether it was
    collected. Called with its firm in context."""
    with transaction.atomic():
        # Step 1 of the lock order (services, "LOCK ORDER"), and read under the
        # lock: an old container may have changed it since the run's query.
        draft = InvoiceDraft.unscoped.select_for_update(of=("self",)).get(pk=draft_id)
        if (
            draft.is_deleted
            or draft.cancelled_at is not None
            or draft.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PAID)
        ):
            return False, False
        # An issued draft without a date is refused by post_invoice, loudly.
        invoice_date = draft.invoice_date and _not_after_today(draft.invoice_date)
        posted = services.post_invoice(draft, by=None, date=invoice_date) is not None
        collected = False
        paid_on = draft.paid_on or draft.invoice_date
        if (
            draft.status == InvoiceStatus.PAID
            and paid_on is not None
            and services.invoice_remaining(draft) > 0
        ):
            services.collect_invoice(
                draft,
                date=_not_after_today(paid_on),
                by=None,
                cash_account=carried_over_cash_account(draft.company_id, draft.currency),
                method=PaymentMethod.TRANSFER,
                note=labels()["carried_over_note"],
            )
            collected = True
        # A legacy zero-total issued draft owes nothing: paid, as one issued
        # today is (billing.issue).
        services.sync_invoice_status(draft)
    return posted, collected


def carry_over_firm(company_id: UUID) -> CarriedOver:
    """One firm's drafts, in one transaction, raising on the first failure.

    The demo refresh (core.demo_activity) calls it for the demo firm alone, so
    a draft issued or marked paid there is posted the way the migration posts.
    """
    result = CarriedOver()
    with transaction.atomic(), company_context(company_id):
        draft_ids = list(_to_carry().filter(company_id=company_id).values_list("pk", flat=True))
        for draft_id in draft_ids:
            posted, collected = _carry_over_draft(draft_id)
            result.posted += posted
            result.collected += collected
    return result


def carry_over_invoices() -> CarriedOver:
    """Carry every firm's drafts that still need it. See the module docstring.

    One query finds them; each is then carried in a transaction of its own,
    with its firm in context. A failure is logged with the draft's id, counted
    in `failed`, and rolled back to the draft as it was; the next run tries it
    again.
    """
    result = CarriedOver()
    # Across every firm: the migration and the daily job have no request to
    # take a company from. Each draft is then carried in its own firm.
    with system_context():
        rows = list(_to_carry().values_list("company_id", "pk"))
    for company_id, draft_id in rows:
        try:
            with company_context(company_id):
                posted, collected = _carry_over_draft(draft_id)
        except Exception:
            logger.exception(
                "Carrying over invoice draft %s of company %s failed; the next run retries it",
                draft_id,
                company_id,
            )
            result.failed += 1
            continue
        result.posted += posted
        result.collected += collected
    return result
