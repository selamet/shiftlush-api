"""Notifications — the "Notifications" section of
docs/superpowers/specs/2026-10-20-m2-notifications-design.md.

Three layers, tested in the order a notification travels through them: notify()
raising it (once, for the right people, in the right company), the e-mail that
may follow it (only when wanted, only after commit, in Turkish), and the API its
recipient reads it through (theirs and nobody else's).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime

import pytest
from django.conf import settings
from django.core import mail as django_mail
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import get_template, render_to_string
from django.urls import reverse
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.notifications.models import (
    EMAIL_ALWAYS,
    EMAIL_DEFAULTS,
    Notification,
    NotificationKind,
    NotificationPreference,
)
from apps.notifications.services import notify
from apps.notifications.tasks import send_notification_email
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core.context import company_context, system_context
from core.error_codes import ErrorCode
from core.mail import notification_url
from core.models import TenantMismatchError
from core.templatetags.email_format import tr_date

PASSWORD = "correct-horse-battery"
CONTRACT_ID = uuid.UUID("01900000-0000-7000-8000-000000000001")
ELEVATOR_ID = uuid.UUID("01900000-0000-7000-8000-000000000002")
FAULT_ID = uuid.UUID("01900000-0000-7000-8000-000000000003")

#: A plausible `data` for every kind, as the jobs will raise it.
SAMPLE_DATA: dict[str, dict[str, object]] = {
    NotificationKind.CONTRACT_RENEWAL_DUE: {
        "contract_number": "2026-0007",
        "customer_name": "Yıldız Sitesi Yönetimi",
        "end_date": date(2026, 11, 30),
        "auto_renew": True,
    },
    NotificationKind.CONTRACT_EXPIRED: {
        "contract_number": "2026-0007",
        "customer_name": "Yıldız Sitesi Yönetimi",
        "end_date": date(2026, 10, 19),
    },
    NotificationKind.CONTRACT_AUTO_RENEWED: {
        "contract_number": "2026-0007",
        "customer_name": "Yıldız Sitesi Yönetimi",
        "end_date": date(2026, 10, 19),
        "successor_contract_number": "2026-0042",
    },
    NotificationKind.CONTRACT_AUTO_RENEWAL_BLOCKED: {
        "contract_number": "2026-0007",
        "customer_name": "Yıldız Sitesi Yönetimi",
        "end_date": date(2026, 10, 19),
        "successor_contract_number": "2026-0042",
    },
    NotificationKind.MAINTENANCE_OVERDUE: {
        "elevator_name": "A Blok",
        "building_name": "Yıldız Sitesi",
        "next_maintenance_date": date(2026, 10, 15),
    },
    NotificationKind.VISIT_ASSIGNED: {"period": "2026-10", "count": 12},
    NotificationKind.FAULT_REPORTED: {
        "reference": "K7M2QX",
        "elevator_name": "A Blok",
        "building_name": "Yıldız Sitesi",
        "priority": "high",
        "channel": "public_qr",
        "description": "Kapı kapanmıyor",
    },
    NotificationKind.FAULT_EMERGENCY: {
        "reference": "K7M2QX",
        "elevator_name": "A Blok",
        "building_name": "Yıldız Sitesi",
        "priority": "emergency",
        "channel": "staff",
        "description": "Kabinde biri mahsur",
    },
    NotificationKind.FAULT_ASSIGNED: {
        "reference": "K7M2QX",
        "elevator_name": "A Blok",
        "building_name": "Yıldız Sitesi",
        "priority": "normal",
    },
    NotificationKind.FAULT_RESOLVED: {
        "reference": "K7M2QX",
        "elevator_name": "A Blok",
        "building_name": "Yıldız Sitesi",
        "technician_name": "Ali Usta",
    },
}

FAULT_KINDS = {
    NotificationKind.FAULT_REPORTED,
    NotificationKind.FAULT_EMERGENCY,
    NotificationKind.FAULT_ASSIGNED,
    NotificationKind.FAULT_RESOLVED,
}

SUBJECT_OF = {
    kind: ("elevator", ELEVATOR_ID)
    if kind == NotificationKind.MAINTENANCE_OVERDUE
    else ("plan", ELEVATOR_ID)
    if kind == NotificationKind.VISIT_ASSIGNED
    else ("fault", FAULT_ID)
    if kind in FAULT_KINDS
    else ("contract", CONTRACT_ID)
    for kind in NotificationKind.values
}


def make_user(company, role: str, letter: str, **extra) -> User:
    return User.objects.create_user(
        email=f"{role}-{letter}@example.com",
        password=PASSWORD,
        company=company,
        first_name=f"{role.title()}",
        last_name=letter,
        role=role,
        is_email_verified=True,
        **extra,
    )


def api_for(user: User) -> APIClient:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(user).access}")
    return client


def raise_for(recipients, kind=NotificationKind.CONTRACT_RENEWAL_DUE, dedupe_key="k1", **extra):
    subject_type, subject_id = SUBJECT_OF[kind]
    return notify(
        kind=kind,
        recipients=recipients,
        subject_type=extra.pop("subject_type", subject_type),
        subject_id=extra.pop("subject_id", subject_id),
        data=extra.pop("data", SAMPLE_DATA[kind]),
        dedupe_key=dedupe_key,
    )


@pytest.fixture
def firm(db):
    company, owner = register_company(
        legal_name="Yükseliş Asansör Ltd",
        display_name="Yükseliş",
        first_name="Nur",
        last_name="Owner",
        email="owner@example.com",
        password=PASSWORD,
    )
    admin = make_user(company, Role.ADMIN, "a")
    django_mail.outbox.clear()
    return company, owner, admin


@pytest.fixture
def other_firm(db):
    company, owner = register_company(
        legal_name="Rakip Asansör Ltd",
        display_name="Rakip",
        first_name="Can",
        last_name="Owner",
        email="owner-b@example.com",
        password=PASSWORD,
    )
    django_mail.outbox.clear()
    return company, owner


# ---------------------------------------------------------------------------
# notify()
# ---------------------------------------------------------------------------


class TestNotify:
    def test_one_notification_per_recipient(self, firm):
        company, owner, admin = firm
        with company_context(company.id):
            created = raise_for([owner, admin])

        assert {n.recipient_id for n in created} == {owner.id, admin.id}
        assert {n.company_id for n in created} == {company.id}
        with company_context(company.id):
            assert Notification.objects.count() == 2

    def test_data_is_stored_as_plain_json(self, firm):
        company, owner, _ = firm
        with company_context(company.id):
            raise_for([owner])
            stored = Notification.objects.get()
        # A date the caller held as a `date` reads back as the ISO string the
        # client and the e-mail template both parse.
        assert stored.data["end_date"] == "2026-11-30"
        assert stored.data["auto_renew"] is True

    def test_data_with_no_json_form_is_refused(self, firm):
        company, owner, _ = firm
        with company_context(company.id), pytest.raises(TypeError):
            raise_for([owner], data={"contract": object()})
        with company_context(company.id):
            assert not Notification.objects.exists()

    def test_an_unknown_kind_is_refused(self, firm):
        company, owner, _ = firm
        with company_context(company.id), pytest.raises(ValueError):
            notify(
                kind="invoice_overdue",
                recipients=[owner],
                subject_type="contract",
                subject_id=CONTRACT_ID,
                data={},
                dedupe_key="k",
            )

    def test_an_inactive_user_is_skipped(self, firm):
        company, owner, admin = firm
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        with company_context(company.id):
            created = raise_for([owner, admin])
        assert [n.recipient_id for n in created] == [owner.id]

    def test_running_twice_raises_nothing_new(self, firm):
        company, owner, admin = firm
        with company_context(company.id):
            raise_for([owner])
            again = raise_for([owner, admin])
            # The owner already had it; the admin is new to it.
            assert [n.recipient_id for n in again] == [admin.id]
            assert raise_for([owner, admin]) == []
            assert Notification.objects.count() == 2

    def test_the_same_recipient_twice_in_one_call_is_one_notification(self, firm):
        company, owner, _ = firm
        with company_context(company.id):
            created = raise_for([owner, owner])
            assert len(created) == 1
            assert Notification.objects.count() == 1

    def test_a_different_key_is_a_different_notification(self, firm):
        company, owner, _ = firm
        with company_context(company.id):
            raise_for([owner], dedupe_key="contract:1:2026-11-30")
            raise_for([owner], dedupe_key="contract:1:2027-11-30")
            assert Notification.objects.count() == 2

    def test_a_deleted_notification_does_not_block_its_key(self, firm):
        # The constraint is conditional on is_deleted=False, like every unique
        # constraint on a soft-deleted model.
        company, owner, _ = firm
        with company_context(company.id):
            (first,) = raise_for([owner])
            first.delete()
            assert len(raise_for([owner])) == 1

    def test_under_system_context_each_lands_in_its_recipients_company(self, firm, other_firm):
        company, owner, _ = firm
        other_company, other_owner = other_firm
        # How the jobs call it: across every firm at once.
        with system_context():
            created = raise_for([owner, other_owner])

        by_recipient = {n.recipient_id: n.company_id for n in created}
        assert by_recipient == {owner.id: company.id, other_owner.id: other_company.id}

    def test_inside_one_firm_it_cannot_reach_a_user_of_another(self, firm, other_firm):
        company, _, _ = firm
        _, other_owner = other_firm
        with company_context(company.id), pytest.raises(TenantMismatchError):
            raise_for([other_owner])


# ---------------------------------------------------------------------------
# E-mail
# ---------------------------------------------------------------------------


def test_every_kind_has_an_email_default():
    assert set(EMAIL_DEFAULTS) == set(NotificationKind.values)


class TestWhenAnEmailIsSent:
    def test_a_contract_kind_is_emailed_by_default_after_commit(
        self, firm, django_capture_on_commit_callbacks
    ):
        company, owner, admin = firm
        with django_capture_on_commit_callbacks(execute=True):
            with company_context(company.id):
                raise_for([owner, admin])
            # Not before the commit: a job that fails after this line must not
            # have told anybody anything.
            assert django_mail.outbox == []

        assert sorted(message.to[0] for message in django_mail.outbox) == sorted(
            [owner.email, admin.email]
        )

    def test_overdue_maintenance_is_not_emailed_by_default(
        self, firm, django_capture_on_commit_callbacks
    ):
        company, owner, _ = firm
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with company_context(company.id):
                created = raise_for([owner], kind=NotificationKind.MAINTENANCE_OVERDUE)
        assert len(created) == 1, "the in-app notification is raised regardless"
        assert callbacks == []
        assert django_mail.outbox == []

    def test_a_user_who_turned_a_kind_off_gets_no_email(
        self, firm, django_capture_on_commit_callbacks
    ):
        company, owner, admin = firm
        with company_context(company.id):
            NotificationPreference.objects.create(
                company=company, user=admin, kind=NotificationKind.CONTRACT_RENEWAL_DUE, email=False
            )
        with django_capture_on_commit_callbacks(execute=True):
            with company_context(company.id):
                raise_for([owner, admin])
        assert [message.to[0] for message in django_mail.outbox] == [owner.email]

    def test_a_user_who_turned_overdue_on_gets_it(self, firm, django_capture_on_commit_callbacks):
        company, owner, admin = firm
        with company_context(company.id):
            NotificationPreference.objects.create(
                company=company, user=admin, kind=NotificationKind.MAINTENANCE_OVERDUE, email=True
            )
        with django_capture_on_commit_callbacks(execute=True):
            with company_context(company.id):
                raise_for([owner, admin], kind=NotificationKind.MAINTENANCE_OVERDUE)
        assert [message.to[0] for message in django_mail.outbox] == [admin.email]

    def test_preferences_are_honoured_under_system_context(
        self, firm, django_capture_on_commit_callbacks
    ):
        company, owner, _ = firm
        with company_context(company.id):
            NotificationPreference.objects.create(
                company=company, user=owner, kind=NotificationKind.CONTRACT_EXPIRED, email=False
            )
        with django_capture_on_commit_callbacks(execute=True), system_context():
            raise_for([owner], kind=NotificationKind.CONTRACT_EXPIRED)
        assert django_mail.outbox == []

    def test_a_rolled_back_job_sends_nothing(self, firm, django_capture_on_commit_callbacks):
        company, owner, _ = firm
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with company_context(company.id), pytest.raises(RuntimeError):
                with transaction.atomic():
                    raise_for([owner])
                    raise RuntimeError("the job failed after raising")
        assert callbacks == []
        assert django_mail.outbox == []
        with company_context(company.id):
            assert not Notification.objects.exists()

    def test_a_second_run_sends_no_second_email(self, firm, django_capture_on_commit_callbacks):
        company, owner, _ = firm
        for _ in range(2):
            with django_capture_on_commit_callbacks(execute=True):
                with company_context(company.id):
                    raise_for([owner])
        assert len(django_mail.outbox) == 1

    def test_a_recipient_deactivated_before_the_send_gets_nothing(self, firm):
        company, owner, _ = firm
        with company_context(company.id):
            (notification,) = raise_for([owner])
        owner.is_active = False
        owner.save(update_fields=["is_active"])

        send_notification_email.call(str(notification.pk))
        assert django_mail.outbox == []

    def test_a_vanished_notification_is_not_an_error(self, db):
        send_notification_email.call(str(uuid.uuid4()))
        assert django_mail.outbox == []


def sent_for(firm, kind, django_capture_on_commit_callbacks, data=None):
    company, owner, _ = firm
    with django_capture_on_commit_callbacks(execute=True):
        with company_context(company.id):
            NotificationPreference.objects.create(
                company=company, user=owner, kind=kind, email=True
            )
            raise_for([owner], kind=kind, data=data or SAMPLE_DATA[kind])
    (message,) = django_mail.outbox
    assert isinstance(message, EmailMultiAlternatives)
    html = next(content for content, mimetype in message.alternatives if mimetype == "text/html")
    return message, html


class TestWhatTheEmailSays:
    def test_renewal_due(self, firm, django_capture_on_commit_callbacks):
        message, html = sent_for(
            firm, NotificationKind.CONTRACT_RENEWAL_DUE, django_capture_on_commit_callbacks
        )
        assert message.subject == "2026-0007 numaralı sözleşmenin süresi doluyor"
        sentence = (
            "Yıldız Sitesi Yönetimi ile yapılan 2026-0007 numaralı sözleşmenin süresi "
            "30.11.2026 tarihinde doluyor."
        )
        assert sentence in message.body
        assert sentence in html
        assert "Otomatik yenileme açık" in message.body
        url = f"{settings.FRONTEND_URL}/contracts/{CONTRACT_ID}"
        assert url in message.body
        assert f'href="{url}"' in html
        assert "Merhaba Nur," in message.body

    def test_renewal_due_without_auto_renew_says_so(self, firm, django_capture_on_commit_callbacks):
        data = {**SAMPLE_DATA[NotificationKind.CONTRACT_RENEWAL_DUE], "auto_renew": False}
        message, _ = sent_for(
            firm, NotificationKind.CONTRACT_RENEWAL_DUE, django_capture_on_commit_callbacks, data
        )
        assert "Otomatik yenileme kapalı" in message.body

    def test_expired(self, firm, django_capture_on_commit_callbacks):
        message, _ = sent_for(
            firm, NotificationKind.CONTRACT_EXPIRED, django_capture_on_commit_callbacks
        )
        assert message.subject == "2026-0007 numaralı sözleşmenin süresi doldu"
        assert "19.10.2026 tarihinde doldu" in message.body
        assert "sözleşmesiz" in message.body

    def test_auto_renewed_names_the_successor(self, firm, django_capture_on_commit_callbacks):
        message, _ = sent_for(
            firm, NotificationKind.CONTRACT_AUTO_RENEWED, django_capture_on_commit_callbacks
        )
        assert "otomatik olarak yenilendi" in message.subject
        assert "Yeni sözleşme 2026-0042 numarasıyla" in message.body

    def test_auto_renewal_blocked_explains_why_and_what_to_do(
        self, firm, django_capture_on_commit_callbacks
    ):
        message, _ = sent_for(
            firm, NotificationKind.CONTRACT_AUTO_RENEWAL_BLOCKED, django_capture_on_commit_callbacks
        )
        assert "yenilenemedi" in message.subject
        assert "KDV oranı tanımlı olmadığı için" in message.body
        assert "2026-0042 numarasıyla taslak olarak oluşturuldu" in message.body

    def test_maintenance_overdue_links_to_the_elevator(
        self, firm, django_capture_on_commit_callbacks
    ):
        message, html = sent_for(
            firm, NotificationKind.MAINTENANCE_OVERDUE, django_capture_on_commit_callbacks
        )
        assert message.subject == "A Blok asansörünün bakımı gecikti"
        assert (
            "A Blok (Yıldız Sitesi) asansörünün bakımı 15.10.2026 tarihinde yapılmalıydı."
            in message.body
        )
        url = f"{settings.FRONTEND_URL}/elevators/{ELEVATOR_ID}"
        assert url in message.body
        assert f'href="{url}"' in html

    def test_a_name_is_escaped_in_html_and_left_alone_in_text(
        self, firm, django_capture_on_commit_callbacks
    ):
        data = {
            **SAMPLE_DATA[NotificationKind.CONTRACT_EXPIRED],
            "customer_name": "Ak & Kara <Yönetim>",
        }
        message, html = sent_for(
            firm, NotificationKind.CONTRACT_EXPIRED, django_capture_on_commit_callbacks, data
        )
        assert "Ak & Kara <Yönetim> ile yapılan" in message.body
        assert "Ak &amp; Kara &lt;Yönetim&gt;" in html
        assert "<Yönetim>" not in html

    def test_a_fact_named_url_cannot_redirect_the_button(
        self, firm, django_capture_on_commit_callbacks
    ):
        data = {**SAMPLE_DATA[NotificationKind.CONTRACT_EXPIRED], "url": "https://evil.example"}
        message, html = sent_for(
            firm, NotificationKind.CONTRACT_EXPIRED, django_capture_on_commit_callbacks, data
        )
        assert "evil.example" not in message.body
        assert "evil.example" not in html


LEAKED_SYNTAX = re.compile(r"\{[#{%]")


def render_kind(kind: str, part: str) -> str:
    subject_type, subject_id = SUBJECT_OF[kind]
    data = {
        key: value.isoformat() if isinstance(value, date) else value
        for key, value in SAMPLE_DATA[kind].items()
    }
    return render_to_string(
        f"email/notification/{part}",
        {
            **data,
            "first_name": "Nur",
            "kind": kind,
            "subject_type": subject_type,
            "url": notification_url(subject_type=subject_type, subject_id=str(subject_id)),
            "kind_subject_template": f"email/notification/kinds/{kind}_subject.txt",
            "kind_body_template": f"email/notification/kinds/{kind}_body.txt",
        },
    )


@pytest.mark.parametrize("kind", NotificationKind.values)
class TestEveryKindHasAnEmail:
    """Adding a kind is adding two partials; this fails until both exist."""

    def test_both_partials_exist(self, kind):
        get_template(f"email/notification/kinds/{kind}_subject.txt")
        get_template(f"email/notification/kinds/{kind}_body.txt")

    @pytest.mark.parametrize("part", ["subject.txt", "body.txt", "body.html"])
    def test_no_template_syntax_reaches_the_reader(self, kind, part):
        rendered = render_kind(kind, part)
        assert LEAKED_SYNTAX.search(rendered) is None

    def test_the_subject_is_one_line(self, kind):
        subject = render_kind(kind, "subject.txt").strip()
        assert subject
        assert "\n" not in subject

    def test_the_html_starts_as_a_document(self, kind):
        assert render_kind(kind, "body.html").lstrip().lower().startswith("<!doctype html>")

    def test_no_date_is_left_in_iso_form(self, kind):
        body = render_kind(kind, "body.txt")
        assert re.search(r"\d{4}-\d{2}-\d{2}", body.replace(str(CONTRACT_ID), "")) is None
        # Only the kinds that carry a day; a period reads as a month name.
        if any(isinstance(value, date) for value in SAMPLE_DATA[kind].values()):
            assert re.search(r"\d{2}\.\d{2}\.\d{4}", body)


class TestTurkishDates:
    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ("2026-11-30", "30.11.2026"),
            (date(2026, 1, 5), "05.01.2026"),
            # 22:30 UTC is already the next day in Istanbul.
            (datetime(2026, 3, 1, 22, 30, tzinfo=UTC), "02.03.2026"),
            ("2026-03-01T22:30:00Z", "02.03.2026"),
            ("not a date", "not a date"),
            (None, ""),
        ],
    )
    def test_tr_date(self, value, shown):
        assert tr_date(value) == shown


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def seed(company, recipient, count=1, prefix="k"):
    with company_context(company.id):
        return [
            raise_for(
                [recipient], kind=NotificationKind.CONTRACT_EXPIRED, dedupe_key=f"{prefix}{i}"
            )[0]
            for i in range(count)
        ]


class TestNotificationList:
    def test_only_the_callers_own_newest_first(self, firm):
        company, owner, admin = firm
        mine = seed(company, owner, count=3)
        seed(company, admin, count=2, prefix="a")

        response = api_for(owner).get(reverse("notification-list"))
        assert response.status_code == 200
        rows = response.data["results"]
        assert [row["id"] for row in rows] == [str(n.pk) for n in reversed(mine)]
        assert set(rows[0]) == {
            "id",
            "kind",
            "subject_type",
            "subject_id",
            "data",
            "read_at",
            "created_at",
        }
        assert rows[0]["data"]["contract_number"] == "2026-0007"
        assert response.data["pagination"]["total"] == 3

    def test_unread_filter(self, firm):
        company, owner, _ = firm
        first, second = seed(company, owner, count=2)
        client = api_for(owner)
        client.post(reverse("notification-read", args=[first.pk]))

        unread = client.get(reverse("notification-list"), {"unread": "true"}).data["results"]
        assert [row["id"] for row in unread] == [str(second.pk)]
        read = client.get(reverse("notification-list"), {"unread": "false"}).data["results"]
        assert [row["id"] for row in read] == [str(first.pk)]

    def test_unread_count(self, firm):
        company, owner, admin = firm
        first, *_ = seed(company, owner, count=3)
        seed(company, admin, count=4, prefix="a")
        client = api_for(owner)
        client.post(reverse("notification-read", args=[first.pk]))

        response = client.get(reverse("notification-unread-count"))
        assert response.status_code == 200
        assert response.data == {"count": 2}

    def test_signed_out_is_refused(self, db):
        response = APIClient().get(reverse("notification-list"))
        assert response.status_code == 401


class TestMarkingRead:
    def test_read_is_idempotent_and_keeps_the_first_time(self, firm):
        company, owner, _ = firm
        (notification,) = seed(company, owner)
        client = api_for(owner)
        url = reverse("notification-read", args=[notification.pk])

        first = client.post(url)
        assert first.status_code == 200
        assert first.data["read_at"] is not None
        second = client.post(url)
        assert second.status_code == 200
        assert second.data["read_at"] == first.data["read_at"]

    def test_a_colleagues_notification_is_not_found(self, firm):
        company, owner, admin = firm
        (theirs,) = seed(company, admin)

        response = api_for(owner).post(reverse("notification-read", args=[theirs.pk]))
        # 404, not 403: the owner runs the firm, not their colleagues' inboxes,
        # and a 403 would confirm the id is real.
        assert response.status_code == 404
        assert response.data["error"]["code"] == ErrorCode.NOT_FOUND.value
        with company_context(company.id):
            theirs.refresh_from_db()
        assert theirs.read_at is None

    def test_read_all_marks_only_the_callers_own(self, firm):
        company, owner, admin = firm
        seed(company, owner, count=3)
        (theirs,) = seed(company, admin, prefix="a")
        client = api_for(owner)

        response = client.post(reverse("notification-read-all"))
        assert response.status_code == 200
        assert response.data == {"count": 3}
        assert client.post(reverse("notification-read-all")).data == {"count": 0}
        assert client.get(reverse("notification-unread-count")).data == {"count": 0}
        with company_context(company.id):
            theirs.refresh_from_db()
        assert theirs.read_at is None

    def test_reading_is_not_an_audit_event(self, firm):
        company, owner, _ = firm
        (notification,) = seed(company, owner)
        api_for(owner).post(reverse("notification-read", args=[notification.pk]))
        assert not AuditLog.objects.filter(table_name="notification").exists()


def defaults() -> list[dict[str, object]]:
    return [
        {"kind": kind, "email": EMAIL_DEFAULTS[kind], "locked": kind in EMAIL_ALWAYS}
        for kind in NotificationKind.values
    ]


class TestPreferences:
    def test_every_kind_is_listed_with_its_default(self, firm):
        _, owner, _ = firm
        response = api_for(owner).get(reverse("notification-preferences"))
        assert response.status_code == 200
        assert response.data == defaults()
        by_kind = {row["kind"]: row["email"] for row in response.data}
        assert by_kind[NotificationKind.CONTRACT_RENEWAL_DUE] is True
        assert by_kind[NotificationKind.MAINTENANCE_OVERDUE] is False

    def test_put_round_trips(self, firm):
        company, owner, _ = firm
        client = api_for(owner)
        # Every kind flipped, except the ones that cannot be turned off.
        wanted = [
            {
                "kind": kind,
                "email": True if kind in EMAIL_ALWAYS else not EMAIL_DEFAULTS[kind],
                "locked": kind in EMAIL_ALWAYS,
            }
            for kind in NotificationKind.values
        ]

        response = client.put(reverse("notification-preferences"), wanted, format="json")
        assert response.status_code == 200, response.data
        assert response.data == wanted
        assert client.get(reverse("notification-preferences")).data == wanted
        with company_context(company.id):
            assert NotificationPreference.objects.filter(user=owner).count() == len(wanted)

    def test_a_kind_left_out_returns_to_its_default(self, firm):
        _, owner, _ = firm
        client = api_for(owner)
        overdue_on = [{"kind": NotificationKind.MAINTENANCE_OVERDUE, "email": True}]
        client.put(reverse("notification-preferences"), overdue_on, format="json")

        response = client.put(reverse("notification-preferences"), [], format="json")
        assert response.status_code == 200
        assert response.data == defaults()

    def test_an_unknown_kind_is_refused(self, firm):
        company, owner, _ = firm
        response = api_for(owner).put(
            reverse("notification-preferences"),
            [{"kind": "invoice_overdue", "email": True}],
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
        assert response.data["error"]["details"] == [{"field": "0.kind", "code": "invalid_choice"}]
        with company_context(company.id):
            assert not NotificationPreference.objects.exists()

    def test_the_same_kind_twice_is_refused(self, firm):
        _, owner, _ = firm
        kind = NotificationKind.CONTRACT_EXPIRED
        response = api_for(owner).put(
            reverse("notification-preferences"),
            [{"kind": kind, "email": True}, {"kind": kind, "email": False}],
            format="json",
        )
        assert response.status_code == 400

    def test_a_preference_change_is_audited(self, firm):
        _, owner, _ = firm
        api_for(owner).put(
            reverse("notification-preferences"),
            [{"kind": NotificationKind.CONTRACT_EXPIRED, "email": False}],
            format="json",
        )
        assert AuditLog.objects.filter(table_name="notification_preference").exists()

    def test_one_users_choice_is_not_anothers(self, firm):
        _, owner, admin = firm
        api_for(owner).put(
            reverse("notification-preferences"),
            [{"kind": NotificationKind.CONTRACT_EXPIRED, "email": False}],
            format="json",
        )
        assert api_for(admin).get(reverse("notification-preferences")).data == defaults()


@pytest.mark.parametrize("role", [Role.ACCOUNTANT, Role.TECHNICIAN, Role.OPERATIONS])
def test_every_role_uses_its_own_notifications(firm, role):
    company, _, _ = firm
    user = make_user(company, role, "r")
    (notification,) = seed(company, user)
    client = api_for(user)

    listed = client.get(reverse("notification-list"))
    assert listed.status_code == 200
    assert [row["id"] for row in listed.data["results"]] == [str(notification.pk)]
    assert client.get(reverse("notification-unread-count")).data == {"count": 1}
    assert client.post(reverse("notification-read", args=[notification.pk])).status_code == 200
    assert client.post(reverse("notification-read-all")).status_code == 200
    assert client.get(reverse("notification-preferences")).status_code == 200
    put = client.put(reverse("notification-preferences"), defaults(), format="json")
    assert put.status_code == 200


class TestAcrossFirms:
    def test_another_firms_notifications_never_appear(self, firm, other_firm):
        company, owner, _ = firm
        other_company, other_owner = other_firm
        (ours,) = seed(company, owner)
        (theirs,) = seed(other_company, other_owner)
        client = api_for(owner)

        rows = client.get(reverse("notification-list")).data["results"]
        assert [row["id"] for row in rows] == [str(ours.pk)]
        assert client.get(reverse("notification-unread-count")).data == {"count": 1}

        response = client.post(reverse("notification-read", args=[theirs.pk]))
        assert response.status_code == 404
        assert client.post(reverse("notification-read-all")).data == {"count": 1}
        with system_context():
            assert Notification.unscoped.get(pk=theirs.pk).read_at is None

    def test_preferences_stay_inside_their_firm(self, firm, other_firm):
        _, owner, _ = firm
        _, other_owner = other_firm
        api_for(owner).put(
            reverse("notification-preferences"),
            [{"kind": NotificationKind.CONTRACT_EXPIRED, "email": False}],
            format="json",
        )
        assert api_for(other_owner).get(reverse("notification-preferences")).data == defaults()


class TestAKindThatIsAlwaysEmailed:
    """fault_emergency: a trapped passenger is not a preference (M4)."""

    def test_it_is_emailed_even_to_somebody_who_turned_it_off(
        self, firm, django_capture_on_commit_callbacks
    ):
        company, owner, _ = firm
        # Written straight to the table: the API refuses to store this, but a row
        # from before the kind was locked, or a hand edit, must not win either.
        with company_context(company.id):
            NotificationPreference.objects.create(
                company=company, user=owner, kind=NotificationKind.FAULT_EMERGENCY, email=False
            )
        with django_capture_on_commit_callbacks(execute=True):
            with company_context(company.id):
                raise_for([owner], kind=NotificationKind.FAULT_EMERGENCY)
        assert [message.to[0] for message in django_mail.outbox] == [owner.email]
        assert "mahsur kalan" in django_mail.outbox[0].subject
        assert f"/faults/{FAULT_ID}" in django_mail.outbox[0].body

    def test_it_is_listed_as_locked_and_on(self, firm):
        _, owner, _ = firm
        rows = api_for(owner).get(reverse("notification-preferences")).data
        (emergency,) = [row for row in rows if row["kind"] == NotificationKind.FAULT_EMERGENCY]
        assert emergency == {
            "kind": NotificationKind.FAULT_EMERGENCY,
            "email": True,
            "locked": True,
        }
        assert all(row["locked"] is False for row in rows if row is not emergency)

    def test_turning_it_off_is_refused(self, firm):
        company, owner, _ = firm
        response = api_for(owner).put(
            reverse("notification-preferences"),
            [{"kind": NotificationKind.FAULT_EMERGENCY, "email": False}],
            format="json",
        )
        assert response.status_code == 400
        assert response.data["error"]["details"] == [
            {"field": "0.email", "code": ErrorCode.NOTIFICATION_EMAIL_LOCKED.value}
        ]
        with company_context(company.id):
            assert not NotificationPreference.objects.exists()
