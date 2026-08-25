"""
AUTOMATED TEST SUITE FOR CUSTOMER BULK PAYMENT ALLOCATION (core/tests/test_customer_payment_allocation.py)

Tests FIFO lump-sum payment distribution, dry-run preview simulation,
atomic database settlement, FinanceEntry integration, and RBAC guards.
"""

from decimal import Decimal
from django.utils import timezone
from django.contrib.auth.models import User, Group
from rest_framework.test import APITestCase
from rest_framework import status

from core.models import (
    Customer, Product, SalesInvoice, SalesInvoiceLine,
    SalesInvoicePayments, FinanceEntry, Inventory
)


class CustomerPaymentAllocationTests(APITestCase):
    """
    Integration test suite for FIFO customer-level bulk payment processing.
    """

    def setUp(self):
        # 1. Groups & Users
        self.billing_group, _ = Group.objects.get_or_create(name='Billing Officer')
        self.sales_group, _ = Group.objects.get_or_create(name='Sales Representative')
        self.operator_group, _ = Group.objects.get_or_create(name='Shop-Floor Operator')

        self.superuser = User.objects.create_superuser(
            username='admin_user',
            email='admin@glassputty.com',
            password='Password123!'
        )
        self.billing_user = User.objects.create_user(
            username='billing_officer',
            email='billing@glassputty.com',
            password='Password123!'
        )
        self.billing_user.groups.add(self.billing_group)

        self.operator_user = User.objects.create_user(
            username='shop_operator',
            email='operator@glassputty.com',
            password='Password123!'
        )
        self.operator_user.groups.add(self.operator_group)

        # 2. Master Domain Data
        self.customer = Customer.objects.create(
            customer_name="Nairobi Glass & Glazing Ltd",
            contact_info="accounts@nairobiglass.co.ke",
            shipping_address="Plot 45, Enterprise Road, Industrial Area"
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
            location="Main Warehouse",
            defaults={'quantity_available': Decimal("1000.00")}
        )

        # 3. Create 3 Chronological Open Invoices
        # Invoice 1: 20,000 KES (Oldest)
        self.inv1 = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date() - timezone.timedelta(days=30),
            status='POSTED',
            subtotal=Decimal('20000.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('20000.00')
        )
        # Invoice 2: 30,000 KES (Middle)
        self.inv2 = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date() - timezone.timedelta(days=15),
            status='POSTED',
            subtotal=Decimal('30000.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('30000.00')
        )
        # Invoice 3: 80,000 KES (Newest)
        self.inv3 = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date() - timezone.timedelta(days=5),
            status='POSTED',
            subtotal=Decimal('80000.00'),
            tax_amount=Decimal('0.00'),
            total_amount=Decimal('80000.00')
        )

    # -------------------------------------------------------------------------
    # TEST 1: Preview Allocation does NOT mutate database (Dry-Run Simulation)
    # -------------------------------------------------------------------------
    def test_preview_allocation_does_not_mutate_database(self):
        """
        Post to preview-payment-allocation with 100,000 KES.
        Verify simulated FIFO breakdown without writing any payments or changing statuses.
        """
        self.client.force_authenticate(user=self.billing_user)
        payload = {
            "amount": "100000.00",
            "payment_method": "BANK_TRANSFER",
            "reference": "TX-PREVIEW-001"
        }
        url = f'/api/sales/customers/{self.customer.pk}/preview-payment-allocation/'
        response = self.client.post(url, data=payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertEqual(data['customer_id'], self.customer.pk)
        self.assertEqual(Decimal(str(data['total_received'])), Decimal('100000.00'))
        self.assertEqual(Decimal(str(data['total_allocated'])), Decimal('100000.00'))
        self.assertEqual(Decimal(str(data['unallocated_amount'])), Decimal('0.00'))
        self.assertEqual(len(data['allocations']), 3)

        # Slice 1: Inv 1 (20,000) -> 100% covered -> projected PAID
        slice1 = data['allocations'][0]
        self.assertEqual(slice1['invoice_id'], self.inv1.pk)
        self.assertEqual(Decimal(str(slice1['allocated_amount'])), Decimal('20000.00'))
        self.assertEqual(Decimal(str(slice1['balance_after'])), Decimal('0.00'))
        self.assertEqual(slice1['projected_status'], 'PAID')

        # Slice 2: Inv 2 (30,000) -> 100% covered -> projected PAID
        slice2 = data['allocations'][1]
        self.assertEqual(slice2['invoice_id'], self.inv2.pk)
        self.assertEqual(Decimal(str(slice2['allocated_amount'])), Decimal('30000.00'))
        self.assertEqual(Decimal(str(slice2['balance_after'])), Decimal('0.00'))
        self.assertEqual(slice2['projected_status'], 'PAID')

        # Slice 3: Inv 3 (80,000) -> 50,000 covered -> projected PARTIALLY_PAID
        slice3 = data['allocations'][2]
        self.assertEqual(slice3['invoice_id'], self.inv3.pk)
        self.assertEqual(Decimal(str(slice3['allocated_amount'])), Decimal('50000.00'))
        self.assertEqual(Decimal(str(slice3['balance_after'])), Decimal('30000.00'))
        self.assertEqual(slice3['projected_status'], 'PARTIALLY_PAID')

        # Zero DB Mutations
        self.assertEqual(SalesInvoicePayments.objects.count(), 0)
        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.inv3.refresh_from_db()
        self.assertEqual(self.inv1.status, 'POSTED')
        self.assertEqual(self.inv2.status, 'POSTED')
        self.assertEqual(self.inv3.status, 'POSTED')

    # -------------------------------------------------------------------------
    # TEST 2: Execute Bulk Allocation FIFO Success (Atomic Settlement)
    # -------------------------------------------------------------------------
    def test_execute_bulk_allocation_fifo_success(self):
        """
        Post to allocate-payment with 100,000 KES.
        Verify 3 SalesInvoicePayments created, statuses updated, and GL entries written.
        """
        self.client.force_authenticate(user=self.billing_user)
        payload = {
            "amount": "100000.00",
            "payment_method": "BANK_TRANSFER",
            "reference": "BANK-DEPOSIT-100K"
        }
        url = f'/api/sales/customers/{self.customer.pk}/allocate-payment/'
        response = self.client.post(url, data=payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertEqual(Decimal(str(data['total_received'])), Decimal('100000.00'))
        self.assertEqual(Decimal(str(data['total_allocated'])), Decimal('100000.00'))
        self.assertEqual(Decimal(str(data['unallocated_amount'])), Decimal('0.00'))
        self.assertEqual(len(data['allocations']), 3)

        # Verify DB Records
        self.assertEqual(SalesInvoicePayments.objects.count(), 3)

        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.status, 'PAID')
        self.assertEqual(self.inv1.total_paid, Decimal('20000.00'))
        self.assertEqual(self.inv1.remaining_balance, Decimal('0.00'))

        self.inv2.refresh_from_db()
        self.assertEqual(self.inv2.status, 'PAID')
        self.assertEqual(self.inv2.total_paid, Decimal('30000.00'))
        self.assertEqual(self.inv2.remaining_balance, Decimal('0.00'))

        self.inv3.refresh_from_db()
        self.assertEqual(self.inv3.status, 'PARTIALLY_PAID')
        self.assertEqual(self.inv3.total_paid, Decimal('50000.00'))
        self.assertEqual(self.inv3.remaining_balance, Decimal('30000.00'))

        # Verify Finance Entries for the payments
        fe1 = FinanceEntry.objects.filter(sales_invoice=self.inv1, amount=Decimal('20000.00'), category='SALES').first()
        self.assertIsNotNone(fe1)
        fe2 = FinanceEntry.objects.filter(sales_invoice=self.inv2, amount=Decimal('30000.00'), category='SALES').first()
        self.assertIsNotNone(fe2)
        fe3 = FinanceEntry.objects.filter(sales_invoice=self.inv3, amount=Decimal('50000.00'), category='SALES').first()
        self.assertIsNotNone(fe3)

    # -------------------------------------------------------------------------
    # TEST 3: Exact Full Settlement Allocation
    # -------------------------------------------------------------------------
    def test_exact_full_settlement_allocation(self):
        """
        Deposit exact total debt (130,000 KES) and verify all 3 invoices become PAID.
        """
        self.client.force_authenticate(user=self.billing_user)
        payload = {
            "amount": "130000.00",
            "payment_method": "BANK_TRANSFER",
            "reference": "EXACT-PAY-130K"
        }
        url = f'/api/sales/customers/{self.customer.pk}/allocate-payment/'
        response = self.client.post(url, data=payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertEqual(Decimal(str(data['total_allocated'])), Decimal('130000.00'))
        self.assertEqual(Decimal(str(data['unallocated_amount'])), Decimal('0.00'))

        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.inv3.refresh_from_db()

        self.assertEqual(self.inv1.status, 'PAID')
        self.assertEqual(self.inv2.status, 'PAID')
        self.assertEqual(self.inv3.status, 'PAID')
        self.assertEqual(self.inv3.remaining_balance, Decimal('0.00'))

    # -------------------------------------------------------------------------
    # TEST 4: Overpayment Reports Unallocated Amount
    # -------------------------------------------------------------------------
    def test_overpayment_reports_unallocated_amount(self):
        """
        Deposit 150,000 KES against 130,000 KES debt.
        Verify all 3 invoices are PAID, allocated is 130,000 KES, and unallocated is 20,000 KES.
        """
        self.client.force_authenticate(user=self.billing_user)
        payload = {
            "amount": "150000.00",
            "payment_method": "BANK_TRANSFER",
            "reference": "OVERPAY-150K"
        }
        url = f'/api/sales/customers/{self.customer.pk}/allocate-payment/'
        response = self.client.post(url, data=payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        self.assertEqual(Decimal(str(data['total_received'])), Decimal('150000.00'))
        self.assertEqual(Decimal(str(data['total_allocated'])), Decimal('130000.00'))
        self.assertEqual(Decimal(str(data['unallocated_amount'])), Decimal('20000.00'))

        self.inv1.refresh_from_db()
        self.inv2.refresh_from_db()
        self.inv3.refresh_from_db()

        self.assertEqual(self.inv1.status, 'PAID')
        self.assertEqual(self.inv2.status, 'PAID')
        self.assertEqual(self.inv3.status, 'PAID')

    # -------------------------------------------------------------------------
    # TEST 5: Manual Invoice Payment still functions independently
    # -------------------------------------------------------------------------
    def test_manual_invoice_payment_still_functions_independently(self):
        """
        Verify POST /api/sales/invoices/{id}/record-payment/ still works independently.
        """
        self.client.force_authenticate(user=self.billing_user)
        payload = {
            "amount": "10000.00",
            "payment_method": "CASH",
            "reference": "MANUAL-CASH-REC"
        }
        url = f'/api/sales/invoices/{self.inv1.pk}/record-payment/'
        response = self.client.post(url, data=payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inv1.refresh_from_db()
        self.assertEqual(self.inv1.status, 'PARTIALLY_PAID')
        self.assertEqual(self.inv1.total_paid, Decimal('10000.00'))
        self.assertEqual(self.inv1.remaining_balance, Decimal('10000.00'))

    # -------------------------------------------------------------------------
    # TEST 6: Shop-Floor Operator cannot allocate bulk payments
    # -------------------------------------------------------------------------
    def test_operator_cannot_allocate_bulk_payments(self):
        """
        A shop-floor operator receives HTTP 403 Forbidden when attempting
        to preview or allocate bulk customer payments.
        """
        self.client.force_authenticate(user=self.operator_user)
        payload = {
            "amount": "50000.00",
            "payment_method": "BANK_TRANSFER",
            "reference": "UNAUTH-ATTEMPT"
        }

        # Preview action -> 403 Forbidden
        preview_url = f'/api/sales/customers/{self.customer.pk}/preview-payment-allocation/'
        preview_resp = self.client.post(preview_url, data=payload, format='json')
        self.assertEqual(preview_resp.status_code, status.HTTP_403_FORBIDDEN)

        # Allocate action -> 403 Forbidden
        alloc_url = f'/api/sales/customers/{self.customer.pk}/allocate-payment/'
        alloc_resp = self.client.post(alloc_url, data=payload, format='json')
        self.assertEqual(alloc_resp.status_code, status.HTTP_403_FORBIDDEN)
