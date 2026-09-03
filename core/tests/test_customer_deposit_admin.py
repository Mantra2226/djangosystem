"""
TESTS: Customer Bulk Deposit Admin Workflow & FIFO Settlement
Verifies CustomerAdmin Accounts Receivable summary, Receive Customer Deposit action button,
dry-run FIFO preview simulation, and atomic execution settlement.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Customer, Product, SalesInvoice, SalesInvoiceLine, SalesInvoicePayments, FinanceEntry
)

User = get_user_model()


class CustomerDepositAdminTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username='admin_user',
            email='admin@example.com',
            password='Password123!'
        )
        self.client.force_login(self.superuser)

        self.customer = Customer.objects.create(
            customer_name="Nairobi Glass & Glazing Ltd",
            contact_info="info@nairobiglass.com",
            shipping_address="Industrial Area, Road A, Nairobi"
        )

        self.finished_putty = Product.objects.create(
            name="Standard Linseed Putty 5kg",
            sku="FG-PUTTY-5KG-001",
            product_type="FINISHED",
            category="Linseed Putty",
            unit_of_measurement="kg",
            selling_price=Decimal("1000.00")
        )

        # Invoice #1: 20 units @ 1000 = 20,000
        self.inv1 = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date() - timezone.timedelta(days=20),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=self.inv1,
            product=self.finished_putty,
            quantity=Decimal("20.00"),
            unit_price=Decimal("1000.00")
        )
        self.inv1.recalculate_totals(save=True)

        # Invoice #2: 30 units @ 1000 = 30,000
        self.inv2 = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date() - timezone.timedelta(days=10),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=self.inv2,
            product=self.finished_putty,
            quantity=Decimal("30.00"),
            unit_price=Decimal("1000.00")
        )
        self.inv2.recalculate_totals(save=True)

    def test_customer_admin_changelist_view_renders_receivables(self):
        """Verify Customer changelist view /admin/core/customer/ renders receivables and open invoice counts."""
        url = reverse('admin:core_customer_changelist')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nairobi Glass")
        self.assertContains(response, "KSh 50,000.00")
        self.assertContains(response, "2")

    def test_customer_admin_change_form_renders_ar_summary_and_action_button(self):
        """Verify Customer change form displays Accounts Receivable debt and Receive Deposit link."""
        url = reverse('admin:core_customer_change', args=[self.customer.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Verify Debt amount and Action links are rendered
        self.assertContains(response, "KSh 50,000.00")
        self.assertContains(response, "Outstanding Debt Pool")
        self.assertContains(response, "Receive Customer Deposit (FIFO)")
        self.assertContains(response, reverse('admin:customer-receive-deposit', args=[self.customer.pk]))

    def test_receive_deposit_view_get(self):
        """Verify GET /admin/core/customer/<pk>/receive-deposit/ renders open invoice queue."""
        url = reverse('admin:customer-receive-deposit', args=[self.customer.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, "Nairobi Glass")
        self.assertContains(response, self.inv1.invoice_number)
        self.assertContains(response, self.inv2.invoice_number)
        self.assertContains(response, "Open Invoices Queue (Chronological FIFO Order)")

    def test_receive_deposit_preview_fifo_dry_run(self):
        """Verify POST with action_preview calculates dry-run allocation without mutating database."""
        url = reverse('admin:customer-receive-deposit', args=[self.customer.pk])
        post_data = {
            'amount': '35000.00',
            'payment_method': 'BANK_TRANSFER',
            'reference': 'PREVIEW-REF-123',
            'action_preview': '1',
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, "Projected FIFO Allocation Breakdown")
        self.assertContains(response, "Simulated Dry-Run")
        self.assertContains(response, "+KSh 20,000.00") # Full Inv 1
        self.assertContains(response, "+KSh 15,000.00") # Partial Inv 2

        # Ensure NO payments were recorded in database
        self.assertEqual(SalesInvoicePayments.objects.filter(invoice__customer=self.customer).count(), 0)
        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.assertEqual(self.inv1.status, 'POSTED')
        self.assertEqual(self.inv2.status, 'POSTED')

    def test_receive_deposit_execute_atomic_settlement_with_surplus(self):
        """Verify POST with action_settle applies atomic settlement, updates invoices and GL, and logs surplus."""
        url = reverse('admin:customer-receive-deposit', args=[self.customer.pk])
        # Deposit $60,000 against $50,000 debt -> $10,000 surplus
        post_data = {
            'amount': '60000.00',
            'payment_method': 'BANK_TRANSFER',
            'reference': 'BANK-TXN-SETTLE-001',
            'action_settle': '1',
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('admin:core_customer_change', args=[self.customer.pk]))

        # Verify Invoices are now fully PAID
        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.assertEqual(self.inv1.status, 'PAID')
        self.assertEqual(self.inv1.remaining_balance, Decimal('0.00'))
        self.assertEqual(self.inv2.status, 'PAID')
        self.assertEqual(self.inv2.remaining_balance, Decimal('0.00'))

        # Verify Payment records created
        self.assertEqual(SalesInvoicePayments.objects.filter(invoice=self.inv1).count(), 1)
        self.assertEqual(SalesInvoicePayments.objects.filter(invoice=self.inv2).count(), 1)

        # Verify General Ledger Finance Entries
        gl_entries = FinanceEntry.objects.filter(category='SALES', entry_type='REVENUE')
        self.assertTrue(gl_entries.filter(amount=Decimal('20000.00')).exists())
        self.assertTrue(gl_entries.filter(amount=Decimal('30000.00')).exists())

        # Follow redirect and verify success banner with surplus notification
        follow_resp = self.client.get(response.url)
        self.assertEqual(follow_resp.status_code, 200)
        self.assertContains(follow_resp, "Successfully executed bulk deposit of KSh 60,000.00 across 2 invoice(s)")
        self.assertContains(follow_resp, "Surplus credit balance: KSh 10,000.00")
