from __future__ import annotations

from django.db import models


class AuditAction(models.TextChoices):
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"


class AuditLog(models.Model):
    """Append-only record of every write.

    A bigserial key is the deliberate exception to the no-sequential-id rule:
    this table is high volume, never joined on and never addressed from the
    outside. The API does not return the id at all — listings page on
    `created_at` — so nothing leaks.

    `company_id` and `user_id` are plain UUIDs rather than foreign keys, so an
    entry survives the hard deletion of whatever it describes. An audit trail
    that can be removed by removing its subject is not an audit trail.
    """

    id = models.BigAutoField(primary_key=True)
    company_id = models.UUIDField(db_index=True)
    user_id = models.UUIDField(null=True, blank=True)

    table_name = models.CharField(max_length=60)
    record_id = models.UUIDField()
    action = models.CharField(max_length=10, choices=AuditAction.choices)

    old_values = models.JSONField(null=True, blank=True)
    new_values = models.JSONField(null=True, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company_id", "-created_at"]),
            models.Index(fields=["table_name", "record_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(action__in=AuditAction.values), name="audit_log_action_valid"
            )
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.table_name} {self.record_id}"
