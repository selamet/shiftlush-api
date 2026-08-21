"""Attachments.

The property under test throughout: what the client says about a file is a
request, and what storage reports is the record. Everything that goes wrong with
direct-to-bucket uploads goes wrong at the seam between those two.

The bucket is faked at the boto3 client, not at `core.storage`, so the module
under test still builds the real parameters — which is where the interesting
details live: the content type inside the signature, the disposition header, the
404 mapping.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.attachments.services import confirm_upload, link_attachment, purge_detached_objects
from apps.customers.models import Customer, CustomerType
from apps.elevators.models import Elevator
from apps.properties.models import Building, BuildingType
from apps.users.models import Role, User
from apps.users.services import issue_tokens, register_company
from core import storage
from core.context import company_context, system_context

PASSWORD = "correct-horse-battery"
JPEG = "image/jpeg"
ONE_MB = 1024 * 1024


class FakeS3:
    """Enough of the S3 API to exercise core.storage honestly."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[int, str]] = {}
        self.signed: list[dict] = []
        self.deleted: list[tuple[str, str]] = []

    def put(self, bucket: str, key: str, size: int, content_type: str) -> None:
        """Stand in for the client PUTting to the signed URL."""
        self.objects[(bucket, key)] = (size, content_type)

    def generate_presigned_url(self, operation: str, Params: dict, ExpiresIn: int) -> str:
        self.signed.append({"operation": operation, **Params, "expires_in": ExpiresIn})
        return f"https://bucket.test/{Params['Key']}?op={operation}&e={ExpiresIn}"

    def head_object(self, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        size, content_type = self.objects[(Bucket, Key)]
        return {"ContentLength": size, "ContentType": content_type}

    def delete_object(self, Bucket: str, Key: str) -> dict:
        self.objects.pop((Bucket, Key), None)
        self.deleted.append((Bucket, Key))
        return {}

    def head_bucket(self, Bucket: str) -> dict:
        return {}


@pytest.fixture
def bucket(monkeypatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(storage, "_client", lambda backend: fake)
    return fake


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
            company=company, type=CustomerType.CORPORATE, legal_name="A customer"
        )
        building = Building.objects.create(
            company=company,
            customer=customer,
            name="A Blok",
            type=BuildingType.RESIDENTIAL,
            address_note="Test",
        )
        elevator = Elevator.objects.create(company=company, building=building, name="Left")
    return company, owner, customer, elevator


def ticket_for(client: APIClient, elevator, **overrides):
    payload = {
        "object_type": ObjectType.ELEVATOR,
        "object_id": str(elevator.id),
        "category": AttachmentCategory.PHOTO,
        "mime_type": JPEG,
        "size_bytes": ONE_MB,
    } | overrides
    return client.post(reverse("attachment-upload-url"), payload, format="json")


def uploaded(bucket: FakeS3, key: str, size: int = ONE_MB, content_type: str = JPEG) -> None:
    bucket.put("shiftlush-dev", key, size, content_type)


class TestAskingForAnUploadUrl:
    def test_a_url_is_returned_without_creating_a_record(self, firm, bucket):
        _, owner, _, elevator = firm
        response = ticket_for(api_for(owner), elevator)

        assert response.status_code == 200
        assert response.data["upload_url"].startswith("https://")
        # A row for a file that was never uploaded shows in lists, cannot be
        # downloaded, and nothing ever cleans it up.
        assert not Attachment.unscoped.exists()

    def test_the_content_type_is_part_of_the_signature(self, firm, bucket):
        _, owner, _, elevator = firm
        ticket_for(api_for(owner), elevator)

        signed = bucket.signed[-1]
        assert signed["operation"] == "put_object"
        # Without this a client may upload anything at all to a URL it was given
        # for a photograph.
        assert signed["ContentType"] == JPEG

    def test_the_users_filename_never_reaches_the_key(self, firm, bucket):
        _, owner, _, elevator = firm
        response = ticket_for(api_for(owner), elevator)

        key = response.data["storage_key"]
        assert key.endswith(".jpg")
        assert str(elevator.id) in key
        # The key is generated from the company, the target and a UUIDv7. The
        # client never gets to influence where an object lands.
        assert key.split("/")[-1].removesuffix(".jpg").count("-") == 4

    def test_an_unsupported_type_is_refused(self, firm, bucket):
        _, owner, _, elevator = firm
        response = ticket_for(api_for(owner), elevator, mime_type="application/zip")
        assert response.status_code == 400

    def test_a_file_over_the_limit_is_refused_before_it_is_uploaded(self, firm, bucket):
        _, owner, _, elevator = firm
        response = ticket_for(api_for(owner), elevator, size_bytes=11 * ONE_MB)
        # Cheap to refuse here; expensive to discover after 11 MB have crossed a
        # mobile connection.
        assert response.status_code == 400

    def test_a_target_in_another_company_is_not_found(self, firm, bucket):
        _, owner, _, _ = firm
        response = ticket_for(
            api_for(owner), type("X", (), {"id": uuid.uuid4()})(), object_type=ObjectType.ELEVATOR
        )
        # 404, not 403: distinguishing "not yours" from "does not exist" turns
        # this endpoint into a way to count another firm's elevators.
        assert response.status_code == 404


class TestConfirming:
    def test_a_confirmed_upload_becomes_a_record(self, firm, bucket):
        _, owner, _, elevator = firm
        client = api_for(owner)
        key = ticket_for(client, elevator).data["storage_key"]
        uploaded(bucket, key)

        response = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "Kabin.jpg"},
            format="json",
        )

        assert response.status_code == 201
        assert response.data["original_filename"] == "Kabin.jpg"
        assert response.data["object_id"] == str(elevator.id)

    def test_the_recorded_size_comes_from_storage_not_from_the_client(self, firm, bucket):
        _, owner, _, elevator = firm
        client = api_for(owner)
        # The client asked for permission to upload 1 MB...
        key = ticket_for(client, elevator, size_bytes=ONE_MB).data["storage_key"]
        # ...and uploaded 4.
        uploaded(bucket, key, size=4 * ONE_MB)

        response = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "Kabin.jpg"},
            format="json",
        )
        assert response.data["size_bytes"] == 4 * ONE_MB

    def test_an_oversized_upload_is_refused_and_the_object_removed(self, firm, bucket):
        _, owner, _, elevator = firm
        client = api_for(owner)
        key = ticket_for(client, elevator).data["storage_key"]
        uploaded(bucket, key, size=40 * ONE_MB)

        response = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "Huge.jpg"},
            format="json",
        )

        assert response.status_code == 422
        assert response.data["error"]["code"] == "FILE_TOO_LARGE"
        # There is no row, so the sweeper will never see it. Deleting now is the
        # only moment anything knows this object exists.
        assert ("shiftlush-dev", key) in bucket.deleted

    def test_an_upload_that_never_landed_says_so(self, firm, bucket):
        _, owner, _, elevator = firm
        client = api_for(owner)
        key = ticket_for(client, elevator).data["storage_key"]

        response = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "Missing.jpg"},
            format="json",
        )
        assert response.status_code == 422
        # Distinct from a validation error: the client should retry the PUT, not
        # the POST.
        assert response.data["error"]["code"] == "UPLOAD_NOT_COMPLETED"

    def test_confirming_twice_returns_the_same_record(self, firm, bucket):
        _, owner, _, elevator = firm
        client = api_for(owner)
        key = ticket_for(client, elevator).data["storage_key"]
        uploaded(bucket, key)
        body = {"storage_key": key, "original_filename": "Kabin.jpg"}

        first = client.post(reverse("attachment-list"), body, format="json")
        second = client.post(reverse("attachment-list"), body, format="json")

        # No idempotency header needed: a storage key identifies one object, so
        # the call is idempotent by construction.
        assert first.data["id"] == second.data["id"]
        assert Attachment.unscoped.count() == 1

    def test_a_key_from_another_company_is_not_found(self, firm, bucket):
        _, owner, _, elevator = firm
        forged = f"{uuid.uuid4()}/elevator/{elevator.id}/photo/{uuid.uuid4()}.jpg"
        uploaded(bucket, forged)

        response = api_for(owner).post(
            reverse("attachment-list"),
            {"storage_key": forged, "original_filename": "Theirs.jpg"},
            format="json",
        )
        assert response.status_code == 404

    def test_a_forged_key_has_nothing_behind_it(self, firm, bucket):
        company, owner, _, elevator = firm
        # Correctly shaped, correct company — but never signed by this server,
        # so no object was ever written under it.
        forged = f"{company.id}/elevator/{elevator.id}/signed_contract/{uuid.uuid4()}.pdf"

        response = api_for(owner).post(
            reverse("attachment-list"),
            {"storage_key": forged, "original_filename": "Forged.pdf"},
            format="json",
        )
        assert response.status_code == 422


class TestDownloading:
    def test_a_download_url_is_signed_for_this_file_only(self, firm, bucket):
        _, owner, _, elevator = firm
        client = api_for(owner)
        key = ticket_for(client, elevator).data["storage_key"]
        uploaded(bucket, key)
        created = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "Kabin.jpg"},
            format="json",
        )

        response = client.get(reverse("attachment-download-url", args=[created.data["id"]]))

        assert response.status_code == 200
        assert response.data["expires_in"] == 300
        assert bucket.signed[-1]["operation"] == "get_object"

    def test_the_response_is_forced_to_download_rather_than_render(self, firm, bucket):
        _, owner, _, elevator = firm
        client = api_for(owner)
        key = ticket_for(client, elevator).data["storage_key"]
        uploaded(bucket, key)
        created = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "Asansör Raporu.pdf"},
            format="json",
        )

        client.get(reverse("attachment-download-url", args=[created.data["id"]]))

        disposition = bucket.signed[-1]["ResponseContentDisposition"]
        # Inline rendering from a bucket means a file that lied about its type
        # runs as a page on a domain the user trusts.
        assert disposition.startswith("attachment;")
        # Turkish filenames survive: the ASCII form is only a fallback.
        assert "filename*=UTF-8''Asans%C3%B6r%20Raporu.pdf" in disposition

    def test_another_companys_file_is_not_found(self, firm, bucket):
        company, owner, _, elevator = firm
        client = api_for(owner)
        key = ticket_for(client, elevator).data["storage_key"]
        uploaded(bucket, key)
        created = client.post(
            reverse("attachment-list"),
            {"storage_key": key, "original_filename": "Kabin.jpg"},
            format="json",
        )

        with system_context():
            _, stranger = register_company(
                legal_name="Other Ltd",
                display_name="Other",
                first_name="O",
                last_name="Ther",
                email="other@example.com",
                password=PASSWORD,
            )

        response = api_for(stranger).get(
            reverse("attachment-download-url", args=[created.data["id"]])
        )
        assert response.status_code == 404


class TestWhoSeesWhat:
    @pytest.fixture
    def technician(self, firm):
        company, _, customer, _ = firm
        with system_context():
            user = User.objects.create_user(
                email="tech@example.com",
                password=PASSWORD,
                company=company,
                first_name="T",
                last_name="Ech",
                role=Role.TECHNICIAN,
            )
        return user

    def _attach(self, firm, bucket, object_type: str, object_id) -> Attachment:
        company, owner, _, _ = firm
        key = f"{company.id}/{object_type}/{object_id}/photo/{uuid.uuid4()}.jpg"
        uploaded(bucket, key)
        with company_context(company.id):
            return confirm_upload(
                company_id=company.id,
                uploaded_by=owner,
                storage_key=key,
                original_filename="Kabin.jpg",
            )

    def test_a_technician_sees_only_assigned_customers_files(self, firm, bucket, technician):
        company, owner, customer, elevator = firm
        with system_context():
            other = Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="Not assigned"
            )
        mine = self._attach(firm, bucket, ObjectType.ELEVATOR, elevator.id)
        theirs = self._attach(firm, bucket, ObjectType.CUSTOMER, other.id)
        technician.customer_assignments.create(company=company, customer=customer)

        listed = api_for(technician).get(reverse("attachment-list")).data["results"]
        ids = {row["id"] for row in listed}

        # The company boundary would have let both through; the assignment
        # boundary is what keeps a contract for a customer they never visit out
        # of the list.
        assert str(mine.id) in ids
        assert str(theirs.id) not in ids

    def test_a_technician_may_not_upload_in_phase_one(self, firm, bucket, technician):
        _, _, _, elevator = firm
        assert ticket_for(api_for(technician), elevator).status_code == 403

    def test_an_accountant_may_not_read_files(self, firm, bucket):
        company, _, _, _ = firm
        with system_context():
            accountant = User.objects.create_user(
                email="acc@example.com",
                password=PASSWORD,
                company=company,
                first_name="A",
                last_name="Cc",
                role=Role.ACCOUNTANT,
            )
        assert api_for(accountant).get(reverse("attachment-list")).status_code == 403


class TestDeletionAndRetention:
    def _attachment(self, firm, bucket) -> Attachment:
        company, owner, _, elevator = firm
        key = f"{company.id}/elevator/{elevator.id}/photo/{uuid.uuid4()}.jpg"
        uploaded(bucket, key)
        with company_context(company.id):
            return confirm_upload(
                company_id=company.id,
                uploaded_by=owner,
                storage_key=key,
                original_filename="Kabin.jpg",
            )

    def test_deleting_leaves_the_object_alone_for_now(self, firm, bucket):
        _, owner, _, _ = firm
        attachment = self._attachment(firm, bucket)

        api_for(owner).delete(reverse("attachment-detail", args=[attachment.id]))

        # Read through `unscoped`: the tenant manager sees nothing outside a
        # request, and a soft-deleted row is invisible to it in any case.
        attachment = Attachment.unscoped.get(pk=attachment.pk)
        assert attachment.is_deleted
        # A file deleted by mistake on Monday is usually noticed by Friday.
        assert attachment.storage_key
        assert not bucket.deleted

    def test_the_sweeper_removes_bytes_but_keeps_the_row(self, firm, bucket):
        attachment = self._attachment(firm, bucket)
        key = attachment.storage_key
        with company_context(attachment.company_id):
            attachment.delete()
        Attachment.unscoped.filter(pk=attachment.pk).update(
            deleted_at=timezone.now() - timedelta(days=31)
        )

        assert purge_detached_objects() == 1

        attachment = Attachment.unscoped.get(pk=attachment.pk)
        # The row survives because the audit trail refers to it; only the bytes
        # and the pointer to them are gone.
        assert attachment.storage_key == ""
        assert ("shiftlush-dev", key) in bucket.deleted

    def test_a_recent_deletion_is_left_alone(self, firm, bucket):
        attachment = self._attachment(firm, bucket)
        with company_context(attachment.company_id):
            attachment.delete()

        assert purge_detached_objects() == 0

    def test_a_purged_attachment_cannot_produce_a_download_url(self, firm, bucket):
        _, owner, _, _ = firm
        attachment = self._attachment(firm, bucket)
        Attachment.unscoped.filter(pk=attachment.pk).update(storage_key="")

        response = api_for(owner).get(reverse("attachment-download-url", args=[attachment.id]))
        assert response.status_code == 422


class TestLinking:
    def test_a_record_may_only_point_at_its_own_file(self, firm, bucket):
        company, owner, customer, elevator = firm
        key = f"{company.id}/elevator/{elevator.id}/photo/{uuid.uuid4()}.jpg"
        uploaded(bucket, key)
        with company_context(company.id):
            attachment = confirm_upload(
                company_id=company.id,
                uploaded_by=owner,
                storage_key=key,
                original_filename="Kabin.jpg",
            )

        from core.exceptions import BusinessRuleError

        # The file belongs to the elevator; pointing the customer at it would
        # make a convenience field into a way to surface the wrong document.
        with pytest.raises(BusinessRuleError), company_context(company.id):
            link_attachment(customer, attachment, "logo")
