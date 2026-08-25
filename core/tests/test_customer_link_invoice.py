"""
TESTS: Customer Sales Invoice Linking Workflow
Verifies linking unassigned and reassigned sales invoices to customers by invoice number,
including automated credit note settlement, error handling, and Unfold admin UI views.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Customer, Product, SalesInvoice, SalesInvoiceLine, CreditNote
)

User = get_user_model()


class CustomerLinkInvoiceTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username='admin_linker',
            email='linker@example.com',
            password='Password123!'
        )
        self.client.force_login(self.superuser)

        self.customer_a = Customer.objects.create(
            customer_name="Alpha Glass Works Ltd",
            contact_info="orders@alphaglass.co.ke",
            shipping_address="Plot 10, Industrial Area, Nairobi"
        )

        self.customer_b = Customer.objects.create(
            customer_name="Beta Windows & Facades",
            contact_info="procurement@betawindows.com",
            shipping_address="Mombasa Road, Nairobi"
        )

        self.putty = Product.objects.create(
            name="Glass Putty 50kg Drum",
            sku="FG-PUTTY-50KG-TST",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="Drum",
            selling_price=Decimal("5000.00")
        )

    def test_link_unassigned_invoice_to_customer_via_invoice_number(self):
        """Unassigned invoice (customer=None) linked to Customer A by invoice number."""
        invoice = SalesInvoice.objects.create(
            customer=None,
            invoice_date=timezone.now().date(),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.putty,
            quantity=Decimal("4.00"),
            unit_price=Decimal("5000.00")
        )
        invoice.recalculate_totals(save=True)
        self.assertIsNone(invoice.customer)

        url = reverse('admin:customer-link-invoice', args=[self.customer_a.pk])
        response = self.client.post(url, {'invoice_number': invoice.invoice_number}, follow=True)

        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.customer, self.customer_a)
        self.assertIn(invoice, self.customer_a.sales_invoices.all())

    def test_link_invoice_via_numeric_id_fallback(self):
        """Entering numeric ID matches and links the invoice successfully."""
        invoice = SalesInvoice.objects.create(
            customer=None,
            invoice_date=timezone.now().date(),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.putty,
            quantity=Decimal("2.00"),
            unit_price=Decimal("5000.00")
        )
        invoice.recalculate_totals(save=True)

        url = reverse('admin:customer-link-invoice', args=[self.customer_a.pk])
        response = self.client.post(url, {'link_invoice_number': str(invoice.pk)}, follow=True)

        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.customer, self.customer_a)

    def test_reassign_invoice_from_another_customer(self):
        """Reassigning invoice from Customer B to Customer A transfers the debt."""
        invoice = SalesInvoice.objects.create(
            customer=self.customer_b,
            invoice_date=timezone.now().date(),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.putty,
            quantity=Decimal("3.00"),
            unit_price=Decimal("5000.00")
        )
        invoice.recalculate_totals(save=True)

        self.assertEqual(invoice.customer, self.customer_b)

        url = reverse('admin:customer-link-invoice', args=[self.customer_a.pk])
        response = self.client.post(url, {'invoice_number': invoice.invoice_number}, follow=True)

        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.customer, self.customer_a)

    def test_link_invoice_auto_applies_existing_customer_credit_notes(self):
        """When an invoice is linked to a customer with open credit notes, credit is auto-applied."""
        # Create posted Credit Note for Customer A with $10,000 credit
        CreditNote.objects.create(
            customer=self.customer_a,
            issue_date=timezone.now().date(),
            subtotal=Decimal("10000.00"),
            total_amount=Decimal("10000.00"),
            applied_amount=Decimal("0.00"),
            reason="Deposit Surplus Overpayment",
            status='POSTED'
        )

        # Create unassigned invoice for $25,000
        invoice = SalesInvoice.objects.create(
            customer=None,
            invoice_date=timezone.now().date(),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.putty,
            quantity=Decimal("5.00"),
            unit_price=Decimal("5000.00")
        )
        invoice.recalculate_totals(save=True)

        # Link invoice
        url = reverse('admin:customer-link-invoice', args=[self.customer_a.pk])
        response = self.client.post(url, {'invoice_number': invoice.invoice_number}, follow=True)

        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.customer, self.customer_a)
        # $10,000 credit note should be consumed, remaining invoice balance = $15,000
        self.assertEqual(invoice.remaining_balance, Decimal("15000.00"))
        self.assertEqual(invoice.status, 'PARTIALLY_PAID')

    def test_link_nonexistent_invoice_returns_error_message(self):
        """Attempting to link an invoice that does not exist gracefully notifies the user."""
        url = reverse('admin:customer-link-invoice', args=[self.customer_a.pk])
        response = self.client.post(url, {'invoice_number': 'SINV-NONEXISTENT-9999'}, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.customer_a.sales_invoices.count(), 0)

    def test_link_invoice_page_renders_get_interface(self):
        """GET request to customer-link-invoice renders the Unfold admin template cleanly."""
        url = reverse('admin:customer-link-invoice', args=[self.customer_a.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin/customer_link_invoice.html')
        self.assertEqual(response.context['customer'], self.customer_a)
