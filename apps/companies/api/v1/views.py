"""The company endpoint.

A singleton: there is no `/companies/{id}`, because the only company a request
can address is its own and the id is already in the token. Exposing a collection
would invite a client to try another id, and the only correct answer to that is
404 — so the route that would produce it does not exist.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.companies.models import Company
from core.context import require_current_company_id
from core.permissions import RolePermission
from core.validators import validate_tax_number


class CompanySerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = [
            "id",
            "legal_name",
            "display_name",
            "tax_office",
            "tax_number",
            "mersis_number",
            "trade_registry_number",
            "neighborhood",
            "street",
            "building_number",
            "unit_number",
            "phone",
            "email",
            "website",
            "logo",
            "logo_url",
            "is_active",
            "created_at",
        ]
        read_only_fields = ["id", "logo_url", "is_active", "created_at"]

    def validate_tax_number(self, value: str) -> str:
        return validate_tax_number(value) if value else value

    def get_logo_url(self, company: Company) -> str:
        # A signed URL rather than a stored one: the bucket is private, and a
        # URL with an expiry cannot be pasted into a public page by accident.
        if company.logo_id is None or not company.logo.storage_key:
            return ""
        from apps.attachments.services import download_url

        return download_url(company.logo)

    def validate_logo(self, value: Any) -> Any:
        if value is None:
            return value
        # The foreign key is a convenience on top of the polymorphic relation
        # and has to agree with it, or a company could display another
        # company's file.
        if value.company_id != require_current_company_id():
            raise serializers.ValidationError("Unknown attachment.")
        return value


class CompanyView(APIView):
    resource = "company"
    permission_classes = [IsAuthenticated, RolePermission]

    def _company(self) -> Company:
        # Not `get_object_or_404`: outside a company context this must fail
        # rather than pick one, and require_current_company_id enforces that.
        return Company.objects.get(pk=require_current_company_id())

    @extend_schema(responses={200: CompanySerializer})
    def get(self, request: Request) -> Response:
        return Response(CompanySerializer(self._company()).data)

    @extend_schema(request=CompanySerializer, responses={200: CompanySerializer})
    def patch(self, request: Request) -> Response:
        serializer = CompanySerializer(self._company(), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)
