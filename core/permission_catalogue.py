"""The catalogue of resources a company role can be granted.

Code owns what exists (resources, their actions, how they are grouped and
whether they can be narrowed to assigned customers); a company owns who holds
what. The seeds below are the grants a new company's five starting roles are
created with, and reproduce the fixed table the app shipped with.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

READ = "read"
WRITE = "write"
DELETE = "delete"
RECEIVE = "receive"

SEED_KEYS: tuple[str, ...] = ("owner", "admin", "operations", "technician", "accountant")
GROUPS = ("customers", "work", "faults", "money", "company")

OWNER, ADMIN, OPS, TECH, ACCT = SEED_KEYS


@dataclass(frozen=True)
class Resource:
    key: str
    actions: frozenset[str]
    group: str
    scopable: bool
    # Seed key -> the actions that seed role holds.
    seeds: Mapping[str, frozenset[str]]


def _seeds(grants: Mapping[str, set[str]]) -> dict[str, frozenset[str]]:
    return {role: frozenset(held) for role, held in grants.items() if held}


def _resource(
    key: str,
    group: str,
    *,
    read: set[str] | None = None,
    write: set[str] | None = None,
    delete: bool = False,
    scopable: bool = False,
) -> Resource:
    """A read/write resource; `delete` adds a DELETE action seeded like WRITE.

    `read=None` means the resource has no read action (write-only), and the
    same for `write=None`.
    """
    actions = set()
    grants: dict[str, set[str]] = {role: set() for role in SEED_KEYS}
    for action, holders in ((READ, read), (WRITE, write)):
        if holders is None:
            continue
        actions.add(action)
        for role in holders:
            grants[role].add(action)
    if delete:
        actions.add(DELETE)
        for role in write or set():
            grants[role].add(DELETE)
    return Resource(key, frozenset(actions), group, scopable, _seeds(grants))


def _notify(key: str, group: str, holders: set[str]) -> Resource:
    """A resource that is only ever received: who gets a kind of notification."""
    return Resource(
        key,
        frozenset({RECEIVE}),
        group,
        False,
        _seeds({role: {RECEIVE} for role in holders}),
    )


_ALL = {OWNER, ADMIN, OPS, TECH, ACCT}
_OFFICE = {OWNER, ADMIN, OPS}

_RESOURCES: list[Resource] = [
    _resource(
        "company",
        "company",
        read=_ALL,
        # Only the owner changes company settings.
        write={OWNER},
    ),
    _resource("user", "company", read={OWNER, ADMIN}, write={OWNER, ADMIN}, delete=True),
    # The one row of 6.2 the administrator is not ticked on, while every other
    # user-management row ticks them. Listing, inviting and editing a colleague are
    # an administrator's job; ending someone's access is the owner's. Written as
    # its own resource rather than by narrowing "user"/WRITE, because narrowing
    # that would take editing away from the administrator too. There is nothing
    # to read here, so nobody may read it.
    _resource("user_deactivation", "company", write={OWNER}, delete=True),
    # Separate from "user" although the roles match today: inviting somebody is
    # not the same act as editing a colleague's record, and the day they need
    # to be granted separately this catalogue is where it happens.
    _resource("invitation", "company", read={OWNER, ADMIN}, write={OWNER, ADMIN}, delete=True),
    # Who may manage the company's roles. Not in the fixed table the app shipped
    # with; seeded on the owner and the administrator.
    _resource("role", "company", read={OWNER, ADMIN}, write={OWNER, ADMIN}, delete=True),
    _resource(
        "customer",
        "customers",
        # The technician is here but sees only assigned customers; that
        # narrowing happens in the queryset, not in this table.
        read=_ALL,
        write=_OFFICE,
        delete=True,
        scopable=True,
    ),
    _resource(
        "building",
        "customers",
        read={OWNER, ADMIN, OPS, TECH},
        write=_OFFICE,
        delete=True,
        scopable=True,
    ),
    _resource("complex", "customers", read=_OFFICE, write=_OFFICE, delete=True),
    _resource(
        "elevator",
        "customers",
        read={OWNER, ADMIN, OPS, TECH},
        write=_OFFICE,
        delete=True,
        scopable=True,
    ),
    _resource(
        "contract",
        "customers",
        read={OWNER, ADMIN, OPS, ACCT},
        write=_OFFICE,
        delete=True,
    ),
    # Not a resource of its own but a slice of the contract. Operations runs the
    # fleet, accounting runs the money, and neither needs the other's column.
    _resource(
        "contract_financials",
        "customers",
        read={OWNER, ADMIN, ACCT},
        write={OWNER, ADMIN, ACCT},
    ),
    _resource(
        "qr_label",
        "customers",
        read={OWNER, ADMIN, OPS, TECH},
        write={OWNER, ADMIN, OPS, TECH},
        scopable=True,
    ),
    _resource("audit_log", "company", read={OWNER, ADMIN}),
    _resource(
        "attachment",
        "customers",
        read={OWNER, ADMIN, OPS, TECH},
        write=_OFFICE,
        delete=True,
        scopable=True,
    ),
    # A technician records their own visit; Operations can too, on behalf of a
    # technician without an account yet. Accounting has no reason to see this
    # and Operations already covers Owner/Admin's oversight need without adding
    # them here separately.
    _resource(
        "maintenance_visit",
        "work",
        read={OWNER, ADMIN, OPS, TECH},
        write={OWNER, ADMIN, OPS, TECH},
        scopable=True,
    ),
    # A visit is not a technician's to correct after the fact - see
    # resource_by_action on MaintenanceVisitViewSet, the same mechanism the
    # QR-label endpoint uses to split "print" from "edit the elevator".
    _resource("maintenance_visit_edit", "work", write={OWNER, ADMIN}, delete=True),
    # The monthly plan (M3). A technician reads it - it is their work list,
    # narrowed in PlannedVisitViewSet to their own rows and the unassigned rows
    # of their customers - but deciding who goes where is the office's. One
    # resource rather than a read/edit pair like the visit's: every write here
    # (PATCH, bulk-assign) belongs to the same three roles. Accounting has no
    # reason to see it, as with visits.
    _resource(
        "planned_visit",
        "work",
        read={OWNER, ADMIN, OPS, TECH},
        write=_OFFICE,
        delete=True,
        scopable=True,
    ),
    # Faults (M4). Anyone who goes to lifts may report one, and the technician
    # works the ones sent to them (arrive, resolve - narrowed to the assigned
    # technician in FaultViewSet). Sending, closing, cancelling and correcting
    # are the office's, as `fault_office`. Accounting reads closed faults
    # through billing (M7), not here.
    _resource(
        "fault",
        "faults",
        read={OWNER, ADMIN, OPS, TECH},
        write={OWNER, ADMIN, OPS, TECH},
        scopable=True,
    ),
    _resource("fault_office", "faults", write=_OFFICE, delete=True),
    # Periodic inspections (M5). The office records what the inspection body
    # found and arranges the fixes; technicians read it for the lifts of their
    # customers (narrowed in the viewsets), because they do the fixing.
    # Accounting has no access.
    _resource(
        "inspection",
        "work",
        read={OWNER, ADMIN, OPS, TECH},
        write=_OFFICE,
        scopable=True,
    ),
    # Running the customer portal (M6): inviting a customer's contacts, seeing
    # their accounts, closing one. The office's job; a technician and the
    # accountant have no say in who outside the firm can sign in. Closing an
    # account is a deactivation, hence DELETE.
    _resource("portal_access", "customers", read=_OFFICE, write=_OFFICE, delete=True),
    # Invoice drafts (M7) - the first module where the accountant is the main
    # user. Operations and technicians have no part in billing. Draft lines
    # can be deleted, hence DELETE.
    _resource(
        "invoice",
        "money",
        read={OWNER, ADMIN, ACCT},
        write={OWNER, ADMIN, ACCT},
        delete=True,
    ),
    # Monthly customer reports. The accountant reads them (they are what the
    # customer was told); only the office makes them, and regenerating replaces
    # a file that was already sent, so it is narrower.
    _resource(
        "customer_report",
        "customers",
        read={OWNER, ADMIN, OPS, ACCT},
        write=_OFFICE,
    ),
    _resource("customer_report_regenerate", "customers", write={OWNER, ADMIN}),
    # The ledger (F0). Reading accounts, cash accounts, statements and the
    # summary. Operations sees a customer's balance only, through the customer
    # read, not here; the technician has no part in money until F1. Nothing is
    # written through this resource: every write is a document, below.
    _resource("ledger", "money", read={OWNER, ADMIN, ACCT}),
    # Collections and payments out. Operations takes the money at the door, so
    # it records them, but reads the ledger no further than above.
    _resource(
        "ledger_document",
        "money",
        read={OWNER, ADMIN, ACCT},
        write={OWNER, ADMIN, ACCT, OPS},
        delete=True,
    ),
    # What a collection or a payment out is recorded against: the cash
    # accounts and suppliers to pick from. Operations records those documents,
    # so it reads these lists, and nothing else of the ledger: not movements,
    # statements, documents or the summary. Nothing is written through it.
    _resource("ledger_picker", "money", read={OWNER, ADMIN, ACCT, OPS}),
    # E-mailing a statement to the customer: a POST, but it moves no money, so
    # it is for whoever reads the ledger. Operations has no part in statements.
    _resource("ledger_statement_send", "money", write={OWNER, ADMIN, ACCT}),
    # Manual entries, transfers, opening balances and the closed-through date:
    # the books themselves, the owner's and the accountant's alone. Cancelling
    # one of those documents voids it, hence DELETE.
    _resource("ledger_admin", "money", read={OWNER, ACCT}, write={OWNER, ACCT}, delete=True),
    # Who receives which notification, matching today's hard-wired recipients.
    # contracts/tasks.py
    _notify("notify_contract", "customers", {OWNER, ADMIN}),
    # faults/services.py
    _notify("notify_fault_office", "faults", {OWNER, ADMIN, OPS}),
    # inspections/services.py
    _notify("notify_inspection_office", "work", {OWNER, ADMIN, OPS}),
    # reports/services.py
    _notify("notify_report", "customers", {OWNER, ADMIN}),
    # billing/services.py
    _notify("notify_billing", "money", {OWNER, ADMIN, ACCT}),
    # maintenance/tasks.py
    _notify("notify_plan", "work", {OWNER, ADMIN, OPS}),
]

CATALOGUE: dict[str, Resource] = {r.key: r for r in _RESOURCES}
