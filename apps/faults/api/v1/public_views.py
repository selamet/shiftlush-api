"""`POST /public/elevators/{qr_token}/faults/` — the product's only unauthenticated write.

M4 spec, "Public report from the QR sticker". The threat model is the design:

- **Abuse:** two throttles (per sticker, per address), and a honeypot field
  that drops bots silently with the same answer a person gets. No captcha: it
  would be a third-party script on a page people use in an emergency.
- **Disclosure:** the answer is a six-character reference and nothing about the
  firm, the building or the lift. An unknown token is a 404 — the sticker is
  public already, so that reveals nothing, and it tells a resident with a
  damaged sticker to call instead.
- **Flooding:** repeated reports within 30 minutes merge into one fault
  (services.report_public).
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.elevators.models import Elevator
from apps.faults import services
from apps.faults.api.v1.serializers import PublicFaultReceiptSerializer, PublicFaultReportSerializer
from apps.faults.models import new_reference
from apps.faults.throttling import PublicFaultAddressThrottle, PublicFaultTokenThrottle
from core.idempotency import replay_exempt


class PublicFaultReportView(APIView):
    permission_classes = [AllowAny]
    # No authentication at all, not merely none required: a signed-in user's
    # stale token in the browser must not turn a resident's report into a 401,
    # and nothing here would use the identity anyway.
    authentication_classes: list[Any] = []
    # Replaces the default throttle rather than adding to it, so these two
    # numbers are the whole budget and the headers report the tighter one.
    throttle_classes = [PublicFaultTokenThrottle, PublicFaultAddressThrottle]

    @extend_schema(
        request=PublicFaultReportSerializer,
        responses={
            202: PublicFaultReceiptSerializer,
            404: OpenApiResponse(description="No lift carries this sticker."),
        },
        auth=[],
        summary="Report a fault from the lift's QR sticker, without an account",
        description=(
            "Answers 202 with a reference and nothing else. A report on a lift that "
            "already has an open public report from the last 30 minutes is added to "
            "it, and that report's reference is returned. `someone_trapped` makes it "
            "an emergency and `elevator_stopped` makes it high priority; there is no "
            "other way to set a priority here. Throttled per sticker and per address."
        ),
    )
    # Exempt from replay protection, and marked so. Idempotency-Key is keyed by
    # the signed-in user and their firm, and this caller has neither — there is
    # nothing to key a stored response by. Repeats are absorbed instead by the
    # 30-minute merge, which makes a retried report land on the same fault and
    # answer with the same reference.
    @replay_exempt
    def post(self, request: Request, qr_token: str) -> Response:
        elevator = (
            Elevator.unscoped.select_related("building")
            .filter(qr_token=qr_token, is_deleted=False)
            .first()
        )
        if elevator is None:
            raise NotFound()
        form = PublicFaultReportSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        if data["website"]:
            # A bot. Same status, same shape, a reference that names nothing:
            # telling it apart from a real answer would teach it what to change.
            return self._receipt(new_reference())

        fault = services.report_public(
            elevator=elevator,
            report=services.PublicReport(
                description=data["description"],
                someone_trapped=data["someone_trapped"],
                elevator_stopped=data["elevator_stopped"],
                reporter_name=data["reporter_name"].strip(),
                reporter_phone=data["reporter_phone"].strip(),
            ),
        )
        return self._receipt(fault.reference)

    @staticmethod
    def _receipt(reference: str) -> Response:
        return Response(
            PublicFaultReceiptSerializer({"reference": reference}).data,
            status=status.HTTP_202_ACCEPTED,
        )
