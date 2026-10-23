"""The ledger over HTTP: accounts, cash accounts, suppliers, the documents that
move money, and the finance summary.

docs/superpowers/specs/2026-10-23-f0-ledger-design.md. Every write calls one
service in apps.ledger.services, which is atomic and takes its own locks in
the order written there; nothing here locks a row or writes a movement. A
document is never edited and never deleted: it is created, read and
cancelled, and cancelling posts the reversal.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, cast

import django_filters
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema
from rest_framework import mixins, status
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from apps.billing.models import InvoiceDraft, InvoiceStatus
from apps.ledger import finance, services, statement
from apps.ledger.api.v1.serializers import (
    AccountCreateSerializer,
    AccountDetailSerializer,
    AccountReadSerializer,
    AccountTotalsSerializer,
    CashAccountPickerSerializer,
    CashAccountReadSerializer,
    CashAccountWriteSerializer,
    CollectionCreateSerializer,
    CollectionReadSerializer,
    FinanceQuerySerializer,
    FinanceSummarySerializer,
    LedgerSettingsSerializer,
    ManualEntryCreateSerializer,
    ManualEntryReadSerializer,
    MovementSerializer,
    OpeningBalanceCreateSerializer,
    OpeningBalanceReadSerializer,
    PaymentOutCreateSerializer,
    PaymentOutReadSerializer,
    StatementQuerySerializer,
    StatementSentSerializer,
    StatementSerializer,
    SupplierReadSerializer,
    SupplierWriteSerializer,
    TransferCreateSerializer,
    TransferReadSerializer,
)
from apps.ledger.balances import with_balance, with_cash_balance
from apps.ledger.models import (
    Account,
    AccountKind,
    CashAccount,
    Collection,
    ManualEntry,
    Movement,
    OpeningBalance,
    PaymentOut,
    Supplier,
    Transfer,
)
from core.exceptions import ServiceUnavailable
from core.filters import TenantFilterSet
from core.idempotency import ReplayProtectedCreate, replay_protected
from core.pagination import StandardPagination
from core.permissions import READ, RolePermission, may
from core.requests import authenticated_company, authenticated_company_id, authenticated_user
from core.scheduling import local_today

IDEMPOTENCY_KEY = OpenApiParameter(
    "Idempotency-Key",
    str,
    OpenApiParameter.HEADER,
    required=True,
    description=(
        "Required. The same key with the same body answers what the first request "
        "did, and records nothing more."
    ),
)

#: An invoice issued longer ago than this, and still unpaid, makes its
#: customer overdue when the request does not say otherwise.
DEFAULT_OVERDUE_DAYS = 30


# Accounts -------------------------------------------------------------------------


class AccountFilter(TenantFilterSet):
    search = django_filters.CharFilter(method="filter_search", help_text="Part of the name.")
    has_balance = django_filters.BooleanFilter(
        method="filter_has_balance", help_text="Whether the balance is anything but zero."
    )
    overdue = django_filters.BooleanFilter(
        method="filter_overdue",
        help_text=(
            "Owes the firm, and has an issued invoice still unpaid that is older "
            "than `overdue_days`."
        ),
    )
    overdue_days = django_filters.NumberFilter(
        method="filter_noop",
        min_value=0,
        help_text=f"Days, for `overdue`. {DEFAULT_OVERDUE_DAYS} when omitted.",
    )

    class Meta:
        model = Account
        fields = ["kind", "is_active", "search", "has_balance", "overdue", "overdue_days"]

    def filter_search(
        self, queryset: QuerySet[Account], name: str, value: str
    ) -> QuerySet[Account]:
        return queryset.filter(name__icontains=value.strip())

    def filter_has_balance(
        self, queryset: QuerySet[Account], name: str, value: bool
    ) -> QuerySet[Account]:
        zero = Q(balance=0)
        return queryset.exclude(zero) if value else queryset.filter(zero)

    def filter_noop(self, queryset: QuerySet[Account], name: str, value: Any) -> QuerySet[Account]:
        # Read by filter_overdue; on its own it narrows nothing.
        return queryset

    def filter_overdue(
        self, queryset: QuerySet[Account], name: str, value: bool
    ) -> QuerySet[Account]:
        days = self.form.cleaned_data.get("overdue_days")
        cutoff = local_today() - timedelta(days=int(DEFAULT_OVERDUE_DAYS if days is None else days))
        # `issued` is an invoice collections do not yet cover; `paid` is one
        # they do (apps.ledger.services.sync_invoice_status).
        unpaid = InvoiceDraft.objects.filter(
            customer_id=OuterRef("customer_id"),
            status=InvoiceStatus.ISSUED,
            cancelled_at__isnull=True,
            invoice_date__lt=cutoff,
        )
        is_overdue = Q(kind=AccountKind.CUSTOMER, balance__gt=0) & Q(Exists(unpaid))
        return queryset.filter(is_overdue) if value else queryset.exclude(is_overdue)


class AccountListPagination(StandardPagination):
    """The standard page, and the totals of every row the filters select."""

    def get_paginated_response_schema(self, schema: Any) -> Any:
        envelope = super().get_paginated_response_schema(schema)
        envelope["required"] = [*envelope["required"], "totals"]
        # Inline: the totals serializer is never a response on its own, so the
        # generator registers no component a reference could point at.
        money = {"type": "string", "format": "decimal", "pattern": r"^-?\d{0,14}(?:\.\d{0,2})?$"}
        envelope["properties"]["totals"] = {
            "type": "object",
            "required": ["receivables", "payables", "net"],
            "properties": {
                "receivables": {**money, "description": "The positive balances, summed."},
                "payables": {
                    **money,
                    "description": "The negative balances, summed, as a positive amount.",
                },
                "net": {**money, "description": "Receivables less payables."},
            },
        }
        return envelope


class AccountViewSet(
    ReplayProtectedCreate,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet[Account],
):
    """Each party's account with the firm, and its balance.

    No update and no delete: an account's balance is its movements, and an
    account that is no longer wanted is made inactive by the party it names.
    """

    resource = "ledger"
    resource_by_action = {"create": "ledger_admin", "statement_send": "ledger_statement_send"}
    permission_classes = [RolePermission]
    queryset = Account.objects.none()
    serializer_class = AccountReadSerializer
    filterset_class = AccountFilter
    pagination_class = AccountListPagination
    ordering = ["name", "id"]
    ordering_fields = ["name", "balance", "kind"]

    def get_queryset(self) -> QuerySet[Account]:
        return with_balance(Account.objects.all())

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if self.action == "retrieve":
            return AccountDetailSerializer
        return AccountReadSerializer

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Every account with its balance; `totals` cover every row the filters select."""
        accounts = self.filter_queryset(self.get_queryset())
        totals = finance.balance_totals(accounts)
        page = self.paginate_queryset(accounts)
        response = self.get_paginated_response(self.get_serializer(page, many=True).data)
        response.data["totals"] = AccountTotalsSerializer(totals).data
        return response

    @extend_schema(request=AccountCreateSerializer, responses={201: AccountReadSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Open an account for a party that has none: a staff member, mostly."""
        form = AccountCreateSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        account = services.open_account(
            company_id=authenticated_company_id(request),
            by=authenticated_user(request),
            **form.validated_data,
        )
        fresh = self.get_queryset().get(pk=account.pk)
        return Response(AccountReadSerializer(fresh).data, status=status.HTTP_201_CREATED)

    # The statement ----------------------------------------------------------------
    #
    # Reads, apart from the send: no lock is taken. Every one goes through
    # `get_object()`, so another firm's account is a 404.

    @property
    def idempotency_key_required(self) -> bool:
        # Sending mails the customer; a retried request must not mail twice. The
        # account create keeps its key optional.
        return getattr(self, "action", None) == "statement_send"

    def _statement(self, params: Any) -> statement.Statement:
        query = StatementQuerySerializer(data=params)
        query.is_valid(raise_exception=True)
        return statement.statement_rows(
            self.get_object(),
            start=query.validated_data.get("start"),
            end=query.validated_data.get("end"),
        )

    @extend_schema(parameters=[StatementQuerySerializer], responses={200: StatementSerializer})
    # Not named `statement`: the router orders the routes by method name and
    # gives each a format-suffix twin, so `statement\.(?P<format>…)` would come
    # first and swallow `statement.csv/` and `statement.pdf/` as unknown formats.
    @action(
        detail=True,
        methods=["get"],
        url_path="statement",
        url_name="statement",
        pagination_class=None,
    )
    def statement_read(self, request: Request, pk: str | None = None) -> Response:
        """Its movements from `start` to `end` (both optional, inclusive), each with
        the running balance after it, under the opening balance before `start`.
        A reversal is its own row. `start` after `end` is `400` on `start`."""
        return Response(StatementSerializer(self._statement(request.query_params)).data)

    @extend_schema(
        parameters=[StatementQuerySerializer], responses={(200, "text/csv"): OpenApiTypes.STR}
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="statement.csv",
        url_name="statement-csv",
        pagination_class=None,
    )
    def statement_csv(self, request: Request, pk: str | None = None) -> HttpResponse:
        """The statement as CSV: semicolons, UTF-8 with a BOM, decimal commas."""
        found = self._statement(request.query_params)
        response = HttpResponse(
            statement.statement_csv(found).encode("utf-8"), content_type="text/csv; charset=utf-8"
        )
        filename = statement.statement_filename(found, "csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @extend_schema(
        parameters=[StatementQuerySerializer],
        responses={(200, "application/pdf"): OpenApiTypes.BINARY},
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="statement.pdf",
        url_name="statement-pdf",
        pagination_class=None,
    )
    def statement_pdf(self, request: Request, pk: str | None = None) -> HttpResponse:
        """The statement as a PDF. `503` where the renderer is not installed."""
        found = self._statement(request.query_params)
        try:
            pdf = statement.render_statement_pdf(found, company=authenticated_company(request))
        except statement.PdfRenderingUnavailable as exc:
            raise ServiceUnavailable() from exc
        response = HttpResponse(pdf, content_type="application/pdf")
        filename = statement.statement_filename(found, "pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response

    @extend_schema(
        parameters=[IDEMPOTENCY_KEY],
        request=StatementQuerySerializer,
        responses={200: StatementSentSerializer},
    )
    @action(detail=True, methods=["post"], url_path="statement/send", url_name="statement-send")
    # replay_protected owns the transaction around the send and its key, so the
    # mails (queued on commit) go only once both are committed.
    @replay_protected
    def statement_send(self, request: Request, pk: str | None = None) -> Response:
        """E-mail the statement's PDF to the customer's contacts who receive reports.

        The body is `{start, end}`, both optional. The mails go out after this
        commits. No recipient is `409 REPORT_NO_RECIPIENT`; requires
        `Idempotency-Key`.
        """
        query = StatementQuerySerializer(data=request.data)
        query.is_valid(raise_exception=True)
        start: date | None = query.validated_data.get("start")
        end: date | None = query.validated_data.get("end")
        try:
            emails = statement.send_statement(
                self.get_object(), start=start, end=end, by=authenticated_user(request)
            )
        except statement.PdfRenderingUnavailable as exc:
            raise ServiceUnavailable() from exc
        return Response(StatementSentSerializer({"recipients": emails}).data)


# Cash accounts and suppliers ------------------------------------------------------


def _cash_account_either(many: bool = False) -> PolymorphicProxySerializer:
    """The two shapes a cash-account read takes, by the reader's role."""
    return PolymorphicProxySerializer(
        component_name="CashAccountListItem",
        serializers=[CashAccountReadSerializer, CashAccountPickerSerializer],
        resource_type_field_name=None,
        many=many,
    )


class CashAccountViewSet(
    ReplayProtectedCreate,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet[CashAccount],
):
    """Where the firm's money is: tills, bank accounts and card POS."""

    resource = "ledger"
    # Operations picks the cash account a collection goes into; the movements
    # stay with whoever reads the ledger.
    resource_by_action = {
        "list": "ledger_picker",
        "retrieve": "ledger_picker",
        "create": "ledger_admin",
        "partial_update": "ledger_admin",
    }
    permission_classes = [RolePermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = CashAccount.objects.none()
    serializer_class = CashAccountReadSerializer
    filterset_fields = ["kind", "is_active"]
    ordering = ["name", "id"]
    ordering_fields = ["name", "balance"]

    def _picker_only(self) -> bool:
        """Whether this request may pick a cash account but not read the ledger.

        Such a reader gets the slim shape: no balance, no IBAN, and no ordering
        by a balance it may not see (an ordering would leak it as a ranking).
        """
        role = getattr(self.request.user, "role", None)
        return self.action in ("list", "retrieve") and not may(role, "ledger", READ)

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        if self._picker_only():
            # An ordering field not listed is ignored by the ordering filter.
            self.ordering_fields = ["name"]

    def get_queryset(self) -> QuerySet[CashAccount]:
        if getattr(self, "request", None) is not None and self._picker_only():
            # No balance annotation: it is neither shown nor sortable here.
            return CashAccount.objects.all()
        return with_cash_balance(CashAccount.objects.all())

    def get_serializer_class(self) -> type[BaseSerializer[Any]]:
        if getattr(self, "request", None) is not None and self._picker_only():
            return CashAccountPickerSerializer
        return CashAccountReadSerializer

    @extend_schema(responses={200: _cash_account_either(many=True)})
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Every cash account. Operations gets the picker shape, without balance or IBAN."""
        return super().list(request, *args, **kwargs)

    @extend_schema(responses={200: _cash_account_either()})
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """One cash account. Operations gets the picker shape, without balance or IBAN."""
        return super().retrieve(request, *args, **kwargs)

    def _read(self, cash: CashAccount) -> Response:
        return Response(CashAccountReadSerializer(self.get_queryset().get(pk=cash.pk)).data)

    @extend_schema(request=CashAccountWriteSerializer, responses={201: CashAccountReadSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = CashAccountWriteSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        user = authenticated_user(request)
        cash = form.save(company_id=user.company_id, created_by=user, updated_by=user)
        response = self._read(cash)
        response.status_code = status.HTTP_201_CREATED
        return response

    @extend_schema(request=CashAccountWriteSerializer, responses={200: CashAccountReadSerializer})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Rename it, set its IBAN, or make it inactive. The currency is fixed once used."""
        form = CashAccountWriteSerializer(self.get_object(), data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        cash = form.save(updated_by=authenticated_user(request))
        return self._read(cash)

    @extend_schema(responses={200: MovementSerializer(many=True)})
    @action(detail=True, methods=["get"])
    def movements(self, request: Request, pk: str | None = None) -> Response:
        """Its movements, newest first."""
        cash = self.get_object()
        rows = Movement.objects.filter(cash_account=cash).order_by("-date", "-created_at", "-id")
        page = self.paginate_queryset(cast(Any, rows))
        return self.get_paginated_response(MovementSerializer(page, many=True).data)


class SupplierViewSet(
    ReplayProtectedCreate,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet[Supplier],
):
    """Whom the firm buys from. Each comes with its account."""

    resource = "ledger"
    resource_by_action = {
        "list": "ledger_picker",
        "retrieve": "ledger_picker",
        "create": "ledger_document",
        "partial_update": "ledger_document",
    }
    permission_classes = [RolePermission]
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = Supplier.objects.none()
    serializer_class = SupplierReadSerializer
    ordering = ["name", "id"]
    ordering_fields = ["name", "created_at"]

    def get_queryset(self) -> QuerySet[Supplier]:
        return Supplier.objects.prefetch_related("accounts")

    def _read(self, supplier: Supplier) -> Response:
        return Response(SupplierReadSerializer(self.get_queryset().get(pk=supplier.pk)).data)

    @extend_schema(request=SupplierWriteSerializer, responses={201: SupplierReadSerializer})
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = SupplierWriteSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        supplier = services.create_supplier(
            company_id=authenticated_company_id(request),
            by=authenticated_user(request),
            **form.validated_data,
        )
        response = self._read(supplier)
        response.status_code = status.HTTP_201_CREATED
        return response

    @extend_schema(request=SupplierWriteSerializer, responses={200: SupplierReadSerializer})
    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        supplier = self.get_object()
        form = SupplierWriteSerializer(supplier, data=request.data, partial=True)
        form.is_valid(raise_exception=True)
        services.update_supplier(
            supplier, changes=dict(form.validated_data), by=authenticated_user(request)
        )
        return self._read(supplier)


# Documents ------------------------------------------------------------------------


class _DocumentViewSet[D: (Collection, PaymentOut, ManualEntry, Transfer, OpeningBalance)](
    ReplayProtectedCreate,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    GenericViewSet[D],
):
    """Created, read and cancelled; never edited, never deleted.

    A create needs an `Idempotency-Key`: a retried request that recorded the
    money twice is exactly what the ledger exists to prevent.
    """

    permission_classes = [RolePermission]
    http_method_names = ["get", "post", "head", "options"]
    idempotency_key_required = True
    ordering = ["-date", "-created_at", "-id"]
    ordering_fields = ["date", "amount", "created_at"]

    def _read(self, document: D) -> Response:
        fresh = self.get_queryset().get(pk=document.pk)
        return Response(self.get_serializer(fresh).data)

    def _created(self, document: D) -> Response:
        response = self._read(document)
        response.status_code = status.HTTP_201_CREATED
        return response

    def _cancel(self, document: D, *, by: Any) -> D:  # pragma: no cover - abstract
        raise NotImplementedError

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def cancel(self, request: Request, pk: str | None = None) -> Response:
        """Post the reversal of each of its movements. Once: again is `409 ALREADY_REVERSED`."""
        document = self._cancel(self.get_object(), by=authenticated_user(request))
        return self._read(document)


class _DatedFilter(TenantFilterSet):
    date = django_filters.DateFromToRangeFilter(help_text="`date_after` / `date_before`.")


class CollectionFilter(_DatedFilter):
    class Meta:
        model = Collection
        fields = ["account", "cash_account", "method", "date"]


class PaymentOutFilter(_DatedFilter):
    class Meta:
        model = PaymentOut
        fields = ["account", "cash_account", "method", "date"]


class CollectionViewSet(_DocumentViewSet[Collection]):
    """Money received (tahsilat): a credit on the account, cash in."""

    resource = "ledger_document"
    queryset = Collection.objects.none()
    serializer_class = CollectionReadSerializer
    filterset_class = CollectionFilter
    ordering = ["-date", "-number", "-id"]

    def get_queryset(self) -> QuerySet[Collection]:
        return Collection.objects.prefetch_related("applications")

    def _cancel(self, document: Collection, *, by: Any) -> Collection:
        return services.cancel_collection(document, by=by)

    @extend_schema(
        request=CollectionCreateSerializer,
        responses={201: CollectionReadSerializer},
        parameters=[IDEMPOTENCY_KEY],
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Record money received, and the invoices it pays."""
        form = CollectionCreateSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        data = form.validated_data
        collection = services.record_collection(
            account=data["account"],
            cash_account=data["cash_account"],
            amount=data["amount"],
            method=data["method"],
            date=data["date"],
            note=data["note"],
            applies=[(row["invoice"], row["amount"]) for row in data["applies"]],
            by=authenticated_user(request),
        )
        return self._created(collection)


class PaymentOutViewSet(_DocumentViewSet[PaymentOut]):
    """Money paid out (tediye): a debit on the account, cash out."""

    resource = "ledger_document"
    queryset = PaymentOut.objects.none()
    serializer_class = PaymentOutReadSerializer
    filterset_class = PaymentOutFilter
    ordering = ["-date", "-number", "-id"]

    def get_queryset(self) -> QuerySet[PaymentOut]:
        return PaymentOut.objects.all()

    def _cancel(self, document: PaymentOut, *, by: Any) -> PaymentOut:
        return services.cancel_payment_out(document, by=by)

    @extend_schema(
        request=PaymentOutCreateSerializer,
        responses={201: PaymentOutReadSerializer},
        parameters=[IDEMPOTENCY_KEY],
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = PaymentOutCreateSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        payment = services.record_payment_out(by=authenticated_user(request), **form.validated_data)
        return self._created(payment)


class ManualEntryViewSet(_DocumentViewSet[ManualEntry]):
    """A debit or credit with no cash behind it, and a reason. Owner and accountant."""

    resource = "ledger_admin"
    queryset = ManualEntry.objects.none()
    serializer_class = ManualEntryReadSerializer
    filterset_fields = ["account", "direction"]

    def get_queryset(self) -> QuerySet[ManualEntry]:
        return ManualEntry.objects.all()

    def _cancel(self, document: ManualEntry, *, by: Any) -> ManualEntry:
        return services.cancel_manual_entry(document, by=by)

    @extend_schema(
        request=ManualEntryCreateSerializer,
        responses={201: ManualEntryReadSerializer},
        parameters=[IDEMPOTENCY_KEY],
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = ManualEntryCreateSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        entry = services.record_manual_entry(by=authenticated_user(request), **form.validated_data)
        return self._created(entry)


class TransferViewSet(_DocumentViewSet[Transfer]):
    """A transfer (virman), between two accounts or two cash accounts."""

    resource = "ledger_admin"
    queryset = Transfer.objects.none()
    serializer_class = TransferReadSerializer
    filterset_fields = ["kind"]

    def get_queryset(self) -> QuerySet[Transfer]:
        return Transfer.objects.all()

    def _cancel(self, document: Transfer, *, by: Any) -> Transfer:
        return services.cancel_transfer(document, by=by)

    @extend_schema(
        request=TransferCreateSerializer,
        responses={201: TransferReadSerializer},
        parameters=[IDEMPOTENCY_KEY],
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = TransferCreateSerializer(data=request.data, context=self.get_serializer_context())
        form.is_valid(raise_exception=True)
        transfer = services.record_transfer(by=authenticated_user(request), **form.validated_data)
        return self._created(transfer)


class OpeningBalanceViewSet(_DocumentViewSet[OpeningBalance]):
    """A balance carried over at go-live (devir). Owner and accountant."""

    resource = "ledger_admin"
    queryset = OpeningBalance.objects.none()
    serializer_class = OpeningBalanceReadSerializer
    filterset_fields = ["account", "cash_account"]

    def get_queryset(self) -> QuerySet[OpeningBalance]:
        return OpeningBalance.objects.all()

    def _cancel(self, document: OpeningBalance, *, by: Any) -> OpeningBalance:
        return services.cancel_opening(document, by=by)

    @extend_schema(
        request=OpeningBalanceCreateSerializer,
        responses={201: OpeningBalanceReadSerializer},
        parameters=[IDEMPOTENCY_KEY],
    )
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        form = OpeningBalanceCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        form.is_valid(raise_exception=True)
        opening = services.record_opening(by=authenticated_user(request), **form.validated_data)
        return self._created(opening)


# Settings -------------------------------------------------------------------------


class LedgerSettingsView(APIView):
    """The ledger's settings for the firm: the closed-through date.

    A singleton with no id, like `/company`: the only firm a request can
    address is its own. Owner and accountant.
    """

    resource = "ledger_admin"
    permission_classes = [RolePermission]

    @extend_schema(responses={200: LedgerSettingsSerializer})
    def get(self, request: Request) -> Response:
        company = authenticated_company(request)
        return Response(
            LedgerSettingsSerializer({"closed_through": company.ledger_closed_through}).data
        )

    @extend_schema(request=LedgerSettingsSerializer, responses={200: LedgerSettingsSerializer})
    def patch(self, request: Request) -> Response:
        form = LedgerSettingsSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        company = services.set_closed_through(
            authenticated_company_id(request),
            form.validated_data["closed_through"],
            by=authenticated_user(request),
        )
        return Response(
            LedgerSettingsSerializer({"closed_through": company.ledger_closed_through}).data
        )


# The finance summary --------------------------------------------------------------


class FinanceViewSet(GenericViewSet[Account]):
    """The section's home: receivables, payables, and the money in the firm."""

    resource = "ledger"
    permission_classes = [RolePermission]
    queryset = Account.objects.none()
    serializer_class = FinanceSummarySerializer
    pagination_class = None
    filter_backends: list[Any] = []

    @extend_schema(parameters=[FinanceQuerySerializer], responses={200: FinanceSummarySerializer})
    @action(detail=False, methods=["get"])
    def summary(self, request: Request) -> Response:
        """`period`: `today` (the default), `yesterday`, `week` (from Monday) or `month`."""
        query = FinanceQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        result = finance.summary(query.validated_data["period"], local_today())
        return Response(FinanceSummarySerializer(result).data)
