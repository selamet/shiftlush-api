"""Server-side pagination, the same shape on every list endpoint."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    """Page numbers rather than cursors.

    The UI shows "1-25 / 342" and lets the user jump; a cursor cannot answer
    "how many" or "page 7". The ceiling is not negotiable — without it a client
    asking for 10,000 rows turns one request into an outage.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data: Any) -> Response:
        return Response(
            OrderedDict(
                [
                    ("results", data),
                    (
                        "pagination",
                        OrderedDict(
                            [
                                ("page", self.page.number),
                                ("page_size", self.get_page_size(self.request)),
                                ("total", self.page.paginator.count),
                                ("total_pages", self.page.paginator.num_pages),
                            ]
                        ),
                    ),
                ]
            )
        )
