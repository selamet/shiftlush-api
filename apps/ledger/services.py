"""Ledger services: the accounts and cash accounts the rest of the system needs.

Writing movements is apps.ledger.posting's job alone; nothing here does.
"""

from __future__ import annotations

from apps.companies.models import Company
from apps.customers.models import Customer
from apps.ledger.models import Account, AccountKind, CashAccount, CashKind

#: Every firm starts with one till of this name. Plain ASCII, so it can live
#: here rather than in core/data.
DEFAULT_CASH_ACCOUNT_NAME = "Merkez kasa"


def account_for_customer(customer: Customer) -> Account:
    """The customer's account, created on first ask.

    Called by the post_save receiver (apps.ledger.signals) and by M7 when an
    invoice posts, so a customer that somehow has none still gets one rather
    than failing the invoice.
    """
    # Looked up unscoped, by the customer the caller already holds: a customer
    # saved with no company in context (a script, a command) would otherwise
    # find no account through the scoped manager and try to make a second one.
    # The create still passes through the tenant guard.
    account = Account.unscoped.filter(customer=customer, is_deleted=False).first()
    if account is None:
        account = Account.objects.create(
            company_id=customer.company_id,
            kind=AccountKind.CUSTOMER,
            customer=customer,
            name=customer.legal_name,
        )
    return account


def create_default_cash_account(company: Company) -> CashAccount:
    """The firm's first till, "Merkez kasa". Idempotent."""
    # Unscoped for the same reason as above: registration has no company in
    # context until this company exists.
    till = CashAccount.unscoped.filter(
        company=company, kind=CashKind.CASH, name=DEFAULT_CASH_ACCOUNT_NAME, is_deleted=False
    ).first()
    if till is None:
        till = CashAccount.objects.create(
            company=company, kind=CashKind.CASH, name=DEFAULT_CASH_ACCOUNT_NAME
        )
    return till
