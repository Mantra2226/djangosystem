from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from .models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory, 
    WorkOrder, ProductionOrder, PurchaseOrder, PurchaseOrderItem, 
    ProcurementOrder, Customer, SalesOrder, SalesOrderItem, DispatchRecord,
    SalesInvoice, SalesInvoicePayments, PurchaseInvoice, PurchasePayment
)
from .services import (
    evaluate_mrp_shortages,
    resolve_raw_autodraft_po,
    resolve_raw_direct_procurement,
    resolve_raw_hold_inbound,
    resolve_intermediate_build,
    resolve_intermediate_hold_active,
    resolve_intermediate_partial_batch,
    check_and_auto_resume_on_hold_orders
)

class MRPEngineTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(name="Alpha Supplier", contact_info="alpha@test.com")
        self.raw_mat = Product.objects.create(
            name="Raw Steel Sheets",
            product_type="RAW",
            category="Metals",
            unit_of_measurement="Sheets",
            supplier=self.supplier
        )
        self.inter_good = Product.objects.create(
            name="Steel Frame Sub-Assembly",
            product_type="INTERMEDIATE",
            category="Frames",
            unit_of_measurement="Units"
        )
        self.finished_good = Product.objects.create(
            name="Heavy Duty Industrial Cabinet",
            product_type="FINISHED",
            category="Cabinets",
            unit_of_measurement="Units",
            selling_price=Decimal("500.00")
        )

        # BOM for Finished Good requiring 2 Steel Frames
        self.finished_bom = BillOfMaterial.objects.create(
            product=self.finished_good,
            name="Cabinet Recipe",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.finished_bom,
            component=self.inter_good,
            quantity_required=Decimal("2.00")
        )

        # BOM for Intermediate Good requiring 5 Raw Steel Sheets
        self.inter_bom = BillOfMaterial.objects.create(
            product=self.inter_good,
            name="Steel Frame Recipe",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.inter_bom,
            component=self.raw_mat,
            quantity_required=Decimal("5.00")
        )

    def test_production_order_shortage_triggers_on_hold_shortage(self):
        wo = WorkOrder.objects.create(
            product=self.finished_good,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date()
        )
        po = ProductionOrder(
            product=self.finished_good,
            work_order=wo,
            quantity=Decimal("10.00"),
            status="IN_PROGRESS"
        )
        po.full_clean()
        po.save()

        # Stock is 0, so status should transition to ON_HOLD_SHORTAGE
        self.assertEqual(po.status, "ON_HOLD_SHORTAGE")
        self.assertIn("MRP SHORTAGE FLAGGED", po.notes)

    def test_raw_material_resolution_options(self):
        wo = WorkOrder.objects.create(
            product=self.inter_good,
            bill_of_material=self.inter_bom,
            quantity_produced=Decimal("5.00"),
            production_start_date=timezone.now().date()
        )
        po = ProductionOrder.objects.create(
            product=self.inter_good,
            work_order=wo,
            quantity=Decimal("5.00"),
            status="ON_HOLD_SHORTAGE"
        )

        # Option 1: Auto-Draft PO
        draft_po = resolve_raw_autodraft_po(po, self.raw_mat.pk, Decimal("25.00"))
        self.assertEqual(draft_po.status, "SENT")
        self.assertEqual(draft_po.items.count(), 1)
        self.assertEqual(draft_po.items.first().quantity_ordered, Decimal("25.00"))

        # Option 2: Direct Procurement
        proc = resolve_raw_direct_procurement(po, self.raw_mat.pk, Decimal("25.00"))
        self.assertEqual(proc.status, "PENDING")
        self.assertEqual(proc.quantity, Decimal("25.00"))

        # Option 3: Hold for Inbound Stock
        resolve_raw_hold_inbound(po, self.raw_mat.pk)
        self.assertEqual(po.status, "ON_HOLD_SHORTAGE")

    def test_intermediate_resolution_options(self):
        wo = WorkOrder.objects.create(
            product=self.finished_good,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date()
        )
        po = ProductionOrder.objects.create(
            product=self.finished_good,
            work_order=wo,
            quantity=Decimal("10.00"),
            status="ON_HOLD_SHORTAGE"
        )

        # Option 1: Build Sub-Assembly
        child_wo, child_po = resolve_intermediate_build(po, self.inter_good.pk, Decimal("20.00"))
        self.assertEqual(child_po.product, self.inter_good)
        self.assertEqual(child_po.quantity, Decimal("20.00"))

        # Option 2: Hold for Active Run
        resolve_intermediate_hold_active(po, self.inter_good.pk)
        self.assertEqual(po.status, "ON_HOLD_SHORTAGE")

        # Option 3: Partial Batch Run with available stock
        Inventory.objects.create(product=self.inter_good, quantity_available=Decimal("8.00"))
        scaled_po = resolve_intermediate_partial_batch(po, Decimal("4.00"))
        self.assertEqual(scaled_po.quantity, Decimal("4.00"))
        self.assertEqual(scaled_po.status, "IN_PROGRESS")

    def test_auto_resume_signal_on_stock_addition(self):
        wo = WorkOrder.objects.create(
            product=self.finished_good,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("2.00"),
            production_start_date=timezone.now().date()
        )
        po = ProductionOrder.objects.create(
            product=self.finished_good,
            work_order=wo,
            quantity=Decimal("2.00"),
            status="ON_HOLD_SHORTAGE"
        )

        # Need 4 steel frames. Add 5 steel frames to inventory
        inv, _ = Inventory.objects.get_or_create(product=self.inter_good, defaults={"quantity_available": Decimal("0.00")})
        inv.quantity_available = Decimal("5.00")
        inv.save() # Triggers post-save signal!

        po.refresh_from_db()
        self.assertEqual(po.status, "IN_PROGRESS")
        self.assertIn("MRP AUTO-RESUMED", po.notes)

    def test_work_order_material_line_enhanced_variance(self):
        Inventory.objects.create(product=self.raw_mat, unit_cost=Decimal("15.00"), quantity_available=Decimal("100.00"))
        wo = WorkOrder.objects.create(
            product=self.inter_good,
            bill_of_material=self.inter_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date()
        )
        line = wo.material_lines.get(component=self.raw_mat)
        line.quantity_expected = Decimal("50.00")
        line.quantity_actual = Decimal("55.00")
        line.save()

        self.assertEqual(line.variance, Decimal("5.00"))
        self.assertEqual(line.variance_percentage, Decimal("10.00"))
        self.assertEqual(line.cost_variance, Decimal("75.00"))
        self.assertEqual(line.variance_status, "OVER_CONSUMPTION")
        self.assertIn("+5.00 (+10.00%)", line.variance_summary)

        # Verify automated LossRecord creation & accuracy
        from .models import LossRecord
        loss_rec = LossRecord.objects.get(work_order_material_line=line)
        self.assertEqual(loss_rec.quantity_lost, Decimal("5.00"))
        self.assertEqual(loss_rec.financial_loss, Decimal("75.00"))
        self.assertEqual(loss_rec.variance_percentage, Decimal("10.00"))
        self.assertEqual(loss_rec.efficiency_rate, Decimal("90.91"))
        self.assertEqual(loss_rec.loss_type, "OVER_CONSUMPTION")

    def test_production_order_code_auto_generation(self):
        wo = WorkOrder.objects.create(
            product=self.finished_good,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("5.00"),
            production_start_date=timezone.now().date()
        )
        po1 = ProductionOrder.objects.create(
            product=self.finished_good,
            work_order=wo,
            quantity=Decimal("5.00")
        )
        po2 = ProductionOrder.objects.create(
            product=self.finished_good,
            work_order=wo,
            quantity=Decimal("5.00")
        )

        self.assertEqual(po1.production_order_code, "POC-0001")
        self.assertEqual(po2.production_order_code, "POC-0002")
        self.assertIn("POC-0001", str(po1))

    def test_purchase_order_and_procurement_status_flow(self):
        # 1. PO starts in DRAFT with 0 items
        po = PurchaseOrder.objects.create(supplier=self.supplier)
        self.assertEqual(po.status, "DRAFT")

        # 2. Add line item -> status transitions to SENT
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=po,
            product=self.raw_mat,
            quantity_ordered=Decimal("100.00"),
            price_per_unit=Decimal("10.00")
        )
        po.refresh_from_db()
        self.assertEqual(po.status, "SENT")

        # 3. Partial procurement delivery -> status transitions to PARTIAL
        proc1 = ProcurementOrder.objects.create(
            purchase_order=po,
            product=self.raw_mat,
            quantity=Decimal("40.00"),
            price_per_unit=Decimal("10.00"),
            status="DELIVERED"
        )
        self.assertIsNotNone(proc1.delivery_date)
        po.refresh_from_db()
        self.assertEqual(po.status, "PARTIAL")

        # 4. Full procurement delivery -> status transitions to RECEIVED
        proc2 = ProcurementOrder.objects.create(
            purchase_order=po,
            product=self.raw_mat,
            quantity=Decimal("60.00"),
            price_per_unit=Decimal("10.00"),
            status="DELIVERED"
        )
        po.refresh_from_db()
        self.assertEqual(po.status, "RECEIVED")

    def test_sales_order_code_generation_and_status_flow(self):
        customer = Customer.objects.create(customer_name="Beta Customer", contact_info="beta@test.com")
        
        # 1. SalesOrder starts in draft status with 0 items
        so = SalesOrder.objects.create(customer=customer)
        self.assertEqual(so.status, "draft")
        self.assertTrue(so.order_number.startswith("SO-"))
        self.assertNotIn("SO-SO-", str(so))

        # 2. Add line item -> status transitions to approved
        item = SalesOrderItem.objects.create(
            sales_order=so,
            product=self.finished_good,
            quantity_ordered=Decimal("10.00")
        )
        so.refresh_from_db()
        self.assertEqual(so.status, "approved")
        self.assertEqual(item.unit_price, Decimal("500.00"))
        self.assertEqual(item.total_price, Decimal("5000.00"))

        # 3. Partial dispatch -> status transitions to partially_dispatched
        inv, _ = Inventory.objects.get_or_create(product=self.finished_good)
        inv.quantity_available = Decimal("20.00")
        inv.save()

        d1 = DispatchRecord.objects.create(
            sales_order_item=item,
            product=self.finished_good,
            quantity_dispatched=Decimal("4.00"),
            dispatch_date=timezone.now().date(),
            status="delivered"
        )
        self.assertEqual(d1.dispatch_code, "DISP-0001")
        self.assertTrue(d1.is_stock_deducted)
        self.assertIsNotNone(d1.delivery_date)
        so.refresh_from_db()
        self.assertEqual(so.status, "partially_dispatched")

        # 4. Full dispatch -> status transitions to completed
        d2 = DispatchRecord.objects.create(
            sales_order_item=item,
            product=self.finished_good,
            quantity_dispatched=Decimal("6.00"),
            dispatch_date=timezone.now().date(),
            status="delivered"
        )
        self.assertEqual(d2.dispatch_code, "DISP-0002")
        so.refresh_from_db()
        self.assertEqual(so.status, "completed")

    def test_sales_invoice_remaining_balance_and_status_flow(self):
        customer = Customer.objects.create(customer_name="Test Customer", contact_info="test@cust.com")
        inv = SalesInvoice.objects.create(
            customer=customer,
            total_amount=Decimal("1000.00")
        )
        # 1. Non-editable auto-generated invoice number
        self.assertTrue(inv.invoice_number.startswith("SINV-"))
        self.assertEqual(inv.status, "Unpaid")
        self.assertEqual(inv.total_paid, Decimal("0.00"))
        self.assertEqual(inv.remaining_balance, Decimal("1000.00"))

        # 2. Partial payment
        p1 = SalesInvoicePayments.objects.create(
            invoice=inv,
            amount=Decimal("400.00"),
            payment_method="TRANSFER",
            reference_number="REF-001"
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, "Partial")
        self.assertEqual(inv.total_paid, Decimal("400.00"))
        self.assertEqual(inv.remaining_balance, Decimal("600.00"))

        # 3. Full payment
        p2 = SalesInvoicePayments.objects.create(
            invoice=inv,
            amount=Decimal("600.00"),
            payment_method="CASH"
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, "Paid")
        self.assertEqual(inv.total_paid, Decimal("1000.00"))
        self.assertEqual(inv.remaining_balance, Decimal("0.00"))

    def test_purchase_invoice_paid_date_automation(self):
        pi = PurchaseInvoice.objects.create(
            invoice_number="PINV-TEST-001",
            supplier=self.supplier,
            total_amount=Decimal("500.00")
        )
        self.assertEqual(pi.status, "UNPAID")
        self.assertIsNone(pi.paid_date)

        # 1. Partial payment -> paid_date remains None
        pm1 = PurchasePayment.objects.create(
            purchase_invoice=pi,
            amount=Decimal("200.00"),
            payment_method="CASH"
        )
        pi.refresh_from_db()
        self.assertEqual(pi.status, "PARTIAL")
        self.assertIsNone(pi.paid_date)

        # 2. Final payment -> paid_date is auto-stamped with payment date
        pm2 = PurchasePayment.objects.create(
            purchase_invoice=pi,
            amount=Decimal("300.00"),
            payment_method="TRANSFER",
            reference_number="TXN-999"
        )
        pi.refresh_from_db()
        self.assertEqual(pi.status, "PAID")
        self.assertIsNotNone(pi.paid_date)
        self.assertEqual(pi.paid_date, pm2.paid_at.date())

        # 3. Payment deletion -> status reverts to PARTIAL and paid_date is cleared
        pm2.delete()
        pi.refresh_from_db()
        self.assertEqual(pi.status, "PARTIAL")
        self.assertIsNone(pi.paid_date)
