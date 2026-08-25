"""
TESTS: Sales Order Confirmation Auto-Invoice Generation & Customer Posting Subsystem
Verifies:
1. ORDER_BASED SalesOrders auto-generate and post a SalesInvoice upon order confirmation.
2. Saving an ORDER_BASED SalesOrder with items in Admin auto-creates and posts the invoice.
3. The generated invoice is linked to both Customer and SalesOrder with full audit trail.
4. Open customer credit notes are auto-applied to the upfront invoice.
5. DELIVERY_BASED SalesOrders defer invoice creation until dispatch fulfillment.
6. Idempotency guards prevent duplicate invoices on repeated confirmations.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Customer, Product, SalesOrder, SalesOrderItem, SalesInvoice,
    SalesInvoiceLine, CreditNote
)

User = get_user_model()


class OrderConfirmationInvoiceAutogenTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username='admin_billing',
            email='billing@example.com',
            password='Password123!'
        )
        self.client.force_login(self.superuser)

        self.customer = Customer.objects.create(
            customer_name="Nairobi Glass & Glazing Ltd",
            contact_info="info@nairobiglass.com",
            shipping_address="Industrial Area, Nairobi"
        )

        self.finished_putty = Product.objects.create(
            name="Glass Putty 25kg Pack",
            sku="FG-PUTTY-25KG-TEST",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="Pack",
            selling_price=Decimal("2500.00")
        )

    def test_order_based_confirmation_generates_and_posts_invoice(self):
        """Direct confirmation of ORDER_BASED order creates and posts commercial invoice for the customer."""
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.finished_putty,
            quantity_ordered=Decimal("10.00"),
            unit_price=Decimal("2500.00")
        )

        invoice = so.confirm_and_generate_invoice()

        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.status, 'POSTED')
        self.assertEqual(invoice.customer, self.customer)
        self.assertEqual(invoice.sales_order, so)
        self.assertEqual(invoice.total_amount, Decimal("25000.00"))
        self.assertEqual(invoice.remaining_balance, Decimal("25000.00"))
        self.assertEqual(invoice.lines.count(), 1)

        # Verify customer's receivables reflect the new invoice
        open_invoices = self.customer.sales_invoices.filter(status__in=['POSTED', 'PARTIALLY_PAID'])
        self.assertEqual(open_invoices.count(), 1)
        self.assertEqual(sum(inv.remaining_balance for inv in open_invoices), Decimal("25000.00"))

    def test_admin_confirm_action_generates_and_posts_invoice(self):
        """Triggering confirm-order action in Django Admin generates and posts the invoice."""
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.finished_putty,
            quantity_ordered=Decimal("6.00"),
            unit_price=Decimal("2500.00")
        )

        url = reverse('admin:salesorder-confirm-order', args=[so.pk])
        response = self.client.post(url, follow=True)

        self.assertEqual(response.status_code, 200)
        so.refresh_from_db()
        self.assertEqual(so.status, 'approved')

        invoice = so.invoices.filter(status='POSTED').first()
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.customer, self.customer)
        self.assertEqual(invoice.total_amount, Decimal("15000.00"))

    def test_order_based_confirmation_auto_applies_existing_credit_notes(self):
        """When an invoice is auto-generated on order confirmation, customer credit is auto-deducted."""
        # Create $5,000 active credit note for customer
        CreditNote.objects.create(
            customer=self.customer,
            issue_date=timezone.now().date(),
            subtotal=Decimal("5000.00"),
            total_amount=Decimal("5000.00"),
            applied_amount=Decimal("0.00"),
            reason="Customer Deposit Surplus",
            status='POSTED'
        )

        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.finished_putty,
            quantity_ordered=Decimal("4.00"),
            unit_price=Decimal("2500.00")
        )

        invoice = so.confirm_and_generate_invoice()

        self.assertIsNotNone(invoice)
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount, Decimal("10000.00"))
        # $5,000 credit note auto-applied
        self.assertEqual(invoice.remaining_balance, Decimal("5000.00"))
        self.assertEqual(invoice.status, 'PARTIALLY_PAID')

    def test_delivery_based_invoicing_policy_defers_invoice(self):
        """DELIVERY_BASED SalesOrders do not generate an upfront invoice upon order confirmation."""
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='DELIVERY_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.finished_putty,
            quantity_ordered=Decimal("10.00"),
            unit_price=Decimal("2500.00")
        )

        invoice = so.confirm_and_generate_invoice()

        self.assertIsNone(invoice)
        self.assertEqual(so.invoices.count(), 0)
        self.assertEqual(so.status, 'approved')

    def test_idempotent_order_confirmation_prevents_duplicate_invoices(self):
        """Calling confirm_and_generate_invoice multiple times returns the single active invoice."""
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.finished_putty,
            quantity_ordered=Decimal("5.00"),
            unit_price=Decimal("2500.00")
        )

        inv1 = so.confirm_and_generate_invoice()
        inv2 = so.confirm_and_generate_invoice()
        inv3 = so.confirm_and_generate_invoice()

        self.assertEqual(inv1.pk, inv2.pk)
        self.assertEqual(inv2.pk, inv3.pk)
        self.assertEqual(so.invoices.count(), 1)

    def test_sales_order_admin_change_form_loads_invoices_viewer_cleanly(self):
        """Admin change form renders invoices_viewer without management form error."""
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.finished_putty,
            quantity_ordered=Decimal("2.00"),
            unit_price=Decimal("2500.00")
        )
        so.confirm_and_generate_invoice()

        url = reverse('admin:core_salesorder_change', args=[so.pk])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Commercial Invoices")

    def test_sales_order_admin_add_form_saves_cleanly_without_management_form_errors(self):
        """Admin add form submits and saves with items without missing management form errors."""
        url = reverse('admin:core_salesorder_add')
        post_data = {
            'customer': str(self.customer.pk),
            'invoicing_policy': 'ORDER_BASED',
            'items-TOTAL_FORMS': '1',
            'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0',
            'items-MAX_NUM_FORMS': '1000',
            'items-0-product': str(self.finished_putty.pk),
            'items-0-quantity_ordered': '8.00',
            'items-0-unit_price': '2500.00',
        }
        response = self.client.post(url, post_data, follow=True)

        self.assertEqual(response.status_code, 200)
        so = SalesOrder.objects.filter(customer=self.customer).order_by('-created_at').first()
        self.assertIsNotNone(so)
        self.assertEqual(so.items.count(), 1)
        # Verify invoice auto-generated upon admin save
        invoice = so.invoices.filter(status='POSTED').first()
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.total_amount, Decimal("20000.00"))
