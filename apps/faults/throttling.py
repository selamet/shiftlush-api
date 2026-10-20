"""The two limits on the product's only unauthenticated write.

The public fault report (M4) is throttled per sticker and per address, both at
once. Per sticker, because a token is printed on a lift anyone can photograph,
and five reports an hour is more than any real fault produces — repeated ones
merge into the first anyway. Per address, because one caller walking a list of
tokens should run out long before the firms behind them notice.

Both are scoped throttles in the sense that matters — a named rate in
DEFAULT_THROTTLE_RATES, configurable from the environment — and both report
their quota through core.throttling.ReportsQuota like every other limit. DRF's
ScopedRateThrottle takes one scope per view from the view itself, which is why
these name their own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import throttling

from core.throttling import ReportsQuota

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


class PublicFaultTokenThrottle(ReportsQuota, throttling.SimpleRateThrottle):
    scope = "public_fault_token"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        token = getattr(view, "kwargs", {}).get("qr_token", "")
        return self.cache_format % {"scope": self.scope, "ident": token}


class PublicFaultAddressThrottle(ReportsQuota, throttling.SimpleRateThrottle):
    scope = "public_fault_address"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
