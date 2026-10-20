"""In-app notifications, and who wants them by e-mail as well.

Spec: docs/superpowers/specs/2026-10-20-m2-notifications-design.md.

A notification carries no sentence. It is a `kind` and the facts the sentence
needs (`data`), captured when it was raised, so the client renders the Turkish
and an e-mail template renders its own — the same rule as error codes.
"""

from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class NotificationKind(models.TextChoices):
    """Every kind of notification, and the `data` keys each one is raised with.

    The keys are a contract between whoever raises a kind (the M2 jobs) and
    whoever renders it (the web client, and templates/email/notification/kinds/).
    Dates travel as ISO strings, because `data` is JSON.

    - ``contract_renewal_due`` — contract_number, customer_name, end_date,
      auto_renew.
    - ``contract_expired`` — contract_number, customer_name, end_date.
    - ``contract_auto_renewed`` — contract_number, customer_name, end_date,
      successor_contract_number.
    - ``contract_auto_renewal_blocked`` — contract_number, customer_name,
      end_date, successor_contract_number.
    - ``maintenance_overdue`` — elevator_name, building_name,
      next_maintenance_date.
    - ``visit_assigned`` — period ("YYYY-MM"), count: how many planned visits
      one assignment action gave the recipient in that month. Raised with
      subject_type "plan" and the recipient's own id as subject_id — the
      subject is their work list, not one row.

    Adding a kind is a backwards-compatible change to the contract; it also
    needs an entry in EMAIL_DEFAULTS below and two template partials.
    """

    CONTRACT_RENEWAL_DUE = "contract_renewal_due", "Contract renewal due"
    CONTRACT_EXPIRED = "contract_expired", "Contract expired"
    CONTRACT_AUTO_RENEWED = "contract_auto_renewed", "Contract auto-renewed"
    CONTRACT_AUTO_RENEWAL_BLOCKED = (
        "contract_auto_renewal_blocked",
        "Contract auto-renewal blocked",
    )
    MAINTENANCE_OVERDUE = "maintenance_overdue", "Maintenance overdue"
    VISIT_ASSIGNED = "visit_assigned", "Planned visits assigned"


#: Whether a kind is e-mailed to somebody who has never said otherwise. A
#: missing NotificationPreference row means "the default", so changing a value
#: here changes it for everyone who has not chosen — which is the point of a
#: default, and why a user's explicit choice is always stored as a row.
#:
#: Contract events are rare and carry money; overdue maintenance is daily
#: operational noise for most readers, who see it in the app anyway. An
#: assignment is a technician's work list changing, which is worth hearing about
#: away from the app — and it comes once per action, not once per lift.
EMAIL_DEFAULTS: dict[str, bool] = {
    NotificationKind.CONTRACT_RENEWAL_DUE: True,
    NotificationKind.CONTRACT_EXPIRED: True,
    NotificationKind.CONTRACT_AUTO_RENEWED: True,
    NotificationKind.CONTRACT_AUTO_RENEWAL_BLOCKED: True,
    NotificationKind.MAINTENANCE_OVERDUE: False,
    NotificationKind.VISIT_ASSIGNED: True,
}


class Notification(CompanyOwnedModel):
    """One thing one person was told.

    Not audited (see core.audit.EXCLUDED_MODELS): raising and reading these is
    bookkeeping, and a daily job raising one per recipient would bury the
    entries the trail exists for.
    """

    recipient = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="notifications"
    )
    kind = models.CharField(max_length=40, choices=NotificationKind.choices)
    # A plain type and id rather than a foreign key: notifications are about
    # contracts today and about invoices and inspections later, and the client
    # only needs enough to build a link.
    subject_type = models.CharField(max_length=40)
    subject_id = models.UUIDField()
    data = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    # Unique per recipient, so a job that runs twice — a retry, a redeploy at
    # the wrong minute — raises nothing new. The caller builds it from what
    # makes the event distinct, kind included, e.g.
    # "contract_renewal_due:<contract id>:<end date>".
    dedupe_key = models.CharField(max_length=255)

    class Meta:
        db_table = "notification"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=models.Q(is_deleted=False),
                name="uq_notification_dedupe_per_recipient",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=NotificationKind.values),
                name="notification_kind_valid",
            ),
        ]
        indexes = [
            # The bell asks "how many unread for me" every minute per open tab.
            models.Index(fields=["recipient", "read_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} -> {self.recipient_id}"


class NotificationPreference(CompanyOwnedModel):
    """A user's own choice about e-mail for one kind.

    In-app delivery is always on, so the only flag is e-mail. A row exists only
    once somebody has chosen; until then EMAIL_DEFAULTS answers. Audited, unlike
    Notification: turning a warning off is a decision worth being able to find.
    """

    user = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="notification_preferences"
    )
    kind = models.CharField(max_length=40, choices=NotificationKind.choices)
    email = models.BooleanField()

    class Meta:
        db_table = "notification_preference"
        ordering = ["user_id", "kind", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind"],
                condition=models.Q(is_deleted=False),
                name="uq_notification_preference_per_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=NotificationKind.values),
                name="notification_preference_kind_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} / {self.kind}: {self.email}"
