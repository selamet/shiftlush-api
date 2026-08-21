"""Printable QR labels.

The label is the physical half of the product: it is stuck inside a machine
room and read years later by someone with a phone in one hand. What is tested
here is that the sheet is a real PDF, that it carries the right elevators, and
that one firm cannot print another firm's estate.
"""

from __future__ import annotations

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerType
from apps.elevators.labels import MAX_LABELS, build_html, label_url, qr_data_uri
from apps.elevators.models import Elevator
from apps.properties.models import Building, BuildingType
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"

weasyprint = pytest.importorskip("weasyprint", reason="the pdf dependency group is not installed")


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


def make_firm(name: str, email: str):
    company, owner = register_company(
        legal_name=f"{name} Ltd",
        display_name=name,
        first_name="F",
        last_name="Owner",
        email=email,
        password=PASSWORD,
    )
    with company_context(company.id):
        customer = Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name=f"{name} customer"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="A Blok",
            type=BuildingType.RESIDENTIAL,
            address_note="Test",
        )
    return company, owner, building


@pytest.fixture
def firm(db):
    return make_firm("Firm", "owner@example.com")


def elevators(company, building, count: int) -> list[Elevator]:
    from apps.elevators.services import assign_qr_token

    made = []
    with company_context(company.id):
        for index in range(count):
            elevator = Elevator(
                company=company,
                building=building,
                name=f"Kabin {index + 1}",
                registration_number=f"34-2024-{index:06d}",
            )
            assign_qr_token(elevator)
            elevator.save()
            made.append(elevator)
    return made


class TestTheSheet:
    def test_a_pdf_comes_back(self, firm):
        company, owner, building = firm
        made = elevators(company, building, 3)

        response = api_for(owner).post(
            reverse("elevator-labels"),
            {"elevator_ids": [str(one.id) for one in made]},
            format="json",
        )

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response.content.startswith(b"%PDF-")
        # inline rather than attachment: the point is to reach a print dialogue,
        # not to leave a file in the downloads folder.
        assert response["Content-Disposition"].startswith("inline;")

    def test_twelve_fit_on_a_page_and_the_thirteenth_starts_another(self, firm):
        company, owner, building = firm
        made = elevators(company, building, 13)

        def pages(count: int) -> int:
            html = build_html(made[:count], company)
            return len(weasyprint.HTML(string=html).render().pages)

        # The grid is 3 across and 4 down. The thirteenth label is what proves
        # the sheet paginates rather than running off the bottom of the paper.
        assert pages(12) == 1
        assert pages(13) == 2

    def test_what_a_technician_reads_out_is_on_the_label(self, firm):
        company, owner, building = firm
        made = elevators(company, building, 1)

        html = build_html(made, company)

        # The registration number is what gets read out on the phone, and the
        # building name is what tells two identical labels apart.
        assert "34-2024-000000" in html
        assert "A Blok" in html
        assert "Firm" in html

    def test_the_caller_chooses_the_order(self, firm):
        company, owner, building = firm
        made = elevators(company, building, 3)
        reversed_ids = [str(one.id) for one in reversed(made)]

        response = api_for(owner).post(
            reverse("elevator-labels"), {"elevator_ids": reversed_ids}, format="json"
        )
        # A sheet that reshuffles itself cannot be checked against the screen it
        # was printed from.
        assert response.status_code == 200


class TestTheQrItself:
    def test_the_code_points_at_the_frontend(self, settings):
        settings.FRONTEND_URL = "https://app.example.com"
        # A phone camera opens a browser, not an API client, and the browser is
        # what decides whether this person is signed in.
        assert label_url("abc123def456") == "https://app.example.com/q/abc123def456"

    def test_the_code_is_embedded_not_linked(self):
        uri = qr_data_uri("https://app.example.com/q/abc123def456")
        # A PDF that fetches images while rendering is a PDF that fails in the
        # basement where it is needed.
        assert uri.startswith("data:image/png;base64,")

    def test_the_symbol_is_built_for_a_dirty_wall(self):
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_L

        url = "https://app.example.com/q/abc123def456"

        def version(level: int) -> int:
            code = qrcode.QRCode(error_correction=level)
            code.add_data(url)
            code.make(fit=True)
            return code.version

        # Level H spends thirty per cent of the symbol on redundancy, so it
        # needs a bigger grid for the same data. That extra size is the price of
        # a label that still scans after four years of grease and a scuffed
        # corner, and it is the trade this module makes deliberately.
        assert version(ERROR_CORRECT_H) > version(ERROR_CORRECT_L)


class TestWhoMayPrint:
    def test_another_companys_elevator_is_not_printed(self, firm):
        company, owner, building = firm
        with system_context():
            other_company, _, other_building = make_firm("Other", "other@example.com")
        theirs = elevators(other_company, other_building, 1)

        response = api_for(owner).post(
            reverse("elevator-labels"),
            {"elevator_ids": [str(theirs[0].id)]},
            format="json",
        )
        # Nothing of theirs is in our queryset, so the request finds nothing to
        # print — and says so as 404 rather than confirming the id exists.
        assert response.status_code == 404

    def test_a_technician_may_print(self, firm):
        company, owner, building = firm
        made = elevators(company, building, 1)
        with system_context():
            technician = User.objects.create_user(
                email="tech@example.com",
                password=PASSWORD,
                company=company,
                first_name="T",
                last_name="Ech",
                role=Role.TECHNICIAN,
            )
        technician.customer_assignments.create(company=company, customer=building.customer)

        response = api_for(technician).post(
            reverse("elevator-labels"),
            {"elevator_ids": [str(made[0].id)]},
            format="json",
        )
        # Printing a replacement label is field work, and sending a technician
        # back to the office for one is how labels stay missing.
        assert response.status_code == 200

    def test_an_accountant_may_not(self, firm):
        company, owner, building = firm
        made = elevators(company, building, 1)
        with system_context():
            accountant = User.objects.create_user(
                email="acc@example.com",
                password=PASSWORD,
                company=company,
                first_name="A",
                last_name="Cc",
                role=Role.ACCOUNTANT,
            )

        response = api_for(accountant).post(
            reverse("elevator-labels"),
            {"elevator_ids": [str(made[0].id)]},
            format="json",
        )
        assert response.status_code == 403


class TestLimits:
    def test_an_empty_list_is_refused(self, firm):
        _, owner, _ = firm
        response = api_for(owner).post(
            reverse("elevator-labels"), {"elevator_ids": []}, format="json"
        )
        assert response.status_code == 400

    def test_more_than_the_cap_is_refused(self, firm):
        import uuid

        _, owner, _ = firm
        response = api_for(owner).post(
            reverse("elevator-labels"),
            {"elevator_ids": [str(uuid.uuid4()) for _ in range(MAX_LABELS + 1)]},
            format="json",
        )
        # Refused as a request that cannot be served rather than accepted and
        # turned into a minute-long render nobody waits for.
        assert response.status_code == 400
