"""Address lookup.

Three endpoints that narrow in sequence, because the data is too big to hand
over whole: up to 81 provinces, around 970 districts, and roughly 50,000
neighbourhoods. How much of that a given deployment actually holds is
`ADDRESS_PROVINCES`, and none of these endpoints needs to know — they report
what the loader put in the tables.

A fourth runs those same three levels backwards. Given a point on the map it
answers with ids from the same tables, so the picker can drive the cascade from
a dropped pin instead of asking a user to find their own neighbourhood in a
list of fifty thousand.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from apps.address.geocoding import GeocoderUnavailable, reverse_geocode
from apps.address.matching import match_place
from apps.address.models import District, Neighborhood, Province
from core.exceptions import ServiceUnavailable
from core.text import normalize
from core.throttling import ScopedRateThrottle

MAX_NEIGHBORHOOD_RESULTS = 20
MIN_SEARCH_LENGTH = 2


class ProvinceSerializer(serializers.ModelSerializer[Province]):
    class Meta:
        model = Province
        fields = ["id", "name"]


class DistrictSerializer(serializers.ModelSerializer[District]):
    class Meta:
        model = District
        fields = ["id", "province_id", "name"]


class NeighborhoodSerializer(serializers.ModelSerializer[Neighborhood]):
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


class ProvinceViewSet(mixins.ListModelMixin, GenericViewSet[Province]):
    """Every province this deployment serves, unpaginated.

    Turkey has 81 and a deployment may serve as few as one, so this is a list to
    render rather than a count to rely on. Small enough to be a dropdown either
    way.
    """

    # Whatever is in the table, which is what `ADDRESS_PROVINCES` and the loader
    # already decided. Narrowing the queryset here instead would leave the other
    # eighty provinces sitting in the table — for the geocoder to match a
    # dropped pin against, and for the next `load_address_data` to widen
    # straight back into the dropdown.
    queryset = Province.objects.all()
    serializer_class = ProvinceSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None


class DistrictViewSet(mixins.ListModelMixin, GenericViewSet[District]):
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


class NeighborhoodViewSet(mixins.ListModelMixin, GenericViewSet[Neighborhood]):
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


ADDRESS_LEVELS = ["province", "district", "neighborhood"]


class GeocodeMatchSerializer(serializers.Serializer[Any]):
    """One level of the address, resolved to a row the client can select."""

    id = serializers.IntegerField(
        read_only=True,
        help_text="Feeds straight back into the province/district/neighborhood endpoints.",
    )
    name = serializers.CharField(read_only=True, help_text="The name as this API stores it.")
    confidence = serializers.FloatField(
        read_only=True,
        help_text=(
            "Trigram similarity between the provider's name and ours, 0-1. "
            "1.0 means the two agree once folded; anything at or above the "
            "server's threshold is a match, and anything below is reported as "
            "no match rather than as a low score."
        ),
    )


class ReverseGeocodeQuerySerializer(serializers.Serializer[Any]):
    """The point to look up. Both are required; neither has a default."""

    lat = serializers.FloatField(min_value=-90, max_value=90)
    lng = serializers.FloatField(min_value=-180, max_value=180)


class ReverseGeocodeSerializer(serializers.Serializer[Any]):
    """What a dropped pin resolves to.

    Ids rather than names, because the form the client is filling in is a
    cascade of selects over the address tables — a name would only send it back
    to look the row up, and would leave it guessing when the lookup found two.
    """

    province = GeocodeMatchSerializer(read_only=True, allow_null=True)
    district = GeocodeMatchSerializer(read_only=True, allow_null=True)
    neighborhood = GeocodeMatchSerializer(read_only=True, allow_null=True)
    unmatched = serializers.ListField(
        read_only=True,
        child=serializers.ChoiceField(choices=ADDRESS_LEVELS),
        help_text=(
            "Levels that could not be resolved and must be chosen by the user. "
            "Stated explicitly rather than left as three nulls: a client that "
            "reads null as 'not filled in yet' behaves differently from one "
            "that reads it as 'we looked and found nothing', and only one of "
            "those asks the user."
        ),
    )


class ReverseGeocodeView(APIView):
    """`GET /geocode/reverse?lat=&lng=` — a point, as ids from our own tables.

    The lookup goes through this API rather than from the browser so answers
    can be cached, the provider's quota stays under one roof, and changing
    provider is a change here (8.6). The throttle is the second half of that:
    caching bounds repeat lookups, but nothing bounds a client walking the
    coordinate space except a rate limit.

    A level that could not be resolved comes back null and named in
    `unmatched`. That is the whole point of the endpoint rather than a
    shortcoming of it — see `apps.address.matching`.
    """

    permission_classes = [IsAuthenticated]
    # Declaring this replaces the API-wide default rather than adding to it, so
    # the endpoint is counted once, against the provider's budget — which is
    # smaller than the general allowance and the only one that costs us
    # anything. `core.throttling`'s version of DRF's class, so this endpoint
    # reports its quota in the same headers as everything else.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "geocode"

    @extend_schema(
        parameters=[ReverseGeocodeQuerySerializer],
        responses={200: ReverseGeocodeSerializer},
        summary="Resolve a map point to province, district and neighborhood ids",
    )
    def get(self, request: Request) -> Response:
        query = ReverseGeocodeQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        try:
            place = reverse_geocode(query.validated_data["lat"], query.validated_data["lng"])
        except GeocoderUnavailable as exc:
            # 503, not 500: nothing about the request was wrong and a retry may
            # well work. The client's answer is to let the user pick the address
            # by hand, which is a different screen from "something went wrong".
            raise ServiceUnavailable() from exc

        return Response(ReverseGeocodeSerializer(match_place(place)).data)
