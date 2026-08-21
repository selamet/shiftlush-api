"""The tenant boundary.

This is the first acceptance criterion in the specification and the one whose
failure is worst: two companies must not be able to reach each other's records
under any circumstance. The layer is only worth having if it is proven, so it is
tested before anything is built on top of it.
"""

from __future__ import annotations

import pytest

from apps.companies.models import Company
from apps.customers.models import Customer, CustomerType
from core.context import company_context, system_context
from core.models import TenantMismatchError


@pytest.fixture
def two_companies(db) -> tuple[Company, Company]:
    with system_context():
        first = Company.objects.create(legal_name="First Ltd", display_name="First")
        second = Company.objects.create(legal_name="Second Ltd", display_name="Second")
    return first, second


def make_customer(company: Company, name: str) -> Customer:
    with company_context(company.id):
        return Customer.objects.create(
            company=company, type=CustomerType.CORPORATE, legal_name=name
        )


class TestReadIsolation:
    def test_company_sees_only_its_own_rows(self, two_companies):
        first, second = two_companies
        make_customer(first, "Customer of first")
        make_customer(second, "Customer of second")

        with company_context(first.id):
            names = list(Customer.objects.values_list("legal_name", flat=True))
        assert names == ["Customer of first"]

    def test_other_companys_row_is_not_reachable_by_id(self, two_companies):
        first, second = two_companies
        theirs = make_customer(second, "Customer of second")

        with company_context(first.id), pytest.raises(Customer.DoesNotExist):
            Customer.objects.get(pk=theirs.pk)

    def test_no_context_returns_nothing_rather_than_everything(self, two_companies):
        first, second = two_companies
        make_customer(first, "Customer of first")
        make_customer(second, "Customer of second")

        # A missing context is a bug either way, but it has to fail closed: an
        # unfiltered queryset here would be a cross-tenant read.
        assert Customer.objects.count() == 0

    def test_unscoped_is_the_only_way_across(self, two_companies):
        first, second = two_companies
        make_customer(first, "Customer of first")
        make_customer(second, "Customer of second")

        with company_context(first.id):
            assert Customer.unscoped.count() == 2


class TestWriteIsolation:
    def test_saving_under_the_wrong_company_raises(self, two_companies):
        first, second = two_companies
        theirs = make_customer(second, "Customer of second")

        # Filtering reads is only half of it: without the save guard a row could
        # still be attached to another company by setting the FK directly.
        with company_context(first.id), pytest.raises(TenantMismatchError):
            theirs.legal_name = "Renamed by the wrong company"
            theirs.save()

    def test_creating_under_the_wrong_company_raises(self, two_companies):
        first, second = two_companies
        with company_context(first.id), pytest.raises(TenantMismatchError):
            Customer.objects.create(
                company=second, type=CustomerType.CORPORATE, legal_name="Smuggled"
            )


class TestSoftDelete:
    def test_delete_marks_rather_than_removes(self, two_companies):
        first, _ = two_companies
        customer = make_customer(first, "To be deleted")

        with company_context(first.id):
            customer.delete()
            assert Customer.objects.count() == 0
            # The row is still there; only the default manager hides it.
            assert Customer.unscoped.filter(pk=customer.pk, is_deleted=True).exists()

    def test_queryset_delete_also_soft_deletes(self, two_companies):
        first, _ = two_companies
        make_customer(first, "Bulk deleted")

        # QuerySet.delete() bypasses the model's delete(), so without the
        # manager override a bulk delete would destroy rows for real.
        with company_context(first.id):
            Customer.objects.all().delete()
            assert Customer.unscoped.filter(is_deleted=True).count() == 1

    def test_relation_traversal_is_filtered_too(self, two_companies):
        first, _ = two_companies
        customer = make_customer(first, "Parent")

        with company_context(first.id):
            customer.contacts.create(company=first, full_name="Contact")
            assert customer.contacts.count() == 1
            customer.contacts.all().delete()
            # base_manager_name is what makes this hold: without it, following a
            # relation would step around both filters.
            assert customer.contacts.count() == 0


class TestSystemContext:
    def test_registration_can_run_without_a_company(self, db):
        # Company registration and invitation acceptance create the very rows
        # the tenant filter keys on, so they must be able to run with no company
        # in context. Without this escape hatch the save guard would reject the
        # first write a new customer ever makes.
        with system_context():
            company = Company.objects.create(legal_name="Brand New Ltd", display_name="Brand New")
            Customer.objects.create(
                company=company, type=CustomerType.CORPORATE, legal_name="First customer"
            )

        with company_context(company.id):
            assert Customer.objects.count() == 1
