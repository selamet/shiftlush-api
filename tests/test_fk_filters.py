"""A foreign-key filter finds the firm's rows instead of refusing every id.

django-filter builds a ModelChoiceFilter's queryset when the FilterSet class is
defined — at import, when no firm is in context — and the tenant manager
answers that with `none()`. Every `?elevator=`, `?customer=`, `?building=` on
every list then failed with `invalid_choice`, including the elevator page's
maintenance history. Filters on User worked only because User is not
tenant-scoped.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from django.urls import reverse
from django_filters import ModelChoiceFilter, ModelMultipleChoiceFilter
from rest_framework.test import APIClient

from apps.users.services import issue_tokens, register_company
from core.models import CompanyOwnedModel
from tests import test_maintenance_visits
from tests.test_pagination_ordering import _routed_views

visit_fixture = test_maintenance_visits.visit_fixture


def api_for(user):
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}",
        HTTP_IDEMPOTENCY_KEY=str(uuid4()),
    )
    return client


class TestTheFiltersWork:
    def test_visits_by_elevator(self, visit_fixture):
        _, owner, elevator, contract = visit_fixture
        api_for(owner).post(
            reverse("maintenance-visit-list"),
            {
                "elevator": str(elevator.id),
                "contract": str(contract.id),
                "technician": str(owner.id),
                "visit_date": str(date.today()),
                "status": "completed",
                "signature_status": "not_present",
            },
            format="json",
        )

        response = api_for(owner).get(reverse("maintenance-visit-list"), {"elevator": elevator.id})

        assert response.status_code == 200, response.data
        assert response.data["pagination"]["total"] == 1

    def test_buildings_by_customer(self, visit_fixture):
        _, owner, elevator, _ = visit_fixture
        response = api_for(owner).get(
            reverse("building-list"), {"customer": elevator.building.customer_id}
        )
        assert response.status_code == 200, response.data
        assert response.data["pagination"]["total"] == 1

    def test_another_firms_id_is_still_nothing_to_see(self, visit_fixture):
        _, owner, *_ = visit_fixture
        other, _ = register_company(
            legal_name="Other Ltd",
            display_name="Other",
            first_name="O",
            last_name="Owner",
            email="owner@other.example.com",
            password="correct-horse-battery",
        )
        from apps.customers.models import Customer, CustomerType
        from core.context import company_context

        with company_context(other.id):
            theirs = Customer.objects.create(
                company=other, type=CustomerType.CORPORATE, legal_name="Theirs"
            )

        response = api_for(owner).get(reverse("building-list"), {"customer": theirs.id})

        # Refused exactly like an id that does not exist; never their rows.
        assert response.status_code in (200, 400)
        if response.status_code == 200:
            assert response.data["pagination"]["total"] == 0


class TestNoFilterIsBuiltAtImport:
    def test_every_foreign_key_filter_reads_its_choices_per_request(self, rf):
        offenders = []
        for view in _routed_views():
            backends = getattr(view, "filter_backends", [])
            for backend_class in backends:
                get_class = getattr(backend_class(), "get_filterset_class", None)
                if get_class is None:
                    continue
                instance = view()
                instance.request = None
                instance.format_kwarg = None
                queryset = getattr(view, "queryset", None)
                try:
                    filterset_class = get_class(instance, queryset)
                except Exception:
                    filterset_class = getattr(view, "filterset_class", None)
                if filterset_class is None:
                    continue
                for name, flt in filterset_class.base_filters.items():
                    if not isinstance(flt, ModelChoiceFilter | ModelMultipleChoiceFilter):
                        continue
                    queryset = flt.extra.get("queryset")
                    model = getattr(queryset, "model", None)
                    if model is not None and issubclass(model, CompanyOwnedModel):
                        offenders.append(f"{filterset_class.__name__}.{name}")
        assert sorted(set(offenders)) == []
