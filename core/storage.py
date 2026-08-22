"""Object storage: signed URLs, and nothing else.

File bytes never pass through this application. The client asks for a signed
URL, uploads straight to the bucket, and confirms afterwards; downloads work the
same way in reverse. Proxying a 10 MB upload through Django holds a worker for
the length of the client's connection, and a technician standing in a lift shaft
does not have a good connection.

Signing is a local HMAC — no request leaves the process — so handing out an
upload URL is cheap and works even while the bucket is unreachable. Only `stat`
and `delete` actually talk to storage.

Every call takes the backend explicitly rather than reading the default. That is
what makes `attachment.storage_backend` worth storing: a file written to R2 last
year keeps resolving through R2 after new uploads have moved elsewhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

logger = logging.getLogger(__name__)


class ObjectNotFound(Exception):
    """No object under that key in that bucket."""


@dataclass(frozen=True)
class StoredObject:
    """What storage says about an object, as opposed to what a client claimed."""

    size_bytes: int
    content_type: str


_clients: dict[str, Any] = {}


@receiver(setting_changed)
def _drop_cached_clients(**kwargs: Any) -> None:
    # Clients are built from settings, so a test that overrides a bucket has to
    # get a new one. Without this the first test to touch storage would pin the
    # configuration for the whole run.
    if kwargs.get("setting") in {"STORAGE_BACKENDS", "DEFAULT_STORAGE_BACKEND"}:
        _clients.clear()


def _client(backend: str) -> Any:
    if backend in _clients:
        return _clients[backend]

    config = settings.STORAGE_BACKENDS.get(backend)
    if not config or not config.get("bucket"):
        raise ImproperlyConfigured(
            f"Storage backend {backend!r} is not configured. A row referring to a "
            f"backend the process cannot reach is a deployment mistake, not a "
            f"runtime condition to recover from."
        )

    _clients[backend] = boto3.client(
        "s3",
        endpoint_url=config["endpoint_url"] or None,
        aws_access_key_id=config["access_key_id"],
        aws_secret_access_key=config["secret_access_key"],
        region_name=config["region"],
        config=Config(
            # v2 signatures are refused by R2 and deprecated everywhere else.
            signature_version="s3v4",
            # Path style keeps MinIO working without wildcard DNS locally.
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    return _clients[backend]


def _bucket(backend: str) -> str:
    return str(settings.STORAGE_BACKENDS[backend]["bucket"])


def upload_url(backend: str, key: str, content_type: str, ttl: int | None = None) -> str:
    """A URL the client may PUT one object to, once, until it expires.

    The content type is part of the signature: a client that uploads with a
    different `Content-Type` header is rejected by storage rather than quietly
    storing a PDF labelled as an image.
    """
    return str(
        _client(backend).generate_presigned_url(
            "put_object",
            Params={"Bucket": _bucket(backend), "Key": key, "ContentType": content_type},
            ExpiresIn=ttl if ttl is not None else settings.UPLOAD_URL_TTL_SECONDS,
        )
    )


def _content_disposition(filename: str) -> str:
    """RFC 5987, because these filenames are Turkish.

    A bare `filename="..."` holding non-ASCII characters is either mangled or
    dropped entirely depending on the browser, so the ASCII form is only a
    fallback and the real name travels percent-encoded in `filename*`.
    """
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


def download_url(
    backend: str, key: str, filename: str, content_type: str, ttl: int | None = None
) -> str:
    """A short-lived URL for reading one object.

    The response headers are pinned in the signature rather than left to
    whatever the object carries. `Content-Disposition: attachment` means a file
    that lied about its type at upload time is downloaded, not rendered — an
    HTML file served inline from a bucket runs as a page, and users trust the
    domain it came from.
    """
    return str(
        _client(backend).generate_presigned_url(
            "get_object",
            Params={
                "Bucket": _bucket(backend),
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": _content_disposition(filename),
            },
            ExpiresIn=ttl if ttl is not None else settings.DOWNLOAD_URL_TTL_SECONDS,
        )
    )


def stat(backend: str, key: str) -> StoredObject:
    """What is actually in the bucket, for checking against what was promised."""
    try:
        head = _client(backend).head_object(Bucket=_bucket(backend), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise ObjectNotFound(key) from exc
        raise
    return StoredObject(
        size_bytes=int(head["ContentLength"]),
        content_type=str(head.get("ContentType", "application/octet-stream")),
    )


def fetch(backend: str, key: str) -> tuple[bytes, str]:
    """Read an object into memory.

    The one case that needs this is embedding a company logo in a printed label,
    where a signed URL would not help: the PDF renderer must not depend on the
    network to produce a file that is needed in a basement.
    """
    try:
        response = _client(backend).get_object(Bucket=_bucket(backend), Key=key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            raise ObjectNotFound(key) from exc
        raise
    body = response["Body"].read()
    return body, str(response.get("ContentType", "application/octet-stream"))


def delete(backend: str, key: str) -> None:
    """Remove an object. Deleting one that is already gone is not an error."""
    _client(backend).delete_object(Bucket=_bucket(backend), Key=key)


def reachable(backend: str) -> bool:
    """For the readiness probe: can this process see the bucket at all.

    Catches everything on purpose. A readiness check has exactly two useful
    answers, and "it raised" is not one of them: an unreachable bucket is the
    condition this exists to report, so letting the exception escape turns the
    probe into a 500 and the orchestrator reads an HTML error page instead of
    "not ready". The narrower list this used to catch missed
    `EndpointConnectionError`, which is the most likely failure of all.

    Logged rather than swallowed, so the reason is still in the record.
    """
    try:
        _client(backend).head_bucket(Bucket=_bucket(backend))
    except Exception:
        logger.warning("Storage backend %r is not reachable", backend, exc_info=True)
        return False
    return True
