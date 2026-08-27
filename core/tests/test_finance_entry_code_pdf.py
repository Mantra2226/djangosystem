"""
TESTS: Finance Entry Sequential Codes, ID Hiding, and In-Memory PDF Voucher Generation
Verifies:
1. Sequential entry_code auto-generation (FE-YYYYMM-XXXX).
2. Admin changelist displays entry_code and hides raw database ID.
3. In-memory PDF voucher generation and download endpoint (/admin/core/financeentry/<id>/pdf/).
4. Admin immutability guards (disabled manual add/change).
5. Unauthenticated redirection via admin_site.admin_view wrapper.
6. DRF serializer contract with entry_code and hidden database PK.
7. ReportLab long description/audit notes flowable text wrapping.
"""

import re
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import FinanceEntry, Customer, SalesInvoice, Product
from core.serializers import FinanceEntrySerializer
from core.utils.pdf_generator import generate_finance_entry_pdf
from core.admin import FinanceEntryAdmin
from django.contrib.admin.sites import AdminSite

User = get_user_model()


class FinanceEntryCodeAndPDFTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username='admin_finance',
            email='finance@example.com',
            password='Password123!'
        )
        self.client.force_login(self.superuser)

        self.customer = Customer.objects.create(
            customer_name="Nairobi Glass Traders",
            contact_info="finance@nairobiglass.com",
            shipping_address="Commercial Street, Nairobi"
        )

        self.product = Product.objects.create(
            name="Glass Putty 25kg Pack",
            sku="FG-PUTTY-25KG-FE-TEST",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="Pack",
            selling_price=Decimal("2500.00")
        )

    def test_finance_entry_auto_generates_sequential_code(self):
        """Creating a FinanceEntry automatically generates a sequential entry_code matching FE-YYYYMM-XXXX."""
        entry1 = FinanceEntry.objects.create(
            entry_type='REVENUE',
            category='SALES',
            amount=Decimal('50000.00'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser,
            description="Q3 Advance sales payment batch"
        )
        entry2 = FinanceEntry.objects.create(
            entry_type='EXPENSE',
            category='LABOR',
            amount=Decimal('15000.00'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser,
            description="Overtime production floor labor"
        )

        self.assertIsNotNone(entry1.entry_code)
        self.assertIsNotNone(entry2.entry_code)

        pattern = r"^FE-\d{6}-\d{4}$"
        self.assertTrue(re.match(pattern, entry1.entry_code), f"Code {entry1.entry_code} did not match {pattern}")
        self.assertTrue(re.match(pattern, entry2.entry_code), f"Code {entry2.entry_code} did not match {pattern}")

        # Assert monotonic increment
        seq1 = int(entry1.entry_code.split('-')[-1])
        seq2 = int(entry2.entry_code.split('-')[-1])
        self.assertEqual(seq2, seq1 + 1)

        # Assert string representation includes entry_code and currency
        self.assertIn(entry1.entry_code, str(entry1))
        self.assertIn("KES 50,000.00", str(entry1))

    def test_finance_entry_admin_hides_raw_id_and_displays_entry_code(self):
        """Changelist renders entry_code, formatted currency, and excludes raw database ID in list_display."""
        entry = FinanceEntry.objects.create(
            entry_type='REVENUE',
            category='SALES',
            amount=Decimal('75000.00'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser,
            description="Direct commercial invoice settlement"
        )

        url = reverse('admin:core_financeentry_changelist')
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, entry.entry_code)
        self.assertContains(response, "KES 75,000.00")
        self.assertContains(response, "📄 Voucher PDF")

        # Verify admin model config
        admin_obj = FinanceEntryAdmin(FinanceEntry, AdminSite())
        self.assertNotIn('id', admin_obj.list_display)
        self.assertNotIn('finance_entry_id', admin_obj.list_display)
        self.assertIn('entry_code', admin_obj.list_display)

    def test_finance_entry_pdf_view_returns_http_200_and_pdf_binary(self):
        """Requesting /admin/core/financeentry/<id>/pdf/ returns 200 OK and PDF binary stream."""
        entry = FinanceEntry.objects.create(
            entry_type='REVENUE',
            category='SALES',
            amount=Decimal('120000.00'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser,
            description="Commercial order invoice settlement receipt"
        )

        url = reverse('admin:financeentry-pdf', args=[entry.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(entry.entry_code, response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_finance_entry_pdf_action_detail_download(self):
        """Unfold actions_detail download-pdf endpoint streams valid PDF voucher."""
        from django.test import RequestFactory
        factory = RequestFactory()

        entry = FinanceEntry.objects.create(
            entry_type='EXPENSE',
            category='OVERHEAD',
            amount=Decimal('8500.00'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser,
            description="Factory generator diesel refill"
        )

        admin_instance = FinanceEntryAdmin(FinanceEntry, AdminSite())
        request = factory.get(f'/admin/core/financeentry/{entry.pk}/download-pdf/')
        request.user = self.superuser

        response = admin_instance.action_download_pdf(request, entry.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(entry.entry_code, response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_finance_entry_immutability_guards(self):
        """Admin interface disables manual addition and editing of ledger entries."""
        admin_obj = FinanceEntryAdmin(FinanceEntry, AdminSite())
        class MockRequest:
            user = self.superuser

        req = MockRequest()
        self.assertFalse(admin_obj.has_add_permission(req))
        self.assertFalse(admin_obj.has_change_permission(req))

    def test_unauthenticated_pdf_request_redirects_to_login(self):
        """Unauthenticated requests to the PDF route are protected by admin_site.admin_view."""
        entry = FinanceEntry.objects.create(
            entry_type='REVENUE',
            category='SALES',
            amount=Decimal('30000.00'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser
        )

        anon_client = Client()
        url = reverse('admin:financeentry-pdf', args=[entry.pk])
        response = anon_client.get(url)

        # Should redirect to admin login
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_finance_entry_serializer_exposes_entry_code(self):
        """FinanceEntrySerializer outputs entry_code, category_display, and suppresses raw database ID."""
        entry = FinanceEntry.objects.create(
            entry_type='REVENUE',
            category='SALES',
            amount=Decimal('45000.00'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser,
            reference_document="SINV-202608-0042",
            description="API transaction test"
        )

        serializer = FinanceEntrySerializer(entry)
        data = serializer.data

        self.assertEqual(data['entry_code'], entry.entry_code)
        self.assertEqual(data['entry_type_display'], 'Revenue')
        self.assertEqual(data['category_display'], 'Sales')
        self.assertEqual(data['logged_by_username'], 'admin_finance')
        self.assertEqual(data['reference_document'], "SINV-202608-0042")
        self.assertNotIn('id', data)
        self.assertNotIn('finance_entry_id', data)

    def test_finance_entry_pdf_long_text_wrapping(self):
        """Long audit notes and lengthy reference strings wrap in flowable ReportLab table without error."""
        long_audit_description = (
            "Detailed General Ledger Audit Log: This transaction reflects a complex reconciliation "
            "of batch variance scrap from Work Order #WOC-9821, combined with secondary customer "
            "credit note offset CN-202608-0012, verified by the chief accounting controller on the shop floor. "
            "All underlying raw material lot allocations and inventory journals have been verified."
        )
        long_reference_doc = "SINV-202608-9999 / DISP-202608-8888 / WO-202608-7777-SPECIAL-CUSTOM-ORDER"

        entry = FinanceEntry.objects.create(
            entry_type='EXPENSE',
            category='LOSS',
            amount=Decimal('18750.50'),
            entry_date=timezone.now().date(),
            logged_by=self.superuser,
            reference_document=long_reference_doc,
            description=long_audit_description
        )

        pdf_buffer = generate_finance_entry_pdf(entry)
        self.assertIsNotNone(pdf_buffer)
        pdf_bytes = pdf_buffer.getvalue()
        self.assertTrue(len(pdf_bytes) > 1000)
        self.assertTrue(pdf_bytes.startswith(b'%PDF'))
