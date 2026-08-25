from decimal import Decimal
from django.test import TestCase, RequestFactory
from django.utils import timezone
from django.contrib.auth.models import User, AnonymousUser
from django.contrib.admin.sites import AdminSite
from core.models import (
    Customer, Product, SalesOrder, SalesOrderItem, SalesInvoice,
    SalesInvoiceLine, SalesInvoicePayments, CreditNote, CreditNoteLine,
    DispatchRecord, Return, Inventory, FinanceEntry, StockTransaction
)
from core.admin import (
    SalesOrderAdmin, SalesOrderItemInline, SalesInvoiceAdmin,
    SalesInvoiceLineInline, CreditNoteAdmin, CreditNoteLineInline
)


class DummyAdminSite(AdminSite):
    pass


class SalesBillingMilestone2Tests(TestCase):
    """
    Test suite for Milestone 2: State Machine, Idempotency, RMA Credit Notes,
    General Ledger Auto-Posting, and Admin Immutability Guards.
    """

    def setUp(self):
        self.site = DummyAdminSite()
        self.factory = RequestFactory()

        # Users
        self.superuser = User.objects.create_superuser('admin_user', 'admin@example.com', 'pass123')
        self.staff_user = User.objects.create_user('staff_user', 'staff@example.com', 'pass123', is_staff=True)
        from django.contrib.auth.models import Permission
        self.staff_user.user_permissions.set(Permission.objects.filter(content_type__app_label='core'))

        # Domain Master Data
        self.customer = Customer.objects.create(
            customer_name="Prime Glazing Ltd",
            contact_info="orders@primeglazing.com",
            shipping_address="102 Industrial Way"
        )
        self.glass_putty_5kg = Product.objects.create(
            name="Glass Putty 5kg Tin",
            sku="FG-PUTTY-5KG",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="tin",
            selling_price=Decimal("25.00")
        )
        # Initial Inventory for finished goods
        self.inventory, _ = Inventory.objects.get_or_create(
            product=self.glass_putty_5kg,
            location="Main Warehouse",
            defaults={'quantity_available': Decimal("100.00")}
        )
        self.inventory.quantity_available = Decimal("100.00")
        self.inventory.save()

    def test_confirm_order_idempotency_prevents_duplicate_invoices(self):
        """
        Calling confirm_and_generate_invoice() multiple times on the same SalesOrder
        returns the existing invoice without creating duplicate invoices or line items.
        """
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("10.00")
        )

        # First confirmation
        inv1 = so.confirm_and_generate_invoice()
        self.assertIsNotNone(inv1)
        self.assertEqual(SalesInvoice.objects.filter(sales_order=so).count(), 1)
        self.assertEqual(inv1.lines.count(), 1)

        # Second confirmation (idempotent call)
        inv2 = so.confirm_and_generate_invoice()
        self.assertEqual(inv1.pk, inv2.pk)
        self.assertEqual(SalesInvoice.objects.filter(sales_order=so).count(), 1)
        self.assertEqual(inv2.lines.count(), 1)

        # Third confirmation
        inv3 = so.confirm_and_generate_invoice()
        self.assertEqual(inv1.pk, inv3.pk)
        self.assertEqual(SalesInvoice.objects.filter(sales_order=so).count(), 1)

    def test_delivery_based_invoicing_triggers_on_dispatch_shipment(self):
        """
        A DELIVERY_BASED SalesOrder confirmation defers invoice creation (returns None),
        but fulfilling/saving a DispatchRecord automatically creates the SalesInvoice and lines.
        """
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='DELIVERY_BASED',
            status='draft'
        )
        so_item = SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("20.00")
        )

        # 1. Confirm order -> No invoice created upfront
        inv = so.confirm_and_generate_invoice()
        self.assertIsNone(inv)
        self.assertEqual(so.invoices.count(), 0)
        so.refresh_from_db()
        self.assertEqual(so.status, 'approved')

        # 2. Dispatch shipment for 15 units
        dispatch = DispatchRecord.objects.create(
            sales_order_item=so_item,
            customer=self.customer,
            product=self.glass_putty_5kg,
            quantity_dispatched=Decimal("15.00"),
            status='shipped',
            dispatch_date=timezone.now().date()
        )

        # 3. Verify auto-generated invoice on dispatch
        auto_invoice = SalesInvoice.objects.filter(dispatch=dispatch).first()
        self.assertIsNotNone(auto_invoice)
        self.assertEqual(auto_invoice.sales_order, so)
        self.assertEqual(auto_invoice.customer, self.customer)
        self.assertEqual(auto_invoice.status, 'POSTED')
        self.assertEqual(auto_invoice.total_amount, Decimal("375.00"))  # 15 * 25.00

        # Verify line snapshot
        line = auto_invoice.lines.first()
        self.assertIsNotNone(line)
        self.assertEqual(line.product, self.glass_putty_5kg)
        self.assertEqual(line.quantity, Decimal("15.00"))
        self.assertEqual(line.unit_price, Decimal("25.00"))
        self.assertEqual(line.total_price, Decimal("375.00"))

    def test_return_generates_credit_note_at_retail_price_without_mutating_invoice(self):
        """
        An approved RMA Return generates a CreditNote and lines valued at customer retail selling price,
        leaves the original SalesInvoice total_amount completely intact, and restores inventory.
        """
        so = SalesOrder.objects.create(customer=self.customer, invoicing_policy='ORDER_BASED')
        so_item = SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("10.00")
        )
        invoice = so.confirm_and_generate_invoice()
        initial_invoice_total = invoice.total_amount
        self.assertEqual(initial_invoice_total, Decimal("250.00"))

        dispatch = DispatchRecord.objects.create(
            sales_order_item=so_item,
            customer=self.customer,
            product=self.glass_putty_5kg,
            quantity_dispatched=Decimal("10.00"),
            status='delivered',
            dispatch_date=timezone.now().date()
        )

        initial_stock = Inventory.objects.get(product=self.glass_putty_5kg).quantity_available

        # Customer returns 2 defective/damaged tins
        ret = Return.objects.create(
            dispatch=dispatch,
            customer=self.customer,
            quantity_returned=Decimal("2.00"),
            reason_for_return="Damaged lid seals on delivery",
            quality_control_status='APPROVED'
        )

        # 1. Verify original invoice is NOT mutated
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount, initial_invoice_total)
        self.assertEqual(invoice.total_amount, Decimal("250.00"))

        # 2. Verify Credit Note was generated and linked to invoice
        credit_note = CreditNote.objects.filter(invoice=invoice).first()
        self.assertIsNotNone(credit_note)
        self.assertEqual(credit_note.customer, self.customer)
        self.assertEqual(credit_note.status, 'POSTED')
        self.assertEqual(credit_note.total_amount, Decimal("50.00"))  # 2 * 25.00

        # Check line item on Credit Note
        cn_line = credit_note.lines.first()
        self.assertIsNotNone(cn_line)
        self.assertEqual(cn_line.product, self.glass_putty_5kg)
        self.assertEqual(cn_line.quantity, Decimal("2.00"))
        self.assertEqual(cn_line.unit_price, Decimal("25.00"))
        self.assertEqual(cn_line.total_price, Decimal("50.00"))

        # 3. Verify stock was restored
        updated_stock = Inventory.objects.get(product=self.glass_putty_5kg).quantity_available
        self.assertEqual(updated_stock, initial_stock + Decimal("2.00"))

    def test_finance_entry_auto_posted_on_invoice_issuance(self):
        """
        Creating/posting a SalesInvoice automatically writes a REVENUE / SALES FinanceEntry.
        """
        so = SalesOrder.objects.create(customer=self.customer, invoicing_policy='ORDER_BASED')
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("10.00")
        )
        invoice = so.confirm_and_generate_invoice()

        fe = FinanceEntry.objects.filter(sales_invoice=invoice, category='SALES').first()
        self.assertIsNotNone(fe)
        self.assertEqual(fe.entry_type, 'REVENUE')
        self.assertEqual(fe.category, 'SALES')
        self.assertEqual(fe.amount, Decimal("250.00"))
        self.assertEqual(fe.entry_date, invoice.invoice_date)

    def test_finance_entry_auto_posted_on_payment(self):
        """
        Logging a SalesInvoicePayment creates a corresponding REVENUE / SALES FinanceEntry.
        """
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            subtotal=Decimal("500.00"),
            total_amount=Decimal("500.00"),
            status='POSTED'
        )

        payment = SalesInvoicePayments.objects.create(
            invoice=invoice,
            amount=Decimal("200.00"),
            payment_method="CASH"
        )

        # Verify finance entry for payment exists
        payment_fe = FinanceEntry.objects.filter(
            sales_invoice=invoice,
            amount=Decimal("200.00"),
            entry_type='REVENUE'
        ).first()
        self.assertIsNotNone(payment_fe)
        self.assertEqual(payment_fe.category, 'SALES')

    def test_finance_entry_auto_posted_on_credit_note(self):
        """
        Issuing/posting a CreditNote creates an EXPENSE / CUSTOMER_REFUND FinanceEntry.
        """
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            subtotal=Decimal("500.00"),
            total_amount=Decimal("500.00"),
            status='POSTED'
        )

        cn = CreditNote.objects.create(
            invoice=invoice,
            status='POSTED'
        )
        CreditNoteLine.objects.create(
            credit_note=cn,
            product=self.glass_putty_5kg,
            quantity=Decimal("3.00"),
            unit_price=Decimal("25.00")
        )
        cn.refresh_from_db()
        self.assertEqual(cn.total_amount, Decimal("75.00"))

        fe = FinanceEntry.objects.filter(
            sales_invoice=invoice,
            category='CUSTOMER_REFUND',
            entry_type='EXPENSE'
        ).first()
        self.assertIsNotNone(fe)
        self.assertEqual(fe.amount, Decimal("75.00"))

    def test_sales_order_lines_locked_post_approval(self):
        """
        Non-superusers cannot add, change, or delete line items once a SalesOrder is approved.
        Superusers retain permissions.
        """
        so_admin = SalesOrderAdmin(SalesOrder, self.site)
        item_inline = SalesOrderItemInline(SalesOrder, self.site)

        draft_so = SalesOrder.objects.create(customer=self.customer)
        self.assertEqual(draft_so.status, 'draft')

        approved_so = SalesOrder.objects.create(customer=self.customer)
        SalesOrderItem.objects.create(sales_order=approved_so, product=self.glass_putty_5kg, quantity_ordered=Decimal("10.00"))
        approved_so.refresh_from_db()
        self.assertEqual(approved_so.status, 'approved')

        req_staff = self.factory.get('/admin/')
        req_staff.user = self.staff_user

        req_super = self.factory.get('/admin/')
        req_super.user = self.superuser

        # Draft order: staff CAN edit lines
        self.assertTrue(item_inline.has_add_permission(req_staff, draft_so))
        self.assertTrue(item_inline.has_change_permission(req_staff, draft_so))
        self.assertTrue(item_inline.has_delete_permission(req_staff, draft_so))

        # Approved order: staff CANNOT edit lines
        self.assertFalse(item_inline.has_add_permission(req_staff, approved_so))
        self.assertFalse(item_inline.has_change_permission(req_staff, approved_so))
        self.assertFalse(item_inline.has_delete_permission(req_staff, approved_so))

        # Approved order: superuser CAN edit lines
        self.assertTrue(item_inline.has_add_permission(req_super, approved_so))
        self.assertTrue(item_inline.has_change_permission(req_super, approved_so))
        self.assertTrue(item_inline.has_delete_permission(req_super, approved_so))

        # Header readonly fields on approved order for staff
        readonly_staff = so_admin.get_readonly_fields(req_staff, approved_so)
        self.assertIn('customer', readonly_staff)
        self.assertIn('invoicing_policy', readonly_staff)

        # Delete permission on SalesOrder: restricted to superuser
        self.assertFalse(so_admin.has_delete_permission(req_staff, approved_so))
        self.assertTrue(so_admin.has_delete_permission(req_super, approved_so))

    def test_posted_sales_invoice_is_fully_immutable(self):
        """
        Non-superusers receive all model fields as read-only on POSTED and PAID invoices,
        and cannot modify line items.
        """
        inv_admin = SalesInvoiceAdmin(SalesInvoice, self.site)
        line_inline = SalesInvoiceLineInline(SalesInvoice, self.site)

        draft_inv = SalesInvoice.objects.create(customer=self.customer, status='DRAFT')
        posted_inv = SalesInvoice.objects.create(customer=self.customer, status='POSTED')
        paid_inv = SalesInvoice.objects.create(customer=self.customer, status='PAID')

        req_staff = self.factory.get('/admin/')
        req_staff.user = self.staff_user

        req_super = self.factory.get('/admin/')
        req_super.user = self.superuser

        # Draft invoice: staff can edit lines
        self.assertTrue(line_inline.has_add_permission(req_staff, draft_inv))
        self.assertTrue(line_inline.has_change_permission(req_staff, draft_inv))
        self.assertTrue(line_inline.has_delete_permission(req_staff, draft_inv))

        # POSTED invoice: staff cannot edit lines
        self.assertFalse(line_inline.has_add_permission(req_staff, posted_inv))
        self.assertFalse(line_inline.has_change_permission(req_staff, posted_inv))
        self.assertFalse(line_inline.has_delete_permission(req_staff, posted_inv))

        # PAID invoice: staff cannot edit lines
        self.assertFalse(line_inline.has_add_permission(req_staff, paid_inv))

        # POSTED invoice: all fields read-only for staff
        readonly_posted = inv_admin.get_readonly_fields(req_staff, posted_inv)
        self.assertIn('customer', readonly_posted)
        self.assertIn('invoice_date', readonly_posted)
        self.assertIn('sales_order', readonly_posted)

        # Superuser delete only
        self.assertFalse(inv_admin.has_delete_permission(req_staff, posted_inv))
        self.assertTrue(inv_admin.has_delete_permission(req_super, posted_inv))
