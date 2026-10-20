"""Filter backends shared by every list endpoint."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import models
from django.db.models import QuerySet
from django_filters import FilterSet
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from rest_framework.request import Request

_PRIMARY_KEY = {"pk", "-pk", "id", "-id"}


class StableOrderingFilter(OrderingFilter):
    """`OrderingFilter` that always ends the ordering in the primary key.

    A client's `?ordering=legal_name` replaces the view's ordering wholesale,
    tiebreaker included, so two customers with the same name could swap places
    between page 1 and page 2 again (#79, #80). Appending the primary key keeps
    whatever order the client asked for and makes the tie impossible.
    """

    def get_ordering(self, request: Request, queryset: QuerySet[Any], view: Any) -> Any:
        ordering = super().get_ordering(request, queryset, view)
        if not ordering:
            return ordering
        ordering = list(ordering)
        if not _PRIMARY_KEY.intersection(ordering):
            ordering.append("pk")
        return ordering


def _choices_per_request(field: Any) -> dict[str, Any]:
    """The `extra` for a foreign-key filter: its choices, read when it is used.

    django-filter's default evaluates `remote_queryset(field)` when the
    FilterSet class is built — at import, with no firm in context — and the
    tenant manager answers that with `none()`. Every id was then an
    "invalid choice". A callable queryset is resolved per request instead,
    under the requesting firm, so another firm's id still matches nothing.
    """
    model = field.remote_field.model
    limit = field.get_limit_choices_to()

    def queryset(request: Any) -> QuerySet[Any]:
        rows: QuerySet[Any] = model._default_manager.complex_filter(limit)
        return rows

    return {"queryset": queryset}


_FK_DEFAULTS = {
    field_class: {**FilterSet.FILTER_DEFAULTS[field_class], "extra": _choices_per_request}
    for field_class in (models.ForeignKey, models.OneToOneField)
}


class TenantFilterSet(FilterSet):
    """The base for every FilterSet in the API.

    Same as django-filter's, except that foreign-key filters read their
    choices per request. Explicit filters declared on a subclass are untouched.
    """

    FILTER_DEFAULTS: ClassVar[dict[Any, Any]] = {**FilterSet.FILTER_DEFAULTS, **_FK_DEFAULTS}


class TenantFilterBackend(DjangoFilterBackend):
    """DjangoFilterBackend, building the FilterSets it generates from
    `filterset_fields` on TenantFilterSet too."""

    filterset_base = TenantFilterSet
