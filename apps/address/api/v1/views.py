"""Address lookup.

Three endpoints that narrow in sequence, because the data is too big to hand
over whole: 81 provinces, around 970 districts, and roughly 50,000
neighbourhoods.
"""

from __future__ import annotations

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import GenericViewSet, mixins

from apps.address.models import District, Neighborhood, Province
from core.text import normalize

MAX_NEIGHBORHOOD_RESULTS = 20
MIN_SEARCH_LENGTH = 2


class ProvinceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Province
        fields = ["id", "name"]


class DistrictSerializer(serializers.ModelSerializer):
    class Meta:
        model = District
        fields = ["id", "province_id", "name"]


class NeighborhoodSerializer(serializers.ModelSerializer):
    district_name = serializers.CharField(source="district.name", read_only=True)
    province_name = serializers.CharField(source="district.province.name", read_only=True)

    class Meta:
        model = Neighborhood
        fields = [
            "id",
            "district_id",
            "district_name",
            "province_name",
            "name",
            "postal_code",
            "type",
        ]


class ProvinceViewSet(mixins.ListModelMixin, GenericViewSet):
    """All 81, unpaginated. Small enough to be a dropdown."""

    queryset = Province.objects.all()
    serializer_class = ProvinceSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None


class DistrictViewSet(mixins.ListModelMixin, GenericViewSet):
    serializer_class = DistrictSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    @extend_schema(parameters=[OpenApiParameter("province", int, required=True)])
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[District]:
        province = self.request.query_params.get("province")
        if not province:
            # Returning all ~970 would let a client skip the province step and
            # render a list nobody can scan.
            return District.objects.none()
        return District.objects.filter(province_id=province)


class NeighborhoodViewSet(mixins.ListModelMixin, GenericViewSet):
    """Typeahead only. The full list is never served.

    Searching goes through `name_normalized`, which the loader fills with the
    same `normalize()` the query uses. They have to agree character for
    character — if they ever diverge, part of the table becomes unreachable and
    nothing raises.
    """

    serializer_class = NeighborhoodSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    @extend_schema(
        parameters=[
            OpenApiParameter("district", int, required=True),
            OpenApiParameter("search", str, description="At least two characters."),
        ]
    )
    def list(self, request, *args, **kwargs):  # type: ignore[no-untyped-def]
        return super().list(request, *args, **kwargs)

    def get_queryset(self) -> QuerySet[Neighborhood]:
        district = self.request.query_params.get("district")
        search = self.request.query_params.get("search", "").strip()

        if not district:
            return Neighborhood.objects.none()

        queryset = Neighborhood.objects.filter(district_id=district).select_related(
            "district", "district__province"
        )

        if search:
            if len(search) < MIN_SEARCH_LENGTH:
                return Neighborhood.objects.none()
            queryset = queryset.filter(name_normalized__contains=normalize(search))

        return queryset[:MAX_NEIGHBORHOOD_RESULTS]
