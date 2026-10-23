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
    PLANNED_DATE_OUTSIDE_PERIOD = (
        "PLANNED_DATE_OUTSIDE_PERIOD",
        "A planned visit's day must fall inside its month",
    )
    PLANNED_DATE_IN_PAST = "PLANNED_DATE_IN_PAST", "A visit cannot be planned for a past day"

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
    IDEMPOTENCY_KEY_REQUIRED = (
        "IDEMPOTENCY_KEY_REQUIRED",
        "This request must carry an Idempotency-Key header",
    )

    # Business rules --------------------------------------------------------
    ELEVATOR_ALREADY_CONTRACTED = (
        "ELEVATOR_ALREADY_CONTRACTED",
        "Elevator is already covered by an open contract",
    )
    NO_ACTIVE_CONTRACT_FOR_ELEVATOR = (
        "NO_ACTIVE_CONTRACT_FOR_ELEVATOR",
        "The named contract is not active or does not cover this elevator",
    )
    CONTACT_NOT_OF_CUSTOMER = (
        "CONTACT_NOT_OF_CUSTOMER",
        "The contact does not belong to this customer",
    )
    CONTACT_HAS_NO_EMAIL = (
        "CONTACT_HAS_NO_EMAIL",
        "The contact has no e-mail address to send a portal invitation to",
    )
    PORTAL_ACCOUNT_ALREADY_ACTIVE = (
        "PORTAL_ACCOUNT_ALREADY_ACTIVE",
        "This contact already has an active portal account",
    )
    PORTAL_EMAIL_IN_USE = (
        "PORTAL_EMAIL_IN_USE",
        "Another active portal account already uses this e-mail address",
    )
    INVOICE_TRANSITION_NOT_ALLOWED = (
        "INVOICE_TRANSITION_NOT_ALLOWED",
        "The invoice draft is not in a state this action can be taken from",
    )
    SIGNATURE_EXISTS = "SIGNATURE_EXISTS", "This visit already has its signature"
    SIGNATURE_NOT_EXPECTED = (
        "SIGNATURE_NOT_EXPECTED",
        "The visit was not recorded as signed, so it takes no signature",
    )
    SIGNATURE_CANNOT_BE_DELETED = (
        "SIGNATURE_CANNOT_BE_DELETED",
        "A visit's signature is kept for good and cannot be deleted",
    )
    INVOICE_LINES_NEED_PRICE = (
        "INVOICE_LINES_NEED_PRICE",
        "Every line needs a price before the draft can be issued",
    )
    MONTHLY_REPORT_NEEDS_EMAIL = (
        "MONTHLY_REPORT_NEEDS_EMAIL",
        "A contact without an e-mail address cannot receive the monthly report",
    )
    REPORT_NO_RECIPIENT = (
        "REPORT_NO_RECIPIENT",
        "Nobody on this customer is set to receive the monthly report",
    )
    REPORT_FILE_CANNOT_BE_DELETED = (
        "REPORT_FILE_CANNOT_BE_DELETED",
        "A monthly report's file belongs to the report and cannot be deleted",
    )
    CATEGORY_NOT_UPLOADABLE = (
        "CATEGORY_NOT_UPLOADABLE",
        "Files of this category are made by the server and cannot be uploaded",
    )
    DUPLICATE_INVOICE_NUMBER = (
        "DUPLICATE_INVOICE_NUMBER",
        "Another issued invoice of this company already has this number",
    )
    FAULT_TRANSITION_NOT_ALLOWED = (
        "FAULT_TRANSITION_NOT_ALLOWED",
        "The fault is not in a state this action can be taken from",
    )
    PLAN_ROW_CLOSED = (
        "PLAN_ROW_CLOSED",
        "The planned visit is done or cancelled; its day can no longer change",
    )
    NOTIFICATION_EMAIL_LOCKED = (
        "NOTIFICATION_EMAIL_LOCKED",
        "This notification is always e-mailed and cannot be turned off",
    )
    LAST_OWNER_CANNOT_BE_DEACTIVATED = (
        "LAST_OWNER_CANNOT_BE_DEACTIVATED",
        "A company must keep at least one active owner",
    )
    ONLY_TECHNICIANS_ARE_ASSIGNED = (
        "ONLY_TECHNICIANS_ARE_ASSIGNED",
        "Customer assignments apply to technicians only",
    )
    TECHNICIAN_RECORDS_OWN_VISITS = (
        "TECHNICIAN_RECORDS_OWN_VISITS",
        "A technician records maintenance visits in their own name only",
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
    # The ledger (F0).
    CURRENCY_MISMATCH = (
        "CURRENCY_MISMATCH",
        "The amount's currency differs from its account's or its cash account's",
    )
    PERIOD_CLOSED = (
        "PERIOD_CLOSED",
        "The date falls in a period the firm has closed",
    )
    ALREADY_REVERSED = (
        "ALREADY_REVERSED",
        "This document has already been cancelled",
    )
    INVOICE_HAS_COLLECTIONS = (
        "INVOICE_HAS_COLLECTIONS",
        "Collections are applied to this invoice; cancel them first",
    )
    OVERPAYMENT_ON_INVOICE = (
        "OVERPAYMENT_ON_INVOICE",
        "The amount applied exceeds the invoice's remaining balance",
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
