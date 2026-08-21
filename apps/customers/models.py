from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


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

    tax_office = models.CharField(max_length=100, blank=True)
    tax_number = models.CharField(max_length=11, blank=True)
    # Ciphertext for individual customers — see User.national_id.
    national_id = models.CharField(max_length=255, blank=True)

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
        constraints = [
            models.CheckConstraint(
                condition=models.Q(type__in=CustomerType.values), name="customer_type_valid"
            )
        ]

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
