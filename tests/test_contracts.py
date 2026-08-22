"""Contracts, their state transitions, and the constraint that matters most.

An elevator under two open contracts would be billed twice and scheduled twice.
The database refuses it; these tests prove the API surfaces that as an answer a
user can act on rather than as an integrity error.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.contracts.models import Contract, ContractElevator, ContractStatus, PricingType, Scope
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator, ElevatorStatus
from apps.elevators.services import assign_qr_token
from apps.properties.models import Building, BuildingType
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"
TODAY = date.today()


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
            company=company, type=CustomerType.COMPLEX_MANAGEMENT, legal_name="Site Management"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="A Blok",
            type=BuildingType.RESIDENTIAL,
            address_note="Behind the market",
        )
        elevators = []
        for index in range(3):
            elevator = Elevator(
                company=company,
                building=building,
                registration_number=f"34-2020-00000{index}",
                name=f"Elevator {index}",
            )
            assign_qr_token(elevator)
            elevator.save()
            elevators.append(elevator)
    return company, owner, customer, building, elevators


def make_contract(client: APIClient, customer_id, **overrides) -> dict:
    payload = {
        "customer": str(customer_id),
        "scope": Scope.MAINTENANCE_AND_REPAIR,
        "start_date": str(TODAY),
        "end_date": str(TODAY + timedelta(days=365)),
        "pricing_type": PricingType.PER_ELEVATOR,
        "monthly_fee": "4750.00",
        **overrides,
    }
    return client.post(reverse("contract-list"), payload, format="json").data


class TestNumbering:
    def test_number_is_assigned_per_company_and_year(self, firm):
        _, owner, customer, _, _ = firm
        client = api_for(owner)
        first = make_contract(client, customer.id)
        second = make_contract(client, customer.id)
        assert first["contract_number"] == f"{TODAY.year}-0001"
        assert second["contract_number"] == f"{TODAY.year}-0002"


class TestOneOpenContractPerElevator:
    def test_an_elevator_cannot_join_two_open_contracts(self, firm):
        _, owner, customer, _, elevators = firm
        client = api_for(owner)
        first = make_contract(client, customer.id)
        second = make_contract(client, customer.id)

        assert (
            client.post(
                reverse("contract-add-elevators", args=[first["id"]]),
                {"elevator_ids": [str(elevators[0].pk)]},
                format="json",
            ).status_code
            == 200
        )

        response = client.post(
            reverse("contract-add-elevators", args=[second["id"]]),
            {"elevator_ids": [str(elevators[0].pk)]},
            format="json",
        )
        # Named, not an integrity error: the user has to be told which elevator
        # is already covered so they can go and free it.
        assert response.status_code == 422
        assert response.data["error"]["code"] == "ELEVATOR_ALREADY_CONTRACTED"

    def test_it_can_join_again_once_the_first_line_is_closed(self, firm):
        _, owner, customer, _, elevators = firm
        client = api_for(owner)
        first = make_contract(client, customer.id)
        second = make_contract(client, customer.id)

        client.post(
            reverse("contract-add-elevators", args=[first["id"]]),
            {"elevator_ids": [str(elevators[0].pk)]},
            format="json",
        )
        client.delete(reverse("contract-remove-elevator", args=[first["id"], str(elevators[0].pk)]))
        assert (
            client.post(
                reverse("contract-add-elevators", args=[second["id"]]),
                {"elevator_ids": [str(elevators[0].pk)]},
                format="json",
            ).status_code
            == 200
        )

    def test_removing_closes_the_line_rather_than_deleting_it(self, firm):
        company, owner, customer, _, elevators = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id)
        client.post(
            reverse("contract-add-elevators", args=[contract["id"]]),
            {"elevator_ids": [str(elevators[0].pk)]},
            format="json",
        )
        client.delete(
            reverse("contract-remove-elevator", args=[contract["id"], str(elevators[0].pk)])
        )
        with company_context(company.id):
            # The billing history has to survive the elevator leaving.
            line = ContractElevator.objects.get(elevator=elevators[0])
            assert line.removed_at is not None

    def test_status_follows_coverage(self, firm):
        company, owner, customer, _, elevators = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id)

        with company_context(company.id):
            assert Elevator.objects.get(pk=elevators[0].pk).status == ElevatorStatus.UNCONTRACTED

        client.post(
            reverse("contract-add-elevators", args=[contract["id"]]),
            {"elevator_ids": [str(elevators[0].pk)]},
            format="json",
        )
        with company_context(company.id):
            assert Elevator.objects.get(pk=elevators[0].pk).status == ElevatorStatus.ACTIVE

    def test_the_status_cannot_be_set_by_hand(self, firm):
        _, owner, _, _, elevators = firm
        response = api_for(owner).patch(
            reverse("elevator-detail", args=[elevators[0].pk]),
            {"status": ElevatorStatus.UNCONTRACTED},
            format="json",
        )
        # Otherwise a client could detach an elevator from its contract by
        # editing a dropdown, and the two records would disagree.
        assert response.status_code == 400


class TestTermination:
    def test_it_closes_every_line_and_frees_the_elevators(self, firm):
        company, owner, customer, _, elevators = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id)
        client.post(
            reverse("contract-add-elevators", args=[contract["id"]]),
            {"elevator_ids": [str(e.pk) for e in elevators]},
            format="json",
        )

        response = client.post(
            reverse("contract-terminate", args=[contract["id"]]),
            {"terminated_at": str(TODAY), "reason": "Customer moved to another firm"},
            format="json",
        )
        assert response.status_code == 200
        assert response.data["status"] == ContractStatus.TERMINATED

        with company_context(company.id):
            assert not ContractElevator.objects.filter(
                contract_id=contract["id"], removed_at__isnull=True
            ).exists()
            for elevator in elevators:
                assert Elevator.objects.get(pk=elevator.pk).status == ElevatorStatus.UNCONTRACTED

    def test_a_reason_is_required(self, firm):
        _, owner, customer, _, _ = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id)
        response = client.post(
            reverse("contract-terminate", args=[contract["id"]]),
            {"terminated_at": str(TODAY), "reason": "   "},
            format="json",
        )
        # A field-level answer, so the interface can mark the field rather than
        # showing a banner. It is what the audit trail will be read for a year
        # later, so it cannot be skipped.
        assert response.status_code == 400
        detail = response.data["error"]["details"][0]
        assert detail["field"] == "reason"
        assert detail["code"] == "TERMINATION_REASON_REQUIRED"

    def test_it_cannot_be_done_through_a_patch(self, firm):
        _, owner, customer, _, _ = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id)
        response = client.patch(
            reverse("contract-detail", args=[contract["id"]]),
            {"status": ContractStatus.TERMINATED},
            format="json",
        )
        # A PATCH would push the side effects onto the client, and one that
        # forgot a step would leave the data inconsistent with nothing saying so.
        assert response.status_code == 400

    def test_terminating_twice_is_refused(self, firm):
        _, owner, customer, _, _ = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id)
        body = {"terminated_at": str(TODAY), "reason": "First"}
        client.post(reverse("contract-terminate", args=[contract["id"]]), body, format="json")
        again = client.post(
            reverse("contract-terminate", args=[contract["id"]]), body, format="json"
        )
        assert again.status_code == 422


class TestRenewal:
    def test_it_produces_a_draft_and_carries_the_elevators(self, firm):
        company, owner, customer, _, elevators = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id)
        client.post(
            reverse("contract-add-elevators", args=[contract["id"]]),
            {"elevator_ids": [str(elevators[0].pk)]},
            format="json",
        )

        response = client.post(
            reverse("contract-renew", args=[contract["id"]]),
            {
                "start_date": str(TODAY + timedelta(days=366)),
                "end_date": str(TODAY + timedelta(days=730)),
            },
            format="json",
        )
        assert response.status_code == 201
        # A draft is what makes renewal reversible, and why it needs none of
        # termination's ceremony: nothing is billed until it is activated.
        assert response.data["status"] == ContractStatus.DRAFT
        assert str(response.data["previous_contract_id"]) == str(contract["id"])

        with company_context(company.id):
            assert Contract.objects.get(pk=contract["id"]).status == ContractStatus.RENEWED
            # Still exactly one open line for the elevator — the old one closed
            # as the new one opened.
            assert (
                ContractElevator.objects.filter(
                    elevator=elevators[0], removed_at__isnull=True
                ).count()
                == 1
            )


class TestFinancialVisibility:
    def _contract_for(self, firm) -> str:
        _, owner, customer, _, _ = firm
        return make_contract(api_for(owner), customer.id)["id"]

    def _user(self, company, role: str, email: str) -> User:
        with system_context():
            return User.objects.create_user(
                email=email,
                password=PASSWORD,
                company=company,
                first_name="X",
                last_name="Y",
                role=role,
            )

    def test_operations_does_not_receive_the_money(self, firm):
        company, _, _, _, _ = firm
        contract_id = self._contract_for(firm)
        user = self._user(company, Role.OPERATIONS, "ops@example.com")
        data = api_for(user).get(reverse("contract-detail", args=[contract_id])).data
        # Removed from the payload rather than nulled: a null reads as "not set
        # yet" and sends someone looking for a value that is not theirs.
        for field in ("monthly_fee", "vat_rate", "pricing_type", "billing_period"):
            assert field not in data

    def test_accounting_does_receive_it(self, firm):
        company, _, _, _, _ = firm
        contract_id = self._contract_for(firm)
        user = self._user(company, Role.ACCOUNTANT, "acc@example.com")
        data = api_for(user).get(reverse("contract-detail", args=[contract_id])).data
        assert data["monthly_fee"] == "4750.00"

    def test_technician_has_no_access_at_all(self, firm):
        company, _, _, _, _ = firm
        contract_id = self._contract_for(firm)
        user = self._user(company, Role.TECHNICIAN, "tech@example.com")
        assert api_for(user).get(reverse("contract-detail", args=[contract_id])).status_code == 403


@pytest.fixture
def contracted(firm):
    """A contract covering the first elevator, created through the API."""
    company, owner, customer, _, elevators = firm
    client = api_for(owner)
    contract = make_contract(client, customer.id)
    client.post(
        reverse("contract-add-elevators", args=[contract["id"]]),
        {"elevator_ids": [str(elevators[0].id)], "unit_price": "1250.00"},
        format="json",
    )
    return company, owner, contract, elevators[0]


def colleague(company, role: str, email: str) -> User:
    with system_context():
        return User.objects.create_user(
            email=email,
            password=PASSWORD,
            company=company,
            first_name=role.title(),
            last_name="Person",
            role=role,
        )


class TestTheContractOnAnElevator:
    """What the elevator detail screen shows about cover.

    The elevator already knows whether it is covered — `status` says so — but
    not by what. Without this the client would have to fetch every contract the
    customer has and search their lines for one elevator.
    """

    def test_a_covered_elevator_names_its_contract(self, contracted):
        _, owner, contract, elevator = contracted
        body = api_for(owner).get(reverse("elevator-detail", args=[elevator.id])).data

        assert body["current_contract"]["contract_number"] == contract["contract_number"]
        assert body["current_contract"]["end_date"] == contract["end_date"]

    def test_an_uncovered_elevator_says_so_plainly(self, firm):
        _, owner, _, _, elevators = firm
        body = api_for(owner).get(reverse("elevator-detail", args=[elevators[2].id])).data
        assert body["current_contract"] is None

    def test_a_terminated_contract_is_not_cover(self, contracted):
        _, owner, contract, elevator = contracted
        api_for(owner).post(
            reverse("contract-terminate", args=[contract["id"]]),
            {"terminated_at": str(TODAY), "reason": "Customer moved to another firm"},
            format="json",
        )

        body = api_for(owner).get(reverse("elevator-detail", args=[elevator.id])).data
        # Telling an operator there is cover where there is none is worse than
        # telling them nothing.
        assert body["current_contract"] is None

    def test_the_detail_costs_no_extra_query_per_line(
        self, contracted, django_assert_max_num_queries
    ):
        _, owner, _, elevator = contracted
        client = api_for(owner)
        # Warm the auth path so the assertion is about the serializer.
        client.get(reverse("elevator-detail", args=[elevator.id]))

        with django_assert_max_num_queries(4):
            client.get(reverse("elevator-detail", args=[elevator.id]))


class TestWhoSeesThePrice:
    def test_an_owner_sees_the_unit_price_as_a_string(self, contracted):
        _, owner, _, elevator = contracted
        body = api_for(owner).get(reverse("elevator-detail", args=[elevator.id])).data

        # A string, not a number. Money never crosses the wire as a float, and a
        # SerializerMethodField returning a raw Decimal would have done exactly
        # that.
        assert body["current_contract"]["unit_price"] == "1250.00"

    def test_an_accountant_cannot_reach_this_screen_at_all(self, contracted):
        company, _, _, elevator = contracted
        accountant = colleague(company, Role.ACCOUNTANT, "acc@example.com")

        # Worth stating rather than leaving implied: accounting may read
        # financials but not elevators, so the only roles that see a unit price
        # here are owner and admin — the intersection of the two permissions.
        assert (
            api_for(accountant).get(reverse("elevator-detail", args=[elevator.id])).status_code
            == 403
        )

    def test_operations_does_not(self, contracted):
        company, _, _, elevator = contracted
        operations = colleague(company, Role.OPERATIONS, "ops@example.com")

        body = api_for(operations).get(reverse("elevator-detail", args=[elevator.id])).data

        # Absent, not null. `null` says the line has no price; absence says the
        # reader is not entitled to ask. And it has to be absent from the body —
        # hiding it in the client would leave the number one network tab away
        # from anyone who looked.
        assert "unit_price" not in body["current_contract"]

    def test_a_technician_does_not_either(self, contracted):
        company, _, _, elevator = contracted
        technician = colleague(company, Role.TECHNICIAN, "tech@example.com")
        with company_context(company.id):
            technician.customer_assignments.create(
                company=company, customer=elevator.building.customer
            )

        body = api_for(technician).get(reverse("elevator-detail", args=[elevator.id])).data
        assert "unit_price" not in body["current_contract"]


class TestWhatTheCustomerPays:
    """The totals, computed here rather than in a browser.

    Money crosses this API as a string so that JavaScript's float arithmetic
    never touches it. A client that adds the lines up parses those strings back
    into floats, and a contract worth 4,750.00 renders as 4,749.999999999999 —
    which undoes the reason the strings exist.
    """

    def test_a_flat_contract_totals_its_fee(self, firm):
        _, owner, customer, _, _ = firm
        client = api_for(owner)
        contract = make_contract(
            client,
            customer.id,
            pricing_type=PricingType.FLAT,
            monthly_fee="4750.00",
            vat_rate="20.00",
        )

        body = client.get(reverse("contract-detail", args=[contract["id"]])).data

        assert body["monthly_subtotal"] == "4750.00"
        assert body["vat_amount"] == "950.00"
        assert body["monthly_total"] == "5700.00"

    def test_a_per_elevator_contract_sums_its_open_lines(self, firm):
        _, owner, customer, _, elevators = firm
        client = api_for(owner)
        contract = make_contract(
            client, customer.id, pricing_type=PricingType.PER_ELEVATOR, vat_rate="20.00"
        )
        client.post(
            reverse("contract-add-elevators", args=[contract["id"]]),
            {"elevator_ids": [str(elevators[0].pk), str(elevators[1].pk)], "unit_price": "1250.00"},
            format="json",
        )

        body = client.get(reverse("contract-detail", args=[contract["id"]])).data
        assert body["monthly_subtotal"] == "2500.00"
        assert body["monthly_total"] == "3000.00"

    def test_a_removed_elevator_stops_being_billed(self, firm):
        _, owner, customer, _, elevators = firm
        client = api_for(owner)
        contract = make_contract(client, customer.id, pricing_type=PricingType.PER_ELEVATOR)
        client.post(
            reverse("contract-add-elevators", args=[contract["id"]]),
            {"elevator_ids": [str(elevators[0].pk), str(elevators[1].pk)], "unit_price": "1250.00"},
            format="json",
        )
        client.delete(
            reverse("contract-remove-elevator", args=[contract["id"], str(elevators[1].pk)])
        )

        body = client.get(reverse("contract-detail", args=[contract["id"]])).data
        # A closed line is not billed. Including it would keep charging for a
        # lift that left the agreement.
        assert body["monthly_subtotal"] == "1250.00"

    def test_the_values_are_strings(self, firm):
        import json

        _, owner, customer, _, _ = firm
        client = api_for(owner)
        contract = make_contract(
            client,
            customer.id,
            pricing_type=PricingType.FLAT,
            monthly_fee="4750.00",
            vat_rate="20.00",
        )

        raw = json.loads(client.get(reverse("contract-detail", args=[contract["id"]])).content)
        # In the rendered JSON, not just in `.data`: a Decimal that reaches the
        # renderer unformatted becomes a JSON number, and the whole point is
        # that it does not.
        for field in ("monthly_subtotal", "vat_amount", "monthly_total"):
            assert isinstance(raw[field], str), field

    def test_operations_is_not_told_the_totals(self, firm):
        company, owner, customer, _, _ = firm
        contract = make_contract(api_for(owner), customer.id)
        operations = colleague(company, Role.OPERATIONS, "ops@example.com")

        body = api_for(operations).get(reverse("contract-detail", args=[contract["id"]])).data

        for field in ("monthly_subtotal", "vat_amount", "monthly_total"):
            assert field not in body, field

    def test_a_renewal_names_its_predecessor(self, firm):
        _, owner, customer, _, _ = firm
        client = api_for(owner)
        original = make_contract(client, customer.id)
        renewed = client.post(
            reverse("contract-renew", args=[original["id"]]),
            {
                "start_date": str(TODAY + timedelta(days=366)),
                "end_date": str(TODAY + timedelta(days=731)),
            },
            format="json",
        ).data

        body = client.get(reverse("contract-detail", args=[renewed["id"]])).data
        # The screen names the contract this one replaced; it only had an id.
        assert body["previous_contract_number"] == original["contract_number"]

    def test_a_contract_with_no_rate_stated_adds_no_vat(self, firm):
        _, owner, customer, _, _ = firm
        client = api_for(owner)
        contract = make_contract(
            client, customer.id, pricing_type=PricingType.FLAT, monthly_fee="1000.00"
        )

        body = client.get(reverse("contract-detail", args=[contract["id"]])).data

        # `vat_rate` is nullable and has no default. No rate is not a rate of
        # zero by accident — it is a contract nobody has said the rate for, and
        # inventing 20% here would be inventing a tax position.
        assert body["vat_amount"] == "0.00"
        assert body["monthly_total"] == "1000.00"
