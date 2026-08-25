"""
TESTS: Credit Note Auto-Generation & Consecutive Invoice Deduction Subsystem
Verifies:
1. Auto-generation of CreditNote on surplus bulk customer deposits.
2. Auto-generation of CreditNote and lines on RMA Return QC approval.
3. Automatic FIFO deduction of available CreditNotes against newly generated consecutive invoices with full audit trail.
4. Auto-settlement of existing open debt when a new CreditNote is issued.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Customer, Product, SalesOrder, SalesOrderItem, SalesInvoice,
    SalesInvoiceLine, SalesInvoicePayments, CreditNote, CreditNoteLine,
    DispatchRecord, Return, FinanceEntry
)
from core.services import (
    execute_customer_bulk_allocation,
    apply_customer_credit_notes_to_invoice,
    apply_credit_note_to_open_invoices
)

User = get_user_model()


class CreditNoteAutoGenerationTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username='admin_user',
            email='admin@example.com',
            password='Password123!'
        )

        self.customer = Customer.objects.create(
            customer_name="Nairobi Glass & Glazing Ltd",
            contact_info="info@nairobiglass.com",
            shipping_address="Industrial Area, Road A, Nairobi"
        )

        self.product = Product.objects.create(
            name="Standard Linseed Putty 5kg",
            sku="FG-PUTTY-5KG-001",
            product_type="FINISHED",
            category="Linseed Putty",
            unit_of_measurement="kg",
            selling_price=Decimal("1000.00")
        )

    def test_surplus_deposit_auto_generates_posted_credit_note(self):
        """Verify that an overpayment/surplus in bulk deposit creates an active CreditNote."""
        # Invoice 1: 20 units @ 1000 = $20,000
        inv1 = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=inv1,
            product=self.product,
            quantity=Decimal("20.00"),
            unit_price=Decimal("1000.00")
        )
        inv1.recalculate_totals(save=True)

        # Deposit $35,000 ($20,000 for invoice + $15,000 surplus)
        result = execute_customer_bulk_allocation(
            customer=self.customer,
            total_received=Decimal("35000.00"),
            payment_method="BANK_TRANSFER",
            reference="TXN-BULK-SURPLUS-001"
        )

        self.assertEqual(result['total_allocated'], Decimal("20000.00"))
        self.assertEqual(result['unallocated_amount'], Decimal("15000.00"))

        # Verify Invoice 1 is fully paid
        inv1.refresh_from_db()
        self.assertEqual(inv1.status, 'PAID')
        self.assertEqual(inv1.remaining_balance, Decimal('0.00'))

        # Verify CreditNote was auto-generated for the $15,000 surplus
        surplus_cn = CreditNote.objects.filter(customer=self.customer, status='POSTED').first()
        self.assertIsNotNone(surplus_cn)
        self.assertEqual(surplus_cn.total_amount, Decimal("15000.00"))
        self.assertEqual(surplus_cn.applied_amount, Decimal("0.00"))
        self.assertEqual(surplus_cn.remaining_credit, Decimal("15000.00"))
        self.assertIn("Surplus credit balance", surplus_cn.reason)

    def test_consecutive_invoices_auto_deducted_from_open_credit_notes(self):
        """Verify consecutive invoices have grand totals deducted from open credit notes in FIFO order."""
        # 1. Create an active CreditNote of $15,000 for customer
        cn = CreditNote.objects.create(
            customer=self.customer,
            issue_date=timezone.now().date(),
            status='POSTED',
            subtotal=Decimal("15000.00"),
            total_amount=Decimal("15000.00"),
            reason="Customer Retainer / Advance Overpayment"
        )
        self.assertEqual(cn.remaining_credit, Decimal("15000.00"))

        # 2. Consecutive Sales Order #1 for $10,000 (10 units @ 1000)
        so1 = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so1,
            product=self.product,
            quantity_ordered=Decimal("10.00"),
            unit_price=Decimal("1000.00")
        )

        inv1 = so1.confirm_and_generate_invoice()
        self.assertIsNotNone(inv1)
        inv1.refresh_from_db()

        # Verify Invoice 1 was fully settled by the credit note deduction
        self.assertEqual(inv1.total_amount, Decimal("10000.00"))
        self.assertEqual(inv1.total_paid, Decimal("10000.00"))
        self.assertEqual(inv1.remaining_balance, Decimal("0.00"))
        self.assertEqual(inv1.status, 'PAID')

        # Verify payment audit trail
        payment1 = SalesInvoicePayments.objects.filter(invoice=inv1).first()
        self.assertIsNotNone(payment1)
        self.assertEqual(payment1.amount, Decimal("10000.00"))
        self.assertEqual(payment1.reference_number, f"CREDIT-APPLIED-{cn.credit_note_number}")

        # Verify CreditNote updated state
        cn.refresh_from_db()
        self.assertEqual(cn.applied_amount, Decimal("10000.00"))
        self.assertEqual(cn.remaining_credit, Decimal("5000.00"))
        self.assertEqual(cn.status, 'POSTED')

        # 3. Consecutive Sales Order #2 for $8,000 (8 units @ 1000)
        so2 = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so2,
            product=self.product,
            quantity_ordered=Decimal("8.00"),
            unit_price=Decimal("1000.00")
        )

        inv2 = so2.confirm_and_generate_invoice()
        self.assertIsNotNone(inv2)
        inv2.refresh_from_db()

        # Verify Invoice 2 has remaining $5,000 deducted, leaving $3,000 due
        self.assertEqual(inv2.total_amount, Decimal("8000.00"))
        self.assertEqual(inv2.total_paid, Decimal("5000.00"))
        self.assertEqual(inv2.remaining_balance, Decimal("3000.00"))
        self.assertEqual(inv2.status, 'PARTIALLY_PAID')

        # Verify CreditNote is now fully utilized
        cn.refresh_from_db()
        self.assertEqual(cn.applied_amount, Decimal("15000.00"))
        self.assertEqual(cn.remaining_credit, Decimal("0.00"))
        self.assertEqual(cn.status, 'POSTED')

    def test_rma_return_approval_auto_generates_credit_note_and_settles_existing_debt(self):
        """Verify RMA Return QC approval creates CreditNote and immediately offsets existing customer debt."""
        # 1. Existing unpaid invoice for $1,000
        inv = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='POSTED'
        )
        SalesInvoiceLine.objects.create(
            invoice=inv,
            product=self.product,
            quantity=Decimal("1.00"),
            unit_price=Decimal("1000.00")
        )
        inv.recalculate_totals(save=True)
        self.assertEqual(inv.remaining_balance, Decimal("1000.00"))

        # 2. Stock inventory and dispatch 3 units
        from core.models import Inventory
        Inventory.objects.update_or_create(
            product=self.product,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('50.00')}
        )

        so = SalesOrder.objects.create(customer=self.customer, invoicing_policy='ORDER_BASED', status='approved')
        so_item = SalesOrderItem.objects.create(sales_order=so, product=self.product, quantity_ordered=Decimal("3.00"), unit_price=Decimal("1000.00"))
        dispatch = DispatchRecord.objects.create(
            sales_order_item=so_item,
            product=self.product,
            customer=self.customer,
            quantity_dispatched=Decimal("3.00"),
            status='delivered'
        )

        # 3. Create Return for 2 units ($2,000 value), QC status PENDING
        ret = Return.objects.create(
            dispatch=dispatch,
            customer=self.customer,
            quantity_returned=Decimal("2.00"),
            reason_for_return="Damaged on transit",
            quality_control_status='PENDING'
        )

        # No credit note yet while PENDING
        self.assertEqual(CreditNote.objects.filter(customer=self.customer).count(), 0)

        # 4. Transition QC status to APPROVED
        ret.quality_control_status = 'APPROVED'
        ret.save()

        # Verify CreditNote was auto-generated for $2,000
        cn = CreditNote.objects.filter(customer=self.customer).first()
        self.assertIsNotNone(cn)
        self.assertEqual(cn.total_amount, Decimal("2000.00"))
        self.assertEqual(cn.lines.count(), 1)
        self.assertEqual(cn.lines.first().total_price, Decimal("2000.00"))

        # Verify $1,000 was immediately applied to settle existing unpaid invoice
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'PAID')
        self.assertEqual(inv.remaining_balance, Decimal("0.00"))

        # Verify CreditNote remaining balance is $1,000
        cn.refresh_from_db()
        self.assertEqual(cn.applied_amount, Decimal("1000.00"))
        self.assertEqual(cn.remaining_credit, Decimal("1000.00"))
        self.assertEqual(cn.status, 'POSTED')
