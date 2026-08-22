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

    def get_paginated_response_schema(self, schema: Any) -> Any:
        """Declare the envelope this class actually returns.

        DRF's default schema describes `count`/`next`/`previous`, and overriding
        `get_paginated_response` does not change it — the schema generator reads
        this method and never runs the other one. Left inherited, every list
        endpoint in the contract described a response the server has never sent,
        and a client generated from it read `count` as undefined: the types
        compile, the build passes, and the row counter silently shows zero.
        """
        return {
            "type": "object",
            "required": ["results", "pagination"],
            "properties": {
                "results": schema,
                "pagination": {
                    "type": "object",
                    "required": ["page", "page_size", "total", "total_pages"],
                    "properties": {
                        "page": {"type": "integer", "example": 1},
                        "page_size": {"type": "integer", "example": 25},
                        "total": {"type": "integer", "example": 342},
                        "total_pages": {"type": "integer", "example": 14},
                    },
                },
            },
        }

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
