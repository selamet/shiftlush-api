"""Serializer pieces more than one app needs.

Kept here rather than in whichever app happened to need it first: a customer
importing its address fields from `properties` would be the wrong direction, and
the second copy is where the two start to disagree.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

#: The joins `AddressReadMixin` reads. Kept beside it so that a viewset selecting
#: this tuple is selecting exactly what the mixin walks — the two drifting apart
#: costs two extra queries per row, and nothing fails to say so.
ADDRESS_JOIN = ("neighborhood", "neighborhood__district", "neighborhood__district__province")


class AddressReadMixin(serializers.Serializer[Any]):
    """The named address behind a `neighborhood` foreign key.

    Every one of these keys is nullable — a record can be entered before anyone
    knows where it is — so the fields are declared nullable too. Without that
    the contract promises a string on rows that hold nothing, a generated client
    reads the field as always present, and the screen prints an empty line
    without anything having failed.
    """

    neighborhood_name = serializers.CharField(
        source="neighborhood.name", read_only=True, allow_null=True
    )
    district_name = serializers.CharField(
        source="neighborhood.district.name", read_only=True, allow_null=True
    )
    province_name = serializers.CharField(
        source="neighborhood.district.province.name", read_only=True, allow_null=True
    )
