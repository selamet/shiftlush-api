from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class ObjectType(models.TextChoices):
    ELEVATOR = "elevator", "Elevator"
    BUILDING = "building", "Building"
    CONTRACT = "contract", "Contract"
    CUSTOMER = "customer", "Customer"
    COMPANY = "company", "Company"
    USER = "user", "User"


class AttachmentCategory(models.TextChoices):
    PHOTO = "photo", "Photo"
    CE_CERTIFICATE = "ce_certificate", "CE certificate"
    DECLARATION_OF_CONFORMITY = "declaration_of_conformity", "Declaration of conformity"
    PERMIT = "permit", "Permit"
    SIGNED_CONTRACT = "signed_contract", "Signed contract"
    INSPECTION_REPORT = "inspection_report", "Inspection report"
    LOGO = "logo", "Logo"
    OTHER = "other", "Other"


class StorageBackend(models.TextChoices):
    R2 = "r2", "Cloudflare R2"
    LOCAL = "local", "Local (development)"
    TR_PROVIDER = "tr_provider", "Turkey-resident provider"


class Attachment(CompanyOwnedModel):
    """A file kept in object storage.

    Bytes never enter this table and never pass through the application server:
    the client uploads straight to storage with a signed URL and then confirms
    the record here.
    """

    # A plain type/id pair rather than a GenericForeignKey: the contenttypes
    # join buys nothing here and makes the tenant filter harder to reason about.
    # Validity of the pair is checked in the service layer.
    object_type = models.CharField(max_length=20, choices=ObjectType.choices)
    object_id = models.UUIDField()

    category = models.CharField(max_length=32, choices=AttachmentCategory.choices)
    original_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size_bytes = models.IntegerField()

    # The user's filename is never used as the key; a key is generated so a
    # crafted name cannot influence where the object lands.
    storage_key = models.CharField(max_length=500)
    # Present from day one even though only one value is used today. It is what
    # lets personal-data categories move to a Turkey-resident provider later
    # without a migration — see the KVKK decision in the specification.
    storage_backend = models.CharField(
        max_length=20, choices=StorageBackend.choices, default=StorageBackend.R2
    )

    uploaded_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, related_name="+", null=True, blank=True
    )

    class Meta:
        db_table = "attachment"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["object_type", "object_id"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(object_type__in=ObjectType.values),
                name="attachment_object_type_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=AttachmentCategory.values),
                name="attachment_category_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(size_bytes__gt=0) & models.Q(size_bytes__lte=10 * 1024 * 1024),
                name="attachment_size_within_limit",
            ),
        ]

    def __str__(self) -> str:
        return self.original_filename
