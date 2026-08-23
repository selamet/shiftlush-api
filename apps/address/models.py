"""Turkish administrative address data.

Reference data: global, not owned by any company, and never soft-deleted. It is
loaded once from a CSV by a management command and refreshed about once a year;
nothing here depends on an external service at runtime.

How much of the country these tables hold is `settings.ADDRESS_PROVINCES` rather
than anything declared here. A deployment serving one province loads one, and
the models are the same either way.
"""

from __future__ import annotations

from django.db import models


class Province(models.Model):
    # The licence-plate code is the real identifier, 1-81, so it is the key.
    id = models.SmallIntegerField(primary_key=True)
    name = models.CharField(max_length=50)
    # Search always goes through the normalised column; see core.text for why
    # lowercasing Turkish directly does not work.
    name_normalized = models.CharField(max_length=50, db_index=True)

    class Meta:
        db_table = "province"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class District(models.Model):
    id = models.IntegerField(primary_key=True)
    province = models.ForeignKey(Province, on_delete=models.PROTECT, related_name="districts")
    name = models.CharField(max_length=80)
    name_normalized = models.CharField(max_length=80, db_index=True)

    class Meta:
        db_table = "district"
        ordering = ["name"]
        indexes = [models.Index(fields=["province", "name_normalized"])]

    def __str__(self) -> str:
        return self.name


class NeighborhoodType(models.TextChoices):
    NEIGHBORHOOD = "neighborhood", "Neighborhood"
    VILLAGE = "village", "Village"
    TOWN = "town", "Town"


class Neighborhood(models.Model):
    """Around 50,000 rows, so the UI never renders the full list."""

    id = models.IntegerField(primary_key=True)
    district = models.ForeignKey(District, on_delete=models.PROTECT, related_name="neighborhoods")
    name = models.CharField(max_length=120)
    name_normalized = models.CharField(max_length=120)
    postal_code = models.CharField(max_length=5, blank=True, db_index=True)
    type = models.CharField(
        max_length=20,
        choices=NeighborhoodType.choices,
        default=NeighborhoodType.NEIGHBORHOOD,
    )

    class Meta:
        db_table = "neighborhood"
        ordering = ["name"]
        indexes = [
            models.Index(fields=["district"]),
            # Typeahead matches on substrings, which a plain b-tree cannot
            # serve; the trigram index is added in a follow-up migration
            # alongside the pg_trgm extension.
            models.Index(fields=["name_normalized"], name="neighborhood_name_norm_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(type__in=NeighborhoodType.values),
                name="neighborhood_type_valid",
            )
        ]

    def __str__(self) -> str:
        return self.name
