"""The audit trail.

One central table rather than a history table per model. The alternative —
django-simple-history and friends — doubles the schema and leaves you querying
forty tables to answer "what happened to this record". One table with a JSONB
diff answers it with one query.

A caveat that has to be respected everywhere else: signals do not fire for
`bulk_create`, `bulk_update` or `QuerySet.update()`. Business code therefore
does not use them. The one place that does is the address loader, and that is
correct — reference data is not a business record, and auditing 50,000
unchanged rows every year would bury the entries that matter.
"""

from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models.signals import post_delete, post_save

from apps.audit.models import AuditAction, AuditLog
from core.context import get_current_actor

# Never written to the trail, in any table. An audit log that records password
# hashes and national IDs turns the safety net into the largest single leak in
# the system.
MASKED_FIELDS = frozenset(
    {
        "password",
        "national_id",
        "token_hash",
        "qr_token",
        "request_hash",
        "response_body",
    }
)

MASK = "***"

# Bookkeeping, not history. `updated_at` moves on every save, so leaving it in
# the diff would file an entry for saves that changed nothing and make the trail
# mostly noise — and the audit row carries its own timestamp anyway.
IGNORED_IN_DIFF = frozenset({"updated_at"})

# Infrastructure, not business records. Auditing these would fill the table with
# rows nobody reads, and the audit table auditing itself is a loop.
EXCLUDED_MODELS = frozenset(
    {
        "audit.AuditLog",
        "core.IdempotencyKey",
        "users.RefreshSession",
        "users.OneTimeToken",
        "address.Province",
        "address.District",
        "address.Neighborhood",
        "contenttypes.ContentType",
        "sessions.Session",
        "admin.LogEntry",
        "auth.Permission",
        "auth.Group",
    }
)


class AuditedModel(models.Model):
    """Remembers the values a row was loaded with.

    Without this the trail can say a row changed but not what it changed from,
    which is the half people actually need when reconstructing an incident.
    """

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db: Any, field_names: Any, values: Any, *args: Any, **kwargs: Any) -> Any:
        # Extra positional and keyword arguments are passed straight through:
        # Django has added parameters to this hook before (6.1 added
        # fetch_mode) and pinning the signature turns a minor upgrade into a
        # TypeError on every query.
        instance = super().from_db(db, field_names, values, *args, **kwargs)
        instance._loaded_values = dict(zip(field_names, values, strict=False))
        return instance


def _serialise(value: Any) -> Any:
    """Make a field value safe to put in JSONB."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _snapshot(instance: models.Model) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field in instance._meta.concrete_fields:  # type: ignore[attr-defined]
        name = field.attname
        if field.name in MASKED_FIELDS or name in MASKED_FIELDS:
            data[field.name] = MASK
            continue
        data[field.name] = _serialise(getattr(instance, name, None))
    return data


def _changed(instance: models.Model) -> tuple[dict[str, Any], dict[str, Any]]:
    """Old and new values, limited to the fields that actually moved."""
    loaded: dict[str, Any] = getattr(instance, "_loaded_values", {})
    if not loaded:
        return {}, _snapshot(instance)

    old: dict[str, Any] = {}
    new: dict[str, Any] = {}
    for field in instance._meta.concrete_fields:  # type: ignore[attr-defined]
        name = field.attname
        if name not in loaded or field.name in IGNORED_IN_DIFF:
            continue
        before, after = loaded[name], getattr(instance, name, None)
        if before == after:
            continue
        if field.name in MASKED_FIELDS or name in MASKED_FIELDS:
            old[field.name] = MASK
            new[field.name] = MASK
        else:
            old[field.name] = _serialise(before)
            new[field.name] = _serialise(after)
    return old, new


def _label(instance: models.Model) -> str:
    return f"{instance._meta.app_label}.{instance._meta.object_name}"


def _write(instance: models.Model, action: str, old: dict[str, Any], new: dict[str, Any]) -> None:
    company_id = getattr(instance, "company_id", None)
    if company_id is None:
        # Nothing to file it under. The company row itself is the one case, and
        # it uses its own id.
        company_id = instance.pk if _label(instance) == "companies.Company" else None
    if company_id is None:
        return

    actor = get_current_actor()
    AuditLog.objects.create(
        company_id=company_id,
        user_id=actor.user_id,
        table_name=instance._meta.db_table,
        record_id=instance.pk,
        action=action,
        old_values=old or None,
        new_values=new or None,
        ip_address=actor.ip_address,
        user_agent=actor.user_agent[:255],
    )


def on_save(
    sender: type[models.Model], instance: models.Model, created: bool, **kwargs: Any
) -> None:
    if _label(instance) in EXCLUDED_MODELS:
        return

    # A soft delete is an UPDATE at the database level, but it is a deletion to
    # anyone reading the trail later, so it is recorded as one.
    if not created and getattr(instance, "is_deleted", False):
        loaded = getattr(instance, "_loaded_values", {})
        if not loaded.get("is_deleted", False):
            _write(instance, AuditAction.DELETE, {}, {})
            return

    if created:
        _write(instance, AuditAction.CREATE, {}, _snapshot(instance))
        return

    old, new = _changed(instance)
    if not new:
        # Saving without changing anything is not an event.
        return
    _write(instance, AuditAction.UPDATE, old, new)


def on_delete(sender: type[models.Model], instance: models.Model, **kwargs: Any) -> None:
    # Only reached by hard_delete, which management commands use. Ordinary
    # deletion is soft and handled above.
    if _label(instance) in EXCLUDED_MODELS:
        return
    _write(instance, AuditAction.DELETE, _snapshot(instance), {})


def connect() -> None:
    post_save.connect(on_save, dispatch_uid="core.audit.on_save")
    post_delete.connect(on_delete, dispatch_uid="core.audit.on_delete")
