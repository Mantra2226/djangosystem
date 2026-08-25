"""
AUTOMATED TEST SUITE FOR SALES & BILLING REST API & PDF GENERATION (MILESTONE 3)
(core/tests/test_sales_billing_api.py)

Tests Order-to-Cash (O2C) endpoints, ReportLab PDF rendering, DRF permissions,
payment processing, and credit note management.
"""

from decimal import Decimal
from django.utils import timezone
from django.contrib.auth.models import User, Group, Permission
from django.contrib.contenttypes.models import ContentType
from rest_framework.test import APITestCase
from rest_framework import status

from core.models import (
    Customer, Product, SalesOrder, SalesOrderItem, SalesInvoice,
    SalesInvoiceLine, SalesInvoicePayments, CreditNote, CreditNoteLine,
    FinanceEntry, Inventory
)
from core.utils.pdf_generator import generate_invoice_pdf, generate_credit_note_pdf


class SalesBillingAPITests(APITestCase):
    """
    Comprehensive test suite covering Milestone 3 REST endpoints and PDF generation.
    """

    def setUp(self):
        # 1. Create Groups
        self.sales_group, _ = Group.objects.get_or_create(name='Sales Representative')
        self.billing_group, _ = Group.objects.get_or_create(name='Billing Officer')
        self.supervisor_group, _ = Group.objects.get_or_create(name='Production Supervisor')
        self.operator_group, _ = Group.objects.get_or_create(name='Shop-Floor Operator')

        # 2. Create Users
        self.superuser = User.objects.create_superuser(
            username='admin_billing',
            email='admin@glassputty.com',
            password='Password123!'
        )
        self.sales_user = User.objects.create_user(
            username='sales_rep_1',
            email='sales@glassputty.com',
            password='Password123!'
        )
        self.sales_user.groups.add(self.sales_group)

        self.billing_user = User.objects.create_user(
            username='billing_officer_1',
            email='billing@glassputty.com',
            password='Password123!'
        )
        self.billing_user.groups.add(self.billing_group)

        self.operator_user = User.objects.create_user(
            username='operator_1',
            email='operator@glassputty.com',
            password='Password123!'
        )
        self.operator_user.groups.add(self.operator_group)

        # 3. Master Domain Data: Glass Putty Manufacturing
        self.customer = Customer.objects.create(
            customer_name="Prime Glazing Ltd",
            contact_info="procurement@primeglazing.com",
            shipping_address="102 Industrial Way, Building 4"
        )

        self.glass_putty_5kg = Product.objects.create(
            name="Glass Putty 5kg Tin",
            sku="FG-PUTTY-5KG",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="tin",
            selling_price=Decimal("25.00")
        )

        self.inventory, _ = Inventory.objects.get_or_create(
            product=self.glass_putty_5kg,
            location="Main Finished Goods Warehouse",
            defaults={'quantity_available': Decimal('500.00')}
        )

    # -------------------------------------------------------------------------
    # TEST 1: Create Sales Order with nested lines via REST API
    # -------------------------------------------------------------------------
    def test_create_sales_order_with_nested_lines_via_api(self):
        """
        Create an order with items via POST /api/sales/orders/ and verify unit prices
        automatically freeze to catalog selling price ($25.00) when omitted.
        """
        self.client.force_authenticate(user=self.sales_user)
        payload = {
            "customer": self.customer.pk,
            "invoicing_policy": "ORDER_BASED",
            "items": [
                {
                    "product": self.glass_putty_5kg.pk,
                    "quantity_ordered": "10.00"
                }
            ]
        }
        response = self.client.post('/api/sales/orders/', data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data

        self.assertTrue(data['order_number'].startswith('SO-'))
        self.assertEqual(data['customer'], self.customer.pk)
        self.assertEqual(data['customer_name'], "Prime Glazing Ltd")
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(Decimal(str(data['items'][0]['unit_price'])), Decimal('25.00'))
        self.assertEqual(Decimal(str(data['items'][0]['total_price'])), Decimal('250.00'))
        self.assertEqual(Decimal(str(data['total_amount'])), Decimal('250.00'))

    # -------------------------------------------------------------------------
    # TEST 2: Confirm ORDER_BASED Sales Order via REST API
    # -------------------------------------------------------------------------
    def test_confirm_order_based_sales_order_via_api(self):
        """
        Trigger POST /api/sales/orders/{id}/confirm/ and verify immediate commercial invoice
        and line generation with status 'POSTED'.
        """
        self.client.force_authenticate(user=self.sales_user)
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal('20.00'),
            unit_price=Decimal('25.00')
        )

        response = self.client.post(f'/api/sales/orders/{so.pk}/confirm/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertIn('sales_order', data)
        self.assertIn('invoice', data)
        self.assertEqual(data['sales_order']['status'], 'approved')

        inv_data = data['invoice']
        self.assertIsNotNone(inv_data)
        self.assertTrue(inv_data['invoice_number'].startswith('SINV-'))
        self.assertEqual(inv_data['status'], 'POSTED')
        self.assertEqual(Decimal(str(inv_data['total_amount'])), Decimal('500.00'))
        self.assertEqual(len(inv_data['lines']), 1)
        self.assertEqual(inv_data['lines'][0]['product_name'], "Glass Putty 5kg Tin")

    # -------------------------------------------------------------------------
    # TEST 3: Confirm DELIVERY_BASED Sales Order defers invoice
    # -------------------------------------------------------------------------
    def test_confirm_delivery_based_sales_order_defers_invoice_via_api(self):
        """
        Confirm order with invoicing_policy='DELIVERY_BASED' and verify order status
        becomes approved with no invoice generated upfront.
        """
        self.client.force_authenticate(user=self.sales_user)
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='DELIVERY_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal('15.00'),
            unit_price=Decimal('25.00')
        )

        response = self.client.post(f'/api/sales/orders/{so.pk}/confirm/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertEqual(data['sales_order']['status'], 'approved')
        self.assertIsNone(data['invoice'])
        self.assertEqual(SalesInvoice.objects.filter(sales_order=so).count(), 0)

    # -------------------------------------------------------------------------
    # TEST 4: Invoice list and detail endpoints
    # -------------------------------------------------------------------------
    def test_invoice_list_and_detail_endpoints(self):
        """
        Verify GET /api/sales/invoices/ and GET /api/sales/invoices/{id}/ return itemized snapshot lines.
        """
        self.client.force_authenticate(user=self.billing_user)
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED',
            subtotal=Decimal('250.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('250.00')
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.glass_putty_5kg,
            quantity=Decimal('10.00'),
            unit_price=Decimal('25.00'),
            tax_rate=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            subtotal=Decimal('250.00'),
            total_price=Decimal('250.00')
        )

        # List endpoint
        list_resp = self.client.get('/api/sales/invoices/')
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(list_resp.data), 1)

        # Detail endpoint
        detail_resp = self.client.get(f'/api/sales/invoices/{invoice.pk}/')
        self.assertEqual(detail_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_resp.data['invoice_number'], invoice.invoice_number)
        self.assertEqual(detail_resp.data['customer_name'], "Prime Glazing Ltd")
        self.assertEqual(len(detail_resp.data['lines']), 1)
        self.assertEqual(detail_resp.data['lines'][0]['product_name'], "Glass Putty 5kg Tin")

    # -------------------------------------------------------------------------
    # TEST 5: Record full payment updates status to PAID and logs FinanceEntry
    # -------------------------------------------------------------------------
    def test_record_payment_updates_invoice_status_to_paid(self):
        """
        Post full payment via /api/sales/invoices/{id}/record-payment/ and verify
        status flips to PAID and writes a FinanceEntry to General Ledger.
        """
        self.client.force_authenticate(user=self.billing_user)
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED',
            subtotal=Decimal('250.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('250.00')
        )

        payload = {
            "amount": "250.00",
            "payment_method": "BANK_TRANSFER",
            "reference": "BANK-TX-998811"
        }
        response = self.client.post(f'/api/sales/invoices/{invoice.pk}/record-payment/', data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'PAID')

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'PAID')
        self.assertEqual(invoice.total_paid, Decimal('250.00'))
        self.assertEqual(invoice.remaining_balance, Decimal('0.00'))

        # Verify Finance Entry created
        fe = FinanceEntry.objects.filter(sales_invoice=invoice, amount=Decimal('250.00'), entry_type='REVENUE').first()
        self.assertIsNotNone(fe)
        self.assertEqual(fe.category, 'SALES')

    # -------------------------------------------------------------------------
    # TEST 6: Record partial payment updates status to PARTIALLY_PAID
    # -------------------------------------------------------------------------
    def test_record_partial_payment_updates_status_to_partially_paid(self):
        """
        Post partial payment and verify status flips to PARTIALLY_PAID.
        """
        self.client.force_authenticate(user=self.billing_user)
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED',
            subtotal=Decimal('500.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('500.00')
        )

        payload = {
            "amount": "200.00",
            "payment_method": "CASH",
            "reference": "CASH-REC-001"
        }
        response = self.client.post(f'/api/sales/invoices/{invoice.pk}/record-payment/', data=payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'PARTIALLY_PAID')

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, 'PARTIALLY_PAID')
        self.assertEqual(invoice.total_paid, Decimal('200.00'))
        self.assertEqual(invoice.remaining_balance, Decimal('300.00'))

    # -------------------------------------------------------------------------
    # TEST 7: Download Invoice PDF endpoint
    # -------------------------------------------------------------------------
    def test_download_invoice_pdf_endpoint(self):
        """
        Request GET /api/sales/invoices/{id}/pdf/ and assert HTTP 200,
        content_type='application/pdf', valid PDF magic bytes, and attachment headers.
        """
        self.client.force_authenticate(user=self.billing_user)
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED',
            subtotal=Decimal('250.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('250.00')
        )
        SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.glass_putty_5kg,
            quantity=Decimal('10.00'),
            unit_price=Decimal('25.00'),
            tax_rate=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            subtotal=Decimal('250.00'),
            total_price=Decimal('250.00')
        )

        response = self.client.get(f'/api/sales/invoices/{invoice.pk}/pdf/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response['Content-Type'].startswith('application/pdf'))
        self.assertIn(f'filename="Invoice_{invoice.invoice_number}.pdf"', response['Content-Disposition'])
        self.assertTrue(len(response.content) > 100)
        self.assertTrue(response.content.startswith(b'%PDF'))

    # -------------------------------------------------------------------------
    # TEST 8: Download Credit Note PDF endpoint
    # -------------------------------------------------------------------------
    def test_download_credit_note_pdf_endpoint(self):
        """
        Request GET /api/sales/credit-notes/{id}/pdf/ and assert HTTP 200,
        content_type='application/pdf', and valid PDF payload.
        """
        self.client.force_authenticate(user=self.billing_user)
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED',
            subtotal=Decimal('250.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('250.00')
        )
        credit_note = CreditNote.objects.create(
            invoice=invoice,
            customer=self.customer,
            status='POSTED',
            reason="Customer RMA - Seal Defect on 2 Tins",
            subtotal=Decimal('50.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('50.00')
        )
        CreditNoteLine.objects.create(
            credit_note=credit_note,
            product=self.glass_putty_5kg,
            quantity=Decimal('2.00'),
            unit_price=Decimal('25.00'),
            tax_rate=Decimal('0.00'),
            tax_amount=Decimal('0.00'),
            subtotal=Decimal('50.00'),
            total_price=Decimal('50.00')
        )

        response = self.client.get(f'/api/sales/credit-notes/{credit_note.pk}/pdf/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response['Content-Type'].startswith('application/pdf'))
        self.assertIn(f'filename="CreditNote_{credit_note.credit_note_number}.pdf"', response['Content-Disposition'])
        self.assertTrue(len(response.content) > 100)
        self.assertTrue(response.content.startswith(b'%PDF'))

    # -------------------------------------------------------------------------
    # TEST 9: Unauthenticated request rejected
    # -------------------------------------------------------------------------
    def test_unauthenticated_request_rejected(self):
        """
        Unauthenticated GET/POST requests return HTTP 401 Unauthorized.
        """
        self.client.force_authenticate(user=None)
        self.client.logout()

        resp1 = self.client.get('/api/sales/orders/')
        self.assertEqual(resp1.status_code, status.HTTP_401_UNAUTHORIZED)

        resp2 = self.client.post('/api/sales/orders/', data={}, format='json')
        self.assertEqual(resp2.status_code, status.HTTP_401_UNAUTHORIZED)

        resp3 = self.client.get('/api/sales/invoices/')
        self.assertEqual(resp3.status_code, status.HTTP_401_UNAUTHORIZED)

        resp4 = self.client.post('/api/sales/invoices/1/record-payment/', data={}, format='json')
        self.assertEqual(resp4.status_code, status.HTTP_401_UNAUTHORIZED)

    # -------------------------------------------------------------------------
    # TEST 10: Shop-Floor Operator cannot confirm order or record payment
    # -------------------------------------------------------------------------
    def test_operator_cannot_confirm_order_or_record_payment(self):
        """
        Standard shop-floor operator receives HTTP 403 Forbidden when attempting
        to record payment or confirm sales orders.
        """
        self.client.force_authenticate(user=self.operator_user)

        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal('10.00'),
            unit_price=Decimal('25.00')
        )

        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED',
            subtotal=Decimal('250.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('250.00')
        )

        # Operator attempts to confirm order -> 403 Forbidden
        confirm_resp = self.client.post(f'/api/sales/orders/{so.pk}/confirm/')
        self.assertEqual(confirm_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Operator attempts to record payment -> 403 Forbidden
        pay_payload = {
            "amount": "250.00",
            "payment_method": "BANK_TRANSFER",
            "reference": "OP-ATTEMPT"
        }
        pay_resp = self.client.post(f'/api/sales/invoices/{invoice.pk}/record-payment/', data=pay_payload, format='json')
        self.assertEqual(pay_resp.status_code, status.HTTP_403_FORBIDDEN)

