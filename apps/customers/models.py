from __future__ import annotations

from typing import Any

from django.db import models

from core.crypto import fingerprint
from core.fields import EncryptedCharField
from core.models import CompanyOwnedModel
from core.text import normalize


class CustomerType(models.TextChoices):
    COMPLEX_MANAGEMENT = "complex_management", "Complex management"
    BUILDING_MANAGEMENT = "building_management", "Building management"
    CORPORATE = "corporate", "Corporate"
    PUBLIC = "public", "Public"
    INDIVIDUAL = "individual", "Individual"


class Customer(CompanyOwnedModel):
    """Who is billed.

    Separate from the building on purpose: one complex management company can be
    the customer for eight buildings, and the contract and the invoice attach
    here, not to any one of them.
    """

    type = models.CharField(max_length=32, choices=CustomerType.choices)
    legal_name = models.CharField(max_length=200)
    # Turkish folded once, on save, so that search and storage cannot disagree.
    # Lowercasing the dotted capital I yields two code points, which then match
    # no stored plain i — so searching the name column directly returns nothing
    # and raises nothing. See core.text.
    legal_name_normalized = models.CharField(max_length=200, default="", editable=False)

    tax_office = models.CharField(max_length=100, blank=True)
    tax_number = models.CharField(max_length=11, blank=True)
    # Ciphertext for individual customers — see User.national_id.
    national_id = EncryptedCharField(max_length=255, blank=True)
    # A blind index over the same value. The ciphertext carries a fresh nonce
    # per write, so two rows holding one national ID look nothing alike and no
    # unique constraint can be written against the column itself.
    national_id_fingerprint = models.CharField(
        max_length=64, default="", blank=True, editable=False
    )

    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(max_length=150, blank=True)

    neighborhood = models.ForeignKey(
        "address.Neighborhood",
        on_delete=models.PROTECT,
        related_name="customers",
        null=True,
        blank=True,
    )
    street = models.CharField(max_length=150, blank=True)
    building_number = models.CharField(max_length=20, blank=True)
    unit_number = models.CharField(max_length=20, blank=True)

    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "customer"
        ordering = ["legal_name"]
        indexes = [
            models.Index(fields=["company", "legal_name_normalized"], name="customer_name_norm_idx")
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(type__in=CustomerType.values), name="customer_type_valid"
            ),
            # Both carry `is_deleted=False`, so a soft-deleted customer releases
            # its number rather than holding it against a record nobody can see.
            models.UniqueConstraint(
                fields=["company", "tax_number"],
                condition=models.Q(is_deleted=False) & ~models.Q(tax_number=""),
                name="uq_customer_tax_number_active",
            ),
            models.UniqueConstraint(
                fields=["company", "national_id_fingerprint"],
                condition=models.Q(is_deleted=False) & ~models.Q(national_id_fingerprint=""),
                name="uq_customer_national_id_active",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        # Derived here rather than in the serializer: a customer created by a
        # management command, a fixture or a test has to be searchable and has
        # to collide with its duplicate exactly as one created over HTTP does.
        self.legal_name_normalized = normalize(self.legal_name)
        self.national_id_fingerprint = fingerprint(self.national_id or "")
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            # A partial save that touches the source has to carry the derived
            # column with it, or the two drift and the drift is silent.
            fields = set(update_fields)
            if "legal_name" in fields:
                fields.add("legal_name_normalized")
            if "national_id" in fields:
                fields.add("national_id_fingerprint")
            kwargs["update_fields"] = fields
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.legal_name


class ContactRole(models.TextChoices):
    MANAGER = "manager", "Manager"
    AUDITOR = "auditor", "Auditor"
    CARETAKER = "caretaker", "Caretaker"
    TECHNICAL_LEAD = "technical_lead", "Technical lead"
    ACCOUNTING = "accounting", "Accounting"
    OTHER = "other", "Other"


class CustomerContact(CompanyOwnedModel):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="contacts")
    full_name = models.CharField(max_length=120)
    role = models.CharField(max_length=32, choices=ContactRole.choices, default=ContactRole.OTHER)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(max_length=150, blank=True)
    is_primary = models.BooleanField(default=False)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "customer_contact"
        ordering = ["-is_primary", "full_name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=ContactRole.values),
                name="customer_contact_role_valid",
            ),
            # At most one primary per customer, and only among live rows — a
            # deleted contact must not hold the slot.
            models.UniqueConstraint(
                fields=["customer"],
                condition=models.Q(is_primary=True, is_deleted=False),
                name="uq_customer_primary_contact",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name
