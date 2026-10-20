"""Filter backends shared by every list endpoint."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
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
