"""The monthly customer report: the record, and who it is meant for."""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerContact, CustomerType
from apps.reports.models import CustomerReport, ReportDelivery
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context
from tests.identifiers import tax_number

PASSWORD = "correct-horse-battery"


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


@pytest.fixture
def firm(db):
    company, owner = register_company(
        legal_name="Firm Ltd",
        display_name="Firm",
        first_name="F",
        last_name="Owner",
        email="owner@example.com",
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company,
            type=CustomerType.CORPORATE,
            legal_name="Customer",
            tax_number=tax_number(1),
        )
    return company, owner, customer


def make_contact(company, customer, **fields) -> CustomerContact:
    with company_context(company.id):
        return CustomerContact.objects.create(
            company=company, customer=customer, full_name="Contact", **fields
        )


class TestTheRecord:
    def test_one_report_per_customer_and_month(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            CustomerReport.objects.create(
                company=company, customer=customer, period="2026-09", summary={}
            )
            with pytest.raises(IntegrityError), transaction.atomic():
                CustomerReport.objects.create(
                    company=company, customer=customer, period="2026-09", summary={}
                )

    def test_a_new_report_waits_to_be_delivered(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            report = CustomerReport.objects.create(
                company=company, customer=customer, period="2026-09", summary={}
            )
        assert report.delivery == ReportDelivery.PENDING
        assert report.recipients == []

    def test_another_firms_reports_are_invisible(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            CustomerReport.objects.create(
                company=company, customer=customer, period="2026-09", summary={}
            )
        other, _ = register_company(
            legal_name="Other Ltd",
            display_name="Other",
            first_name="O",
            last_name="Owner",
            email="other@example.com",
            password=PASSWORD,
        )
        with company_context(other.id):
            assert CustomerReport.objects.count() == 0

    def test_the_switch_is_off_and_owner_only(self, firm):
        company, owner, _ = firm
        with system_context():
            admin = User.objects.create_user(
                email="admin@example.com",
                password=PASSWORD,
                company=company,
                first_name="A",
                last_name="Admin",
                role=Role.ADMIN,
            )

        assert api_for(owner).get(reverse("company")).data["monthly_reports_enabled"] is False
        refused = api_for(admin).patch(
            reverse("company"), {"monthly_reports_enabled": True}, format="json"
        )
        assert refused.status_code == 403
        allowed = api_for(owner).patch(
            reverse("company"), {"monthly_reports_enabled": True}, format="json"
        )
        assert allowed.status_code == 200
        assert allowed.data["monthly_reports_enabled"] is True

    def test_a_contact_without_email_cannot_receive(self, firm):
        company, owner, customer = firm
        contact = make_contact(company, customer)
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]),
            {"receives_monthly_report": True},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "receives_monthly_report", "code": "MONTHLY_REPORT_NEEDS_EMAIL"}
        ]

    def test_a_contact_with_email_can_receive(self, firm):
        company, owner, customer = firm
        contact = make_contact(company, customer, email="a@example.com")
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]),
            {"receives_monthly_report": True},
            format="json",
        )
        assert response.status_code == 200, response.data
        assert response.data["receives_monthly_report"] is True

    def test_clearing_the_email_unticks(self, firm):
        company, owner, customer = firm
        contact = make_contact(
            company, customer, email="a@example.com", receives_monthly_report=True
        )
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]), {"email": ""}, format="json"
        )
        assert response.status_code == 200, response.data
        assert response.data["receives_monthly_report"] is False
        with company_context(company.id):
            contact.refresh_from_db()
        assert contact.receives_monthly_report is False

    def test_asking_to_tick_while_clearing_the_email_is_refused(self, firm):
        company, owner, customer = firm
        contact = make_contact(
            company, customer, email="a@example.com", receives_monthly_report=True
        )
        response = api_for(owner).patch(
            reverse("customer-contact-detail", args=[contact.pk]),
            {"email": "", "receives_monthly_report": True},
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "receives_monthly_report", "code": "MONTHLY_REPORT_NEEDS_EMAIL"}
        ]
        with company_context(company.id):
            contact.refresh_from_db()
        assert contact.email == "a@example.com"
        assert contact.receives_monthly_report is True

    def test_the_migration_ticks_the_primary_contact_with_email(self, firm):
        company, _, customer = firm
        with company_context(company.id):
            other = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Other"
            )
        primary = make_contact(company, customer, email="p@example.com", is_primary=True)
        bare_primary = make_contact(company, other, is_primary=True)
        secondary = make_contact(company, customer, email="s@example.com")

        migration = importlib.import_module("apps.customers.migrations.0007_tick_primary_contacts")
        with system_context():
            migration.tick_primary_contacts(django_apps, None)
            ticked = {
                c.pk: c.receives_monthly_report
                for c in CustomerContact.objects.filter(
                    pk__in=[primary.pk, bare_primary.pk, secondary.pk]
                )
            }
        assert ticked == {primary.pk: True, bare_primary.pk: False, secondary.pk: False}
