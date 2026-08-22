"""The token on the sticker, and the one instant it can be taken twice.

Issue #60. Uniqueness of a QR token cannot be settled by reading. Whatever a SELECT says,
another request can commit the same value before this one's INSERT lands. The
window is microseconds wide and every elevator create runs through it, so the
loser of that race received a constraint violation over a field it never
supplied and could not correct by editing its own request. Section 11.1 asks for
the database's refusal to be caught and the token regenerated, at most three
times, and never for a duplicate to be kept quietly.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError
from django.db.models.signals import pre_save
from django.urls import reverse
from rest_framework.test import APIClient

from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.properties.models import Building, BuildingType
from apps.users.models import User
from apps.users.services import issue_tokens, register_company
from core.context import company_context

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
            company=company, type=CustomerType.COMPLEX_MANAGEMENT, legal_name="Site Management"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="A Blok",
            type=BuildingType.RESIDENTIAL,
            address_note="Behind the market",
        )
    return company, owner, building


@pytest.fixture
def a_competing_request(firm):
    """Let another request commit our token after any check and before our write.

    A `pre_save` receiver is the only place that is genuinely *between* the two:
    whatever the create decided the token would be, it is still free when the
    receiver runs and taken by the time the INSERT reaches the database. That is
    the race, reproduced rather than described.

    It fires once. The point is that a create survives losing the race, not that
    it survives losing it forever.
    """
    company, _, building = firm
    taken: list[str] = []

    def steal(sender, instance, **kwargs):
        if taken or not instance.qr_token:
            return
        taken.append(instance.qr_token)
        thief = Elevator(
            company=company,
            building=building,
            registration_number="34-2020-999999",
            name="Somebody else's lift",
            qr_token=instance.qr_token,
        )
        thief.save()

    pre_save.connect(steal, sender=Elevator)
    try:
        yield taken
    finally:
        pre_save.disconnect(steal, sender=Elevator)


class TestATokenTakenBeforeTheInsertLands:
    def test_the_elevator_is_still_created(self, firm, a_competing_request):
        _, owner, building = firm

        response = api_for(owner).post(
            reverse("elevator-list"),
            {
                "building": str(building.pk),
                "registration_number": "34-2020-000001",
                "name": "Sol asansor",
            },
            format="json",
        )

        assert response.status_code == 201, (
            "a token taken by a concurrent create reached the caller as "
            f"{response.data['error']['code']}, over a field they never sent "
            "and cannot correct by changing their request."
        )

    def test_it_gets_a_token_of_its_own(self, firm, a_competing_request):
        _, owner, building = firm

        created = api_for(owner).post(
            reverse("elevator-list"),
            {
                "building": str(building.pk),
                "registration_number": "34-2020-000001",
                "name": "Sol asansor",
            },
            format="json",
        )

        with company_context(firm[0].id):
            token = Elevator.objects.get(pk=created.data["id"]).qr_token
        # Regenerated, not reused. Two elevators sharing a sticker is the one
        # outcome worse than the error.
        assert token not in a_competing_request
        assert len(token) == 12


class TestTheRetryHasLimits:
    """Asked of the retry directly: the API cannot lose the race three times.

    Reproducing three consecutive collisions through a request would mean
    stealing every token the loop generates, which is a scenario that cannot
    happen in the world. The rule it would be testing — three attempts, then the
    error surfaces — belongs to the loop, so it is asked there.
    """

    def test_it_gives_up_after_three_attempts(self, db):
        from apps.elevators.services import MAX_ATTEMPTS, save_with_qr_token

        attempts: list[str] = []

        def always_taken(token: str) -> None:
            attempts.append(token)
            raise IntegrityError("UNIQUE constraint failed: elevator.qr_token")

        with pytest.raises(IntegrityError):
            save_with_qr_token(always_taken)

        # Bounded, and a fresh token each time: a loop that retried the same
        # value would spin against a refusal that can never change its mind.
        assert len(attempts) == MAX_ATTEMPTS
        assert len(set(attempts)) == MAX_ATTEMPTS

    def test_a_different_constraint_is_not_retried(self, db):
        from apps.elevators.services import save_with_qr_token

        attempts: list[str] = []

        def registration_number_taken(token: str) -> None:
            attempts.append(token)
            raise IntegrityError(
                'duplicate key value violates unique constraint "uq_elevator_registration_number"'
            )

        with pytest.raises(IntegrityError):
            save_with_qr_token(registration_number_taken)

        # A duplicate registration number is the caller's to fix and no token
        # will change it. Retrying would spend three round trips before handing
        # back the answer it already had.
        assert len(attempts) == 1
