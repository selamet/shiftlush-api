"""Every error code the API can return.

The backend sends codes, not sentences. Turkish lives in the frontend
translation file, which means one place to change wording, one place to add a
language, and no user-facing text compiled into the API.

Adding a code is a backwards-compatible change. Removing or renaming one is a
breaking change and needs a new API version — a client switching on a code it
no longer receives falls through to nothing.
"""

from __future__ import annotations

from django.db import models


class ErrorCode(models.TextChoices):
    # Generic ---------------------------------------------------------------
    VALIDATION_ERROR = "VALIDATION_ERROR", "Request failed validation"
    NOT_FOUND = "NOT_FOUND", "Record not found"
    PERMISSION_DENIED = "PERMISSION_DENIED", "Insufficient permissions"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED", "Authentication failed"
    INTERNAL_ERROR = "INTERNAL_ERROR", "Unexpected server error"
    THROTTLED = "THROTTLED", "Rate limit exceeded"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE", "A required service is not available"
    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION", "The request violates a database constraint"
    API_VERSION_SUNSET = "API_VERSION_SUNSET", "This API version is no longer served"

    # Field level -----------------------------------------------------------
    INVALID_TAX_NUMBER = "INVALID_TAX_NUMBER", "Tax number failed its check digit"
    INVALID_NATIONAL_ID = "INVALID_NATIONAL_ID", "National ID failed its check digit"
    INVALID_PHONE = "INVALID_PHONE", "Phone number is not a valid Turkish number"
    INVALID_POSTAL_CODE = "INVALID_POSTAL_CODE", "Postal code must be five digits"
    END_DATE_BEFORE_START_DATE = (
        "END_DATE_BEFORE_START_DATE",
        "End date must be after the start date",
    )
    INSTALLATION_DATE_IN_FUTURE = (
        "INSTALLATION_DATE_IN_FUTURE",
        "Installation date cannot be in the future",
    )

    # Conflict --------------------------------------------------------------
    RECORD_IN_USE = "RECORD_IN_USE", "Record is referenced by other records"
    DUPLICATE_REGISTRATION_NUMBER = (
        "DUPLICATE_REGISTRATION_NUMBER",
        "Registration number already exists for this company",
    )
    DUPLICATE_CONTRACT_NUMBER = (
        "DUPLICATE_CONTRACT_NUMBER",
        "Contract number already exists for this company",
    )
    DUPLICATE_TAX_NUMBER = (
        "DUPLICATE_TAX_NUMBER",
        "Tax number already belongs to another customer of this company",
    )
    DUPLICATE_NATIONAL_ID = (
        "DUPLICATE_NATIONAL_ID",
        "National ID already belongs to another customer of this company",
    )
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED", "E-mail address is already registered"
    INVITATION_ALREADY_PENDING = (
        "INVITATION_ALREADY_PENDING",
        "This address already holds an invitation that has not been accepted",
    )
    IDEMPOTENCY_KEY_REUSED = (
        "IDEMPOTENCY_KEY_REUSED",
        "Idempotency key was reused with a different request body",
    )

    # Business rules --------------------------------------------------------
    ELEVATOR_ALREADY_CONTRACTED = (
        "ELEVATOR_ALREADY_CONTRACTED",
        "Elevator is already covered by an open contract",
    )
    LAST_OWNER_CANNOT_BE_DEACTIVATED = (
        "LAST_OWNER_CANNOT_BE_DEACTIVATED",
        "A company must keep at least one active owner",
    )
    ONLY_TECHNICIANS_ARE_ASSIGNED = (
        "ONLY_TECHNICIANS_ARE_ASSIGNED",
        "Customer assignments apply to technicians only",
    )
    CANNOT_DEACTIVATE_SELF = (
        "CANNOT_DEACTIVATE_SELF",
        "A user cannot deactivate their own account",
    )
    BUILDING_CUSTOMER_MISMATCH = (
        "BUILDING_CUSTOMER_MISMATCH",
        "A building in a complex must share the complex's customer",
    )
    TERMINATION_REASON_REQUIRED = (
        "TERMINATION_REASON_REQUIRED",
        "A termination reason is required",
    )
    FIELD_REQUIRED_FOR_CUSTOMER_TYPE = (
        "FIELD_REQUIRED_FOR_CUSTOMER_TYPE",
        "This customer type requires this field",
    )
    FIELD_NOT_VALID_FOR_CUSTOMER_TYPE = (
        "FIELD_NOT_VALID_FOR_CUSTOMER_TYPE",
        "This field does not apply to this customer type",
    )
    STATUS_NOT_USER_SELECTABLE = (
        "STATUS_NOT_USER_SELECTABLE",
        "This status is assigned by the system",
    )

    # Authentication --------------------------------------------------------
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS", "E-mail or password is incorrect"
    # Kept under its original name: renaming a code is a breaking change, and
    # this is the one the sign-in screen already switches on. What it means has
    # narrowed — 7.4 counts failures per e-mail *and* address, so what is locked
    # is this caller for fifteen minutes, not the account. The same person
    # signing in from anywhere else is unaffected, and the wording says address
    # rather than account so a translation of it cannot promise otherwise.
    ACCOUNT_LOCKED = (
        "ACCOUNT_LOCKED",
        "Too many failed attempts from this address; try again later",
    )
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE", "Account is not active"
    TOKEN_INVALID = "TOKEN_INVALID", "Token is invalid or has already been used"
    TOKEN_EXPIRED = "TOKEN_EXPIRED", "Token has expired"
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED", "E-mail address has not been verified"

    # Attachments -----------------------------------------------------------
    FILE_TOO_LARGE = "FILE_TOO_LARGE", "File exceeds the 10 MB limit"
    UNSUPPORTED_MIME_TYPE = "UNSUPPORTED_MIME_TYPE", "File type is not accepted"
    UPLOAD_NOT_COMPLETED = (
        "UPLOAD_NOT_COMPLETED",
        "No uploaded object was found for this key",
    )
    ATTACHMENT_TARGET_MISMATCH = (
        "ATTACHMENT_TARGET_MISMATCH",
        "Attachment does not belong to the record it is being linked to",
    )
