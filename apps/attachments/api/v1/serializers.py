from __future__ import annotations

from rest_framework import serializers

from apps.attachments.models import Attachment, AttachmentCategory, ObjectType
from apps.attachments.services import ALLOWED_TYPES, MAX_SIZE_BYTES


class AttachmentSerializer(serializers.ModelSerializer):
    """What a client is told about a file.

    `storage_key` and `storage_backend` are absent on purpose. They are how the
    server finds the bytes, not facts about the document, and publishing them
    would tie the API contract to the layout of a bucket that is expected to
    change when the personal-data categories move provider.
    """

    uploaded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = [
            "id",
            "object_type",
            "object_id",
            "category",
            "original_filename",
            "mime_type",
            "size_bytes",
            "uploaded_by",
            "uploaded_by_name",
            "created_at",
        ]
        read_only_fields = fields

    def get_uploaded_by_name(self, attachment: Attachment) -> str:
        # The list shows who uploaded each file; without this the client would
        # fetch a user per row to render one column.
        user = attachment.uploaded_by
        return f"{user.first_name} {user.last_name}".strip() if user else ""


class UploadUrlRequestSerializer(serializers.Serializer):
    object_type = serializers.ChoiceField(choices=ObjectType.choices)
    object_id = serializers.UUIDField()
    category = serializers.ChoiceField(choices=AttachmentCategory.choices)
    mime_type = serializers.ChoiceField(choices=sorted(ALLOWED_TYPES))
    # Declared, not measured. The real size is read from storage after the
    # upload; this only avoids starting one that cannot succeed.
    size_bytes = serializers.IntegerField(min_value=1, max_value=MAX_SIZE_BYTES)


class UploadUrlResponseSerializer(serializers.Serializer):
    upload_url = serializers.URLField()
    storage_key = serializers.CharField()
    expires_in = serializers.IntegerField()
    #: The client must send exactly this `Content-Type` — it is part of the
    #: signature, so any other value is refused by storage rather than stored.
    content_type = serializers.CharField()


class AttachmentConfirmSerializer(serializers.Serializer):
    """The second half of an upload.

    Only the key and the display name: everything else — which record the file
    belongs to, its category, its size, its type — is read back from the key and
    from storage, so the two calls cannot contradict each other.
    """

    storage_key = serializers.CharField(max_length=500)
    original_filename = serializers.CharField(max_length=255)


class DownloadUrlSerializer(serializers.Serializer):
    download_url = serializers.URLField()
    expires_in = serializers.IntegerField()
