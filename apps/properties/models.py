from __future__ import annotations

from django.db import models

from core.models import CompanyOwnedModel


class Complex(CompanyOwnedModel):
    """An optional grouping layer. A standalone apartment block has none."""

    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="complexes"
    )
    name = models.CharField(max_length=150)

    neighborhood = models.ForeignKey(
        "address.Neighborhood",
        on_delete=models.PROTECT,
        related_name="complexes",
        null=True,
        blank=True,
    )
    street = models.CharField(max_length=150, blank=True)
    building_number = models.CharField(max_length=20, blank=True)

    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "complex"
        ordering = ["name"]
        verbose_name_plural = "complexes"

    def __str__(self) -> str:
        return self.name


class BuildingType(models.TextChoices):
    RESIDENTIAL = "residential", "Residential"
    COMMERCIAL = "commercial", "Commercial"
    MIXED_USE = "mixed_use", "Mixed use"
    PUBLIC = "public", "Public"
    HOSPITAL = "hospital", "Hospital"
    MALL = "mall", "Mall"
    HOTEL = "hotel", "Hotel"
    SCHOOL = "school", "School"
    INDUSTRIAL = "industrial", "Industrial"


class Building(CompanyOwnedModel):
    complex = models.ForeignKey(
        Complex, on_delete=models.PROTECT, related_name="buildings", null=True, blank=True
    )
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="buildings"
    )
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=32, choices=BuildingType.choices)

    neighborhood = models.ForeignKey(
        "address.Neighborhood",
        on_delete=models.PROTECT,
        related_name="buildings",
        null=True,
        blank=True,
    )
    street = models.CharField(max_length=150, blank=True)
    building_number = models.CharField(max_length=20, blank=True)
    # Required free text, because new estates and public-housing districts are
    # simply not in the address dataset. Without it a crew can be sent to a
    # building nobody can find.
    address_note = models.TextField()

    # Never required: field staff open records without knowing the position, and
    # blocking on it would stop the record being created at all.
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    floor_count = models.SmallIntegerField(null=True, blank=True)
    unit_count = models.SmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "building"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(type__in=BuildingType.values), name="building_type_valid"
            )
        ]

    def __str__(self) -> str:
        return self.name
