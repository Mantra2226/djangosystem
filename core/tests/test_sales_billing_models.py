from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError
from core.models import (
    Customer, Product, SalesOrder, SalesOrderItem, SalesInvoice,
    SalesInvoiceLine, SalesInvoicePayments, CreditNote, CreditNoteLine,
    DocumentSequence, Inventory
)


class SalesBillingModelTests(TestCase):
    """
    Test suite for Milestone 1: Data Model & Financial Immutability in the Sales & Invoicing Subsystem.
    Verifies price freezing, itemized lines, credit notes, atomic numbering, and dual invoicing policies.
    """

    def setUp(self):
        self.customer = Customer.objects.create(
            customer_name="Acme Glazing Solutions",
            contact_info="orders@acmeglazing.com",
            shipping_address="45 Glaziers Row, Industrial Park"
        )
        self.glass_putty_5kg = Product.objects.create(
            name="Glass Putty 5kg Tin",
            sku="FG-PUTTY-5KG",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="tin",
            selling_price=Decimal("25.00")
        )
        self.bulk_putty_base = Product.objects.create(
            name="Bulk Putty Base",
            sku="INT-PUTTY-BASE",
            product_type="INTERMEDIATE",
            category="Bulk",
            unit_of_measurement="kg",
            selling_price=Decimal("4.50")
        )

    def test_sales_order_item_freezes_catalog_price_on_creation(self):
        """
        Verifies that SalesOrderItem freezes the catalog price at creation time
        and remains immune to future catalog price changes (financial immutability).
        """
        so = SalesOrder.objects.create(customer=self.customer)
        item = SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("10.00")
        )

        # Assert unit price was frozen to 25.00
        self.assertEqual(item.unit_price, Decimal("25.00"))
        self.assertEqual(item.total_price, Decimal("250.00"))

        # Update product catalog selling price to 35.00
        self.glass_putty_5kg.selling_price = Decimal("35.00")
        self.glass_putty_5kg.save()

        # Refresh order item from database and assert price has NOT changed
        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal("25.00"))
        self.assertEqual(item.total_price, Decimal("250.00"))

    def test_sales_order_item_custom_unit_price_override(self):
        """
        Verifies that providing an explicit negotiated/discounted unit price
        is preserved and not overwritten by the catalog selling price.
        """
        so = SalesOrder.objects.create(customer=self.customer)
        item = SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("20.00"),
            unit_price=Decimal("21.50")  # Discounted rate
        )

        self.assertEqual(item.unit_price, Decimal("21.50"))
        self.assertEqual(item.total_price, Decimal("430.00"))
        self.assertEqual(item.quantity, Decimal("20.00"))

    def test_sales_invoice_line_financial_calculations(self):
        """
        Verifies SalesInvoiceLine subtotal, tax amount, and total price calculations with VAT.
        """
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            invoice_date=timezone.now().date(),
            status='DRAFT'
        )
        line = SalesInvoiceLine.objects.create(
            invoice=invoice,
            product=self.glass_putty_5kg,
            quantity=Decimal("10.00"),
            unit_price=Decimal("25.00"),
            tax_rate=Decimal("16.00")  # 16% VAT
        )

        self.assertEqual(line.subtotal, Decimal("250.00"))
        self.assertEqual(line.tax_amount, Decimal("40.00"))  # 250 * 0.16 = 40.00
        self.assertEqual(line.total_price, Decimal("290.00"))

        # Verify parent invoice header totals auto-recalculated
        invoice.refresh_from_db()
        self.assertEqual(invoice.subtotal, Decimal("250.00"))
        self.assertEqual(invoice.tax_amount, Decimal("40.00"))
        self.assertEqual(invoice.total_amount, Decimal("290.00"))

    def test_sales_invoice_links_to_sales_order(self):
        """
        Verifies bidirectional foreign key relationship between SalesOrder and SalesInvoice.
        """
        so = SalesOrder.objects.create(customer=self.customer)
        invoice = SalesInvoice.objects.create(
            sales_order=so,
            customer=self.customer,
            total_amount=Decimal("500.00")
        )

        self.assertEqual(invoice.sales_order, so)
        self.assertIn(invoice, so.invoices.all())
        self.assertEqual(so.invoices.count(), 1)

    def test_sales_order_invoicing_policy_order_based_generates_invoice_and_lines(self):
        """
        Verifies that confirm_and_generate_invoice() on an ORDER_BASED SalesOrder
        transitions status to approved, creates a SalesInvoice, and snapshots all order items into SalesInvoiceLines.
        """
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='ORDER_BASED',
            status='draft'
        )
        item1 = SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("10.00")  # 10 * 25 = 250
        )
        item2 = SalesOrderItem.objects.create(
            sales_order=so,
            product=self.bulk_putty_base,
            quantity_ordered=Decimal("100.00")  # 100 * 4.50 = 450
        )

        invoice = so.confirm_and_generate_invoice()

        so.refresh_from_db()
        self.assertEqual(so.status, 'approved')
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.sales_order, so)
        self.assertEqual(invoice.customer, self.customer)
        self.assertEqual(invoice.total_amount, Decimal("700.00"))
        self.assertEqual(invoice.status, 'POSTED')

        # Check line item snapshots
        lines = list(invoice.lines.all())
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].sales_order_item, item1)
        self.assertEqual(lines[0].product, self.glass_putty_5kg)
        self.assertEqual(lines[0].quantity, Decimal("10.00"))
        self.assertEqual(lines[0].unit_price, Decimal("25.00"))
        self.assertEqual(lines[0].total_price, Decimal("250.00"))

        self.assertEqual(lines[1].sales_order_item, item2)
        self.assertEqual(lines[1].product, self.bulk_putty_base)
        self.assertEqual(lines[1].quantity, Decimal("100.00"))
        self.assertEqual(lines[1].unit_price, Decimal("4.50"))
        self.assertEqual(lines[1].total_price, Decimal("450.00"))

    def test_sales_order_invoicing_policy_delivery_based_defers_invoice(self):
        """
        Verifies that confirm_and_generate_invoice() on a DELIVERY_BASED SalesOrder
        approves the order without generating an upfront invoice.
        """
        so = SalesOrder.objects.create(
            customer=self.customer,
            invoicing_policy='DELIVERY_BASED',
            status='draft'
        )
        SalesOrderItem.objects.create(
            sales_order=so,
            product=self.glass_putty_5kg,
            quantity_ordered=Decimal("5.00")
        )

        invoice = so.confirm_and_generate_invoice()

        so.refresh_from_db()
        self.assertEqual(so.status, 'approved')
        self.assertIsNone(invoice)
        self.assertEqual(so.invoices.count(), 0)

    def test_credit_note_and_lines_creation_and_recalculation(self):
        """
        Verifies CreditNote creation, line items calculation, and reverse relationship to SalesInvoice.
        """
        invoice = SalesInvoice.objects.create(
            customer=self.customer,
            subtotal=Decimal("500.00"),
            total_amount=Decimal("500.00"),
            status='POSTED'
        )

        credit_note = CreditNote.objects.create(
            invoice=invoice,
            reason="Damaged tins during transit"
        )
        self.assertTrue(credit_note.credit_note_number.startswith("CN-"))
        self.assertEqual(credit_note.customer, self.customer)
        self.assertEqual(credit_note.status, 'DRAFT')

        # Add credit note lines
        line1 = CreditNoteLine.objects.create(
            credit_note=credit_note,
            product=self.glass_putty_5kg,
            quantity=Decimal("2.00"),
            unit_price=Decimal("25.00"),
            tax_rate=Decimal("16.00")
        )
        self.assertEqual(line1.subtotal, Decimal("50.00"))
        self.assertEqual(line1.tax_amount, Decimal("8.00"))
        self.assertEqual(line1.total_price, Decimal("58.00"))

        line2 = CreditNoteLine.objects.create(
            credit_note=credit_note,
            product=self.glass_putty_5kg,
            quantity=Decimal("1.00"),
            unit_price=Decimal("25.00"),
            tax_rate=Decimal("16.00")
        )
        self.assertEqual(line2.total_price, Decimal("29.00"))

        # Verify CreditNote header recalculation
        credit_note.refresh_from_db()
        self.assertEqual(credit_note.subtotal, Decimal("75.00"))
        self.assertEqual(credit_note.tax_amount, Decimal("12.00"))
        self.assertEqual(credit_note.total_amount, Decimal("87.00"))

        # Verify reverse relation from invoice
        self.assertIn(credit_note, invoice.credit_notes.all())

    def test_atomic_document_sequencing(self):
        """
        Verifies atomic sequential numbering produces correctly formatted, monotonically increasing sequence numbers.
        """
        ym = timezone.now().strftime('%Y%m')
        num1 = DocumentSequence.get_next_number('SALES_INVOICE', 'SINV')
        num2 = DocumentSequence.get_next_number('SALES_INVOICE', 'SINV')
        num3 = DocumentSequence.get_next_number('SALES_INVOICE', 'SINV')

        self.assertEqual(num1, f"SINV-{ym}-0001")
        self.assertEqual(num2, f"SINV-{ym}-0002")
        self.assertEqual(num3, f"SINV-{ym}-0003")

        # Verify independent sequence counter for Credit Notes
        cn1 = DocumentSequence.get_next_number('CREDIT_NOTE', 'CN')
        cn2 = DocumentSequence.get_next_number('CREDIT_NOTE', 'CN')
        self.assertEqual(cn1, f"CN-{ym}-0001")
        self.assertEqual(cn2, f"CN-{ym}-0002")

    def test_sales_invoice_status_lifecycle_and_payments(self):
        """
        Verifies SalesInvoice payment status transitions (DRAFT -> PARTIALLY_PAID -> PAID)
        and balance computations.
        """
        inv = SalesInvoice.objects.create(
            customer=self.customer,
            subtotal=Decimal("1000.00"),
            total_amount=Decimal("1000.00")
        )
        self.assertEqual(inv.status, 'DRAFT')
        self.assertEqual(inv.total_paid, Decimal("0.00"))
        self.assertEqual(inv.remaining_balance, Decimal("1000.00"))

        # Partial Payment
        p1 = SalesInvoicePayments.objects.create(
            invoice=inv,
            amount=Decimal("400.00"),
            payment_method="TRANSFER",
            reference_number="TXN-400-A"
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'PARTIALLY_PAID')
        self.assertEqual(inv.total_paid, Decimal("400.00"))
        self.assertEqual(inv.remaining_balance, Decimal("600.00"))

        # Second Payment settling remaining balance
        p2 = SalesInvoicePayments.objects.create(
            invoice=inv,
            amount=Decimal("600.00"),
            payment_method="CASH"
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'PAID')
        self.assertEqual(inv.total_paid, Decimal("1000.00"))
        self.assertEqual(inv.remaining_balance, Decimal("0.00"))
