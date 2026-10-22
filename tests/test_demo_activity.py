"""The demo company's Phase 2 life — core/demo_activity.py.

A demonstration that opens on empty Phase 2 screens shows nothing of Phase 2.
These check that every module comes out populated and in the states a demo
needs (an emergency on the board, overdue lifts, a red label, a draft waiting
for a price, a portal login that works), that the live tenant can be brought up
to date safely and repeatably, and that no e-mail ever leaves during seeding.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core import mail as django_mail
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.billing.models import InvoiceDraft, InvoiceLine, InvoiceStatus
from apps.companies.models import Company
from apps.contracts.models import Contract, ContractStatus
from apps.customers.models import Customer
from apps.elevators.models import Elevator, ElevatorStatus
from apps.faults.models import Fault, FaultChannel, FaultPriority, FaultStatus
from apps.inspections.models import Inspection, InspectionKind, Label, NonConformityStatus
from apps.maintenance.models import (
    ChecklistItem,
    ChecklistResult,
    MaintenanceVisit,
    MaintenanceVisitItem,
    PlannedVisit,
    PlannedVisitStatus,
    VisitStatus,
)
from apps.maintenance.services import due_date_in as period_due
from apps.maintenance.services import period_of
from apps.notifications.models import Notification, NotificationKind
from apps.portal import services as portal_services
from apps.portal.models import PortalAccount, PortalAccountStatus
from apps.users.models import Role, User
from core import demo, demo_activity
from core.context import company_context, system_context
from core.management.commands.create_demo_account import DEFAULT_EMAIL, LEGAL_NAME
from core.scheduling import local_today

SAMPLE_DATA = str(Path(__file__).resolve().parent / "data" / "address")


@pytest.fixture
def seeded(db, django_capture_on_commit_callbacks) -> Company:
    """`seed_demo_data`, with every after-commit callback run as a real commit would."""
    call_command("load_address_data", path=SAMPLE_DATA)
    django_mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        call_command("seed_demo_data", stdout=StringIO())
    with system_context():
        return Company.objects.get()


def in_tenant(company: Company, query: Any) -> Any:
    with company_context(company.id):
        return query()


def test_seeding_sends_no_email(seeded):
    # The owner's verification, every notification, the portal invitations:
    # none of it may reach a mailbox.
    assert django_mail.outbox == []


class TestVisitsAndThePlan:
    def test_most_covered_lifts_have_a_history(self, seeded):
        today = local_today()
        with company_context(seeded.id):
            covered = list(
                Elevator.objects.filter(
                    contract_lines__removed_at__isnull=True,
                    contract_lines__contract__status=ContractStatus.ACTIVE,
                ).distinct()
            )
            visited = [lift for lift in covered if lift.last_maintenance_date]
            overdue = [
                lift
                for lift in covered
                if lift.next_maintenance_date and lift.next_maintenance_date < today
            ]
            assert len(visited) >= 0.8 * len(covered)
            assert len(overdue) >= demo_activity.NEGLECTED_LIFTS
            oldest = MaintenanceVisit.objects.order_by("visit_date").first()
            assert oldest is not None
            assert oldest.visit_date <= today - timedelta(days=55)

    def test_visits_look_like_visits(self, seeded):
        with company_context(seeded.id):
            completed = MaintenanceVisit.objects.filter(status=VisitStatus.COMPLETED)
            assert completed.exists()
            for visit in completed[:10]:
                assert visit.items.count() == len(ChecklistItem.values)
            attention = MaintenanceVisitItem.objects.filter(result=ChecklistResult.ATTENTION_NEEDED)
            assert attention.exists()
            assert all(item.note for item in attention)
            failed = MaintenanceVisit.objects.exclude(status=VisitStatus.COMPLETED)
            assert failed.exists()
            assert all(visit.status_note for visit in failed)

    def test_the_plan_is_the_generators_and_closed_by_visits(self, seeded):
        today = local_today()
        with company_context(seeded.id):
            this_month = PlannedVisit.objects.filter(period=period_of(today))
            assert this_month.exists()
            assert PlannedVisit.objects.exclude(period=period_of(today)).exists()
            done = this_month.filter(status=PlannedVisitStatus.DONE)
            assert done.exists()
            assert all(row.visit_id for row in done)
            tried = this_month.filter(
                status=PlannedVisitStatus.PLANNED, visit__status=VisitStatus.INCOMPLETE
            )
            assert tried.count() >= 1
            unassigned = this_month.filter(
                status=PlannedVisitStatus.PLANNED, technician__isnull=True
            )
            assert unassigned.count() >= 2
            if today.day > 1:
                assert this_month.filter(
                    status=PlannedVisitStatus.PLANNED, due_date__lt=today
                ).exists()

    def test_the_next_two_weeks_have_planned_days(self, seeded):
        # The calendar opens on this week: it needs visits on its days, and
        # some left in the tray for the office to place.
        today = local_today()
        with company_context(seeded.id):
            scheduled = list(PlannedVisit.objects.filter(planned_date__isnull=False))
            waiting = PlannedVisit.objects.filter(
                status=PlannedVisitStatus.PLANNED,
                technician__isnull=False,
                planned_date__isnull=True,
            )
            assert scheduled
            assert waiting.exists()
            for row in scheduled:
                assert row.planned_date is not None
                assert today <= row.planned_date <= today + timedelta(days=13)
                assert row.planned_date.weekday() != 6
                assert period_of(row.planned_date) == row.period
                assert row.technician_id is not None

    def test_every_building_and_the_firm_have_a_position(self, seeded):
        # The route order needs somewhere to start and somewhere to go.
        from apps.properties.models import Building

        centre = demo_activity.DEMO_CENTRE
        with company_context(seeded.id):
            buildings = list(Building.objects.all())
            assert buildings
            for building in buildings:
                assert building.latitude is not None and building.longitude is not None
                assert abs(float(building.latitude) - centre[0]) < 0.1
                assert abs(float(building.longitude) - centre[1]) < 0.1
        seeded.refresh_from_db()
        assert seeded.latitude is not None and seeded.longitude is not None


class TestFaults:
    def test_one_in_every_state(self, seeded):
        with company_context(seeded.id):
            states = set(Fault.objects.values_list("status", flat=True))
            assert states >= {
                FaultStatus.OPEN,
                FaultStatus.ASSIGNED,
                FaultStatus.ON_SITE,
                FaultStatus.RESOLVED,
                FaultStatus.CLOSED,
                FaultStatus.CANCELLED,
            }
            assert 8 <= Fault.objects.count() <= 12

    def test_the_emergency_came_from_the_sticker(self, seeded):
        with company_context(seeded.id):
            emergency = Fault.objects.get(priority=FaultPriority.EMERGENCY)
            assert emergency.channel == FaultChannel.PUBLIC_QR
            assert emergency.status == FaultStatus.OPEN
            assert emergency.reporter_name and emergency.reporter_phone
            assert emergency.elevator.status == ElevatorStatus.OUT_OF_SERVICE

    def test_closed_ones_billable_and_not(self, seeded):
        with company_context(seeded.id):
            closed = Fault.objects.filter(status=FaultStatus.CLOSED)
            assert closed.filter(billable=True).exclude(parts_used="").exists()
            waived = closed.get(billable=False)
            assert waived.billable_overridden is True
            for fault in closed:
                assert fault.reported_at <= fault.resolved_at <= fault.closed_at  # type: ignore[operator]


class TestInspections:
    def test_every_lift_has_a_periodic_inspection(self, seeded):
        with company_context(seeded.id):
            lifts = Elevator.objects.count()
            inspected = (
                Inspection.objects.filter(kind=InspectionKind.PERIODIC)
                .values("elevator")
                .distinct()
                .count()
            )
            assert inspected == lifts
            labels = list(Elevator.objects.values_list("inspection_label", flat=True))
            assert labels.count(Label.GREEN) + labels.count(Label.BLUE) >= 0.6 * len(labels)

    def test_the_ones_that_matter(self, seeded):
        today = local_today()
        with company_context(seeded.id):
            yellow = Inspection.objects.filter(
                label=Label.YELLOW,
                correction_deadline__gt=today,
                correction_deadline__lte=today + timedelta(days=30),
                nonconformities__status=NonConformityStatus.OPEN,
            ).distinct()
            assert yellow.count() == 2
            red = Inspection.objects.get(label=Label.RED)
            assert red.elevator.status == ElevatorStatus.OUT_OF_SERVICE
            defect = red.nonconformities.get()
            assert defect.fault is not None
            assert defect.fault.priority == FaultPriority.HIGH
            follow_up = Inspection.objects.get(kind=InspectionKind.FOLLOW_UP)
            assert follow_up.follows is not None


class TestPortalAndBilling:
    def test_one_active_account_and_one_invitation(self, seeded):
        with company_context(seeded.id):
            accounts = list(PortalAccount.objects.all())
        assert sorted(account.status for account in accounts) == [
            PortalAccountStatus.ACTIVE,
            PortalAccountStatus.INVITED,
        ]
        email = demo_activity.portal_login(seeded)
        assert email
        account = portal_services.authenticate(
            email=email, password=demo_activity.PORTAL_PASSWORD, ip="198.51.100.1"
        )
        assert account.company_id == seeded.id

    def test_drafts_from_the_real_generator(self, seeded):
        with company_context(seeded.id):
            drafts = InvoiceDraft.objects.all()
            assert drafts.exists()
            assert InvoiceLine.objects.filter(fault__isnull=False, needs_price=True).exists()
            assert drafts.filter(status=InvoiceStatus.ISSUED).exists()
            assert drafts.filter(status=InvoiceStatus.PAID).count() == 1
            for draft in drafts:
                assert draft.total == draft.subtotal + draft.vat_amount


class TestContractsAndNotifications:
    def test_a_renewal_window_and_an_auto_renewal(self, seeded):
        today = local_today()
        with company_context(seeded.id):
            assert Contract.objects.filter(
                status=ContractStatus.ACTIVE,
                end_date__gt=today,
                end_date__lte=today + timedelta(days=60),
            ).exists()
            renewed = Contract.objects.get(status=ContractStatus.RENEWED)
            successor = Contract.objects.get(previous_contract=renewed)
            assert successor.status == ContractStatus.ACTIVE

    def test_a_history_in_the_bell(self, seeded):
        with company_context(seeded.id):
            owner = User.objects.get(company=seeded, role=Role.OWNER)
            technicians = User.objects.filter(company=seeded, role=Role.TECHNICIAN)
            mine = Notification.objects.filter(recipient=owner)
            kinds = set(Notification.objects.values_list("kind", flat=True))
            assert kinds >= {
                NotificationKind.MAINTENANCE_OVERDUE,
                NotificationKind.FAULT_EMERGENCY,
                NotificationKind.INSPECTION_RED_LABEL,
                NotificationKind.INVOICE_DRAFTS_READY,
                NotificationKind.CONTRACT_RENEWAL_DUE,
                NotificationKind.CONTRACT_AUTO_RENEWED,
                NotificationKind.VISIT_ASSIGNED,
            }
            assert mine.filter(read_at__isnull=True).exists()
            assert mine.filter(read_at__isnull=False).exists()
            assert len({n.created_at.date() for n in mine}) > 1
            for technician in technicians:
                assert Notification.objects.filter(recipient=technician).exists()


# ---------------------------------------------------------------------------
# Bringing the live demo tenant up to date
# ---------------------------------------------------------------------------


@pytest.fixture
def phase_one_demo(db, monkeypatch) -> Company:
    """The live demo tenant as it is today: Phase 1 data only."""
    call_command("load_address_data", path=SAMPLE_DATA)
    original = demo_activity.populate_activity
    monkeypatch.setattr(
        demo_activity, "populate_activity", lambda *a, **k: demo_activity.Activity()
    )
    call_command("create_demo_account", "--with-data", stdout=StringIO())
    monkeypatch.setattr(demo_activity, "populate_activity", original)
    with system_context():
        return Company.objects.get(legal_name=LEGAL_NAME)


def counts(company: Company) -> dict[str, int]:
    models = [
        MaintenanceVisit,
        MaintenanceVisitItem,
        PlannedVisit,
        Fault,
        Inspection,
        PortalAccount,
        InvoiceDraft,
        InvoiceLine,
        Notification,
        Contract,
    ]
    with company_context(company.id):
        return {model.__name__: model.objects.count() for model in models}


class TestRefresh:
    def test_it_brings_a_phase_one_tenant_up_to_date(
        self, phase_one_demo, django_capture_on_commit_callbacks
    ):
        assert counts(phase_one_demo)["Fault"] == 0
        out = StringIO()
        django_mail.outbox.clear()
        with django_capture_on_commit_callbacks(execute=True):
            call_command("refresh_demo_data", stdout=out)
        after = counts(phase_one_demo)
        for name in ("MaintenanceVisit", "PlannedVisit", "Fault", "Inspection", "InvoiceDraft"):
            assert after[name] > 0, name
        assert after["PortalAccount"] == 2
        assert django_mail.outbox == []
        assert f"/ {demo_activity.PORTAL_PASSWORD}" in out.getvalue()

    def test_a_tenant_the_daily_jobs_already_ran_on(self, phase_one_demo):
        # Production: plan_maintenance and draft_invoices have been running on
        # the demo tenant since M3 and M7, so rows and drafts already exist.
        from apps.billing import services as billing
        from apps.maintenance import services as maintenance

        with company_context(phase_one_demo.id):
            maintenance.generate_plan(today=local_today())
            billing.generate(today=local_today())
        call_command("refresh_demo_data", stdout=StringIO())
        with company_context(phase_one_demo.id):
            assert PlannedVisit.objects.filter(
                period=period_of(local_today()), status=PlannedVisitStatus.DONE
            ).exists()
            assert InvoiceLine.objects.filter(fault__isnull=False, needs_price=True).exists()
            for draft in InvoiceDraft.objects.all():
                assert draft.total == draft.subtotal + draft.vat_amount

    def test_planned_days_are_given_once(self, phase_one_demo):
        def days() -> dict[Any, Any]:
            with company_context(phase_one_demo.id):
                return dict(
                    PlannedVisit.objects.filter(planned_date__isnull=False).values_list(
                        "id", "planned_date"
                    )
                )

        call_command("refresh_demo_data", stdout=StringIO())
        first = days()
        assert first
        out = StringIO()
        call_command("refresh_demo_data", stdout=out)
        assert days() == first
        assert "planned days +0" in out.getvalue()

    def test_positions_are_given_once(self, phase_one_demo):
        from apps.properties.models import Building

        def positions() -> dict[Any, Any]:
            with company_context(phase_one_demo.id):
                return {b.pk: (b.latitude, b.longitude) for b in Building.objects.all()}

        call_command("refresh_demo_data", stdout=StringIO())
        first = positions()
        assert all(lat is not None for lat, _ in first.values())
        call_command("refresh_demo_data", stdout=StringIO())
        assert positions() == first

    def test_a_second_run_changes_nothing(self, phase_one_demo):
        call_command("refresh_demo_data", stdout=StringIO())
        first = counts(phase_one_demo)
        out = StringIO()
        call_command("refresh_demo_data", stdout=out)
        assert counts(phase_one_demo) == first
        assert "faults +0" in out.getvalue()
        assert "notifications +0" in out.getvalue()

    def test_it_refuses_any_other_company(self, db):
        call_command("load_address_data", path=SAMPLE_DATA)
        call_command("seed_demo_data", stdout=StringIO())
        with pytest.raises(CommandError, match="only ever writes to the demo tenant"):
            call_command("refresh_demo_data", email="demo@shiftlush.local")
        with pytest.raises(CommandError, match="not a demo owner"):
            call_command("refresh_demo_data", email="nobody@example.com")

    def test_it_refuses_an_empty_demo_tenant(self, db):
        call_command("create_demo_account", stdout=StringIO())
        with pytest.raises(CommandError, match="create_demo_account --with-data"):
            call_command("refresh_demo_data", email=DEFAULT_EMAIL)
        with system_context():
            company = Company.objects.get(legal_name=LEGAL_NAME)
        assert in_tenant(company, lambda: Customer.objects.count()) == 0


class TestOneStrayRowBlocksNothing:
    """SL-161: the live demo tenant held one hand-entered visit, and the refresh
    skipped the visit history of every other lift."""

    def test_a_pre_existing_visit_leaves_every_other_lift_its_history(self, phase_one_demo):
        today = local_today()
        with company_context(phase_one_demo.id):
            covered = list(
                Elevator.objects.filter(
                    contract_lines__removed_at__isnull=True,
                    contract_lines__contract__status=ContractStatus.ACTIVE,
                )
                .distinct()
                .order_by("registration_number", "id")
            )
            stray_lift = covered[5]
            line = stray_lift.contract_lines.get(removed_at__isnull=True)
            office = User.objects.get(company=phase_one_demo, role=Role.OPERATIONS)
            stray = MaintenanceVisit.objects.create(
                company=phase_one_demo,
                elevator=stray_lift,
                contract=line.contract,
                technician=office,
                visit_date=today - timedelta(days=4),
                status=VisitStatus.COMPLETED,
            )

        call_command("refresh_demo_data", stdout=StringIO())

        with company_context(phase_one_demo.id):
            # The hand-entered visit is the only one on its lift.
            assert list(MaintenanceVisit.objects.filter(elevator=stray_lift)) == [stray]
            others = [lift for lift in covered if lift.pk != stray_lift.pk]
            for lift in others:
                assert MaintenanceVisit.objects.filter(elevator=lift).exists(), lift
            refreshed = Elevator.objects.filter(pk__in=[lift.pk for lift in others])
            assert all(lift.last_maintenance_date for lift in refreshed)
            overdue = refreshed.filter(next_maintenance_date__lt=today).count()
            assert overdue >= demo_activity.NEGLECTED_LIFTS
            assert PlannedVisit.objects.filter(
                period=period_of(today), status=PlannedVisitStatus.DONE
            ).exists()
        first = counts(phase_one_demo)
        call_command("refresh_demo_data", stdout=StringIO())
        assert counts(phase_one_demo) == first

    def test_stray_faults_inspections_and_portal_accounts_fill_only_their_slot(
        self, phase_one_demo
    ):
        from apps.faults import services as fault_services

        with company_context(phase_one_demo.id):
            lift = Elevator.objects.order_by("registration_number", "id")[0]
            office = User.objects.get(company=phase_one_demo, role=Role.OPERATIONS)
            # A cancelled fault, an inspection on one lift, and a contact
            # already invited to the portal, all entered by hand.
            stray = fault_services.report_by_staff(
                elevator=lift, description="x", priority=FaultPriority.NORMAL, reported_by=office
            )
            fault_services.cancel(stray, reason="test", by=office)
            from apps.inspections import services as inspection_services

            inspection_services.record(
                elevator=lift, inspected_on=local_today(), label=Label.GREEN, by=office
            )

        call_command("refresh_demo_data", stdout=StringIO())

        with company_context(phase_one_demo.id):
            states = set(Fault.objects.values_list("status", flat=True))
            assert states >= {
                FaultStatus.OPEN,
                FaultStatus.ASSIGNED,
                FaultStatus.ON_SITE,
                FaultStatus.RESOLVED,
                FaultStatus.CLOSED,
            }
            # The cancelled slot was already filled: no second cancellation.
            assert Fault.objects.filter(status=FaultStatus.CANCELLED).count() == 1
            assert Inspection.objects.filter(elevator=lift).count() == 1
            lifts = Elevator.objects.count()
            assert Inspection.objects.values("elevator").distinct().count() == lifts
            assert Inspection.objects.filter(label=Label.RED).exists()
            assert PortalAccount.objects.count() == 2


class TestSeedConsistency:
    """SL-162 (audit P3-16): a demo is believed only if it adds up."""

    def test_capacity_names_notes_and_people(self, seeded):
        from apps.customers.models import CustomerContact

        with company_context(seeded.id):
            lifts = list(Elevator.objects.all())
            assert all(
                lift.capacity_kg is not None and lift.capacity_persons == lift.capacity_kg // 75
                for lift in lifts
            )
            by_building: dict[Any, list[str]] = {}
            for lift in lifts:
                by_building.setdefault(lift.building_id, []).append(lift.name)
            assert all(len(names) == len(set(names)) for names in by_building.values())
            notes = demo_activity.ACTIVITY["attention_notes"]
            findings = MaintenanceVisitItem.objects.filter(result=ChecklistResult.ATTENTION_NEEDED)
            assert findings.exists()
            assert all(item.note == notes[item.item] for item in findings)
            staff = {u.full_name for u in User.objects.filter(company=seeded)}
            contacts = set(CustomerContact.objects.values_list("full_name", flat=True))
            assert staff.isdisjoint(contacts)

    def test_plan_and_lift_agree(self, seeded):
        today = local_today()
        with company_context(seeded.id):
            for row in PlannedVisit.objects.filter(
                status=PlannedVisitStatus.PLANNED, manually_edited=False
            ).select_related("elevator"):
                expected = period_due(row.period, row.elevator.next_maintenance_date)
                assert row.due_date == expected, (row.elevator, row.period)
        assert today


class TestSignatures:
    """The demo's visits are signed for, or say why not (visit signature spec)."""

    @pytest.fixture
    def bucket(self, monkeypatch) -> Any:
        from core import storage
        from tests.test_attachments import FakeS3

        fake = FakeS3()
        monkeypatch.setattr(storage, "_client", lambda backend: fake)
        return fake

    def test_with_storage_completed_visits_are_signed_for_or_say_why(self, bucket, db, settings):
        settings.DEMO_SIGNATURE_IMAGES = True
        from apps.attachments.models import Attachment, AttachmentCategory

        call_command("load_address_data", path=SAMPLE_DATA)
        call_command("seed_demo_data", stdout=StringIO())
        with system_context():
            company = Company.objects.get()
        with company_context(company.id):
            completed = MaintenanceVisit.objects.filter(status=VisitStatus.COMPLETED)
            assert not completed.filter(signature_status="").exists()
            for status in ("signed", "not_present", "declined"):
                assert completed.filter(signature_status=status).exists(), status
            signed = completed.filter(signature_status="signed")
            images = Attachment.objects.filter(category=AttachmentCategory.SIGNATURE)
            assert images.count() == signed.count()
            assert {image.object_id for image in images} == {visit.pk for visit in signed}
            assert all(visit.signer_name and visit.signed_at for visit in signed)
            assert all(image.mime_type == "image/png" for image in images)
        # Run again: every visit already has its answer.
        again = demo_activity.populate_activity(company, staff_domain=demo.STAFF_DOMAIN)
        assert again.signatures == 0

    def test_without_storage_nobody_is_left_pending(self, seeded):
        # No bucket (a laptop without MinIO): a demo must never show a signature
        # that is still on its way, so those visits say nobody was there.
        with company_context(seeded.id):
            completed = MaintenanceVisit.objects.filter(status=VisitStatus.COMPLETED)
            assert not completed.filter(signature_status="").exists()
            assert not completed.filter(signature_status="signed").exists()
