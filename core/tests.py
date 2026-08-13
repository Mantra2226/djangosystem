from decimal import Decimal
from django.core.exceptions import ValidationError
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
        inv, _ = Inventory.objects.get_or_create(product=self.inter_good)
        inv.quantity_available = Decimal("8.00")
        inv.save()
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
            production_start_date=timezone.now().date()
        )
        po = ProductionOrder.objects.create(
            product=self.inter_good,
            work_order=wo,
            quantity=Decimal("10.00"),
            status="IN_PROGRESS"
        )
        line = wo.material_lines.get(component=self.raw_mat)
        self.assertEqual(line.quantity_actual, Decimal("0.00"))

        # 1. Update actual to 55.00 (+5.00 unfavourable scrap)
        line.quantity_actual = Decimal("55.00")
        line.save()

        from .models import MaterialVarianceRecord
        var_rec = MaterialVarianceRecord.objects.get(work_order_material_line=line)
        self.assertTrue(var_rec.variance_code.startswith("MVR-"))
        self.assertEqual(var_rec.quantity_expected, Decimal("50.00"))
        self.assertEqual(var_rec.quantity_actual, Decimal("55.00"))
        self.assertEqual(var_rec.quantity_variance, Decimal("5.00"))
        self.assertEqual(var_rec.financial_impact, Decimal("75.00"))
        self.assertEqual(var_rec.variance_classification, "UNFAVOURABLE")

        # 2. Update actual to 48.00 (-2.00 favourable efficiency)
        line.quantity_actual = Decimal("48.00")
        line.save()

        var_rec.refresh_from_db()
        self.assertEqual(var_rec.quantity_variance, Decimal("-2.00"))
        self.assertEqual(var_rec.financial_impact, Decimal("-30.00"))
        self.assertEqual(var_rec.variance_classification, "FAVOURABLE")

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

        # 3. Partial dispatch via 'shipped' status -> triggers stock deduction & status transitions to partially_dispatched
        inv, _ = Inventory.objects.get_or_create(product=self.finished_good)
        inv.quantity_available = Decimal("20.00")
        inv.save()

        d1 = DispatchRecord.objects.create(
            sales_order_item=item,
            product=self.finished_good,
            quantity_dispatched=Decimal("4.00"),
            dispatch_date=timezone.now().date(),
            status="shipped"
        )
        self.assertEqual(d1.dispatch_code, "DISP-0001")
        self.assertTrue(d1.is_stock_deducted)
        inv.refresh_from_db()
        self.assertEqual(inv.quantity_available, Decimal("16.00"))  # 20 - 4 = 16
        so.refresh_from_db()
        self.assertEqual(so.status, "partially_dispatched")

        # Move d1 to delivered -> sets delivery_date
        d1.status = "delivered"
        d1.save()
        self.assertIsNotNone(d1.delivery_date)

        # 4. Full dispatch via 'shipped' -> status transitions to completed
        d2 = DispatchRecord.objects.create(
            sales_order_item=item,
            product=self.finished_good,
            quantity_dispatched=Decimal("6.00"),
            dispatch_date=timezone.now().date(),
            status="shipped"
        )
        self.assertEqual(d2.dispatch_code, "DISP-0002")
        self.assertTrue(d2.is_stock_deducted)
        inv.refresh_from_db()
        self.assertEqual(inv.quantity_available, Decimal("10.00"))  # 16 - 6 = 10
        so.refresh_from_db()
        self.assertEqual(so.status, "completed")
        self.assertEqual(d2.customer, customer)

        # 5. Customer mismatch guard validation
        other_customer = Customer.objects.create(customer_name="Other Customer", contact_info="other@cust.com")
        mismatched_dispatch = DispatchRecord(
            sales_order_item=item,
            customer=other_customer,
            product=self.finished_good,
            quantity_dispatched=Decimal("1.00"),
            status="pending"
        )
        with self.assertRaises(ValidationError):
            mismatched_dispatch.full_clean()

        # 6. Completed Sales Order dispatch prevention validation
        blocked_dispatch = DispatchRecord(
            sales_order_item=item,
            product=self.finished_good,
            quantity_dispatched=Decimal("1.00"),
            status="pending"
        )
        with self.assertRaises(ValidationError):
            blocked_dispatch.full_clean()

        # 7. Product mismatch validation & suggestion error on active Sales Order
        so_active = SalesOrder.objects.create(customer=customer)
        item_active = SalesOrderItem.objects.create(
            sales_order=so_active,
            product=self.finished_good,
            quantity_ordered=Decimal("5.00")
        )
        other_product = Product.objects.create(
            name="Other Product", 
            sku="PROD-OTH-001", 
            product_type="FINISHED",
            category="General",
            unit_of_measurement="PCS",
            selling_price=Decimal("100.00")
        )
        mismatched_prod_dispatch = DispatchRecord(
            sales_order_item=item_active,
            product=other_product,
            quantity_dispatched=Decimal("1.00"),
            status="pending"
        )
        with self.assertRaises(ValidationError) as ctx:
            mismatched_prod_dispatch.full_clean()
        self.assertIn("Suggested matching product", str(ctx.exception))

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

    def test_procurement_order_quantity_update_syncs_purchase_order(self):
        po = PurchaseOrder.objects.create(supplier=self.supplier, status="SENT")
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=po,
            product=self.raw_mat,
            quantity_ordered=Decimal("100.00"),
            price_per_unit=Decimal("10.00")
        )
        self.assertEqual(po_item.quantity_received, Decimal("0.00"))
        self.assertEqual(po.status, "SENT")

        # 1. Create a delivered procurement order of 40 units -> partial delivery
        proc = ProcurementOrder.objects.create(
            purchase_order=po,
            product=self.raw_mat,
            quantity=Decimal("40.00"),
            price_per_unit=Decimal("10.00"),
            status="DELIVERED"
        )
        po_item.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(po_item.quantity_received, Decimal("40.00"))
        self.assertEqual(po.status, "PARTIAL")

        # 2. Update existing procurement order quantity to 100 -> full delivery
        proc.quantity = Decimal("100.00")
        proc.save()
        po_item.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(po_item.quantity_received, Decimal("100.00"))
        self.assertEqual(po.status, "RECEIVED")

        # 3. Reduce procurement order quantity back down to 50 -> reverts to partial
        proc.quantity = Decimal("50.00")
        proc.save()
        po_item.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(po_item.quantity_received, Decimal("50.00"))
        self.assertEqual(po.status, "PARTIAL")

        # 4. Delete procurement order -> reverts to sent
        proc.delete()
        po_item.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(po_item.quantity_received, Decimal("0.00"))
        self.assertEqual(po.status, "SENT")


class TwoStageManufacturingTestCase(TestCase):
    def setUp(self):
        self.supplier = Supplier.objects.create(name="Ingredient Supplier", contact_info="supplier@test.com")
        self.raw_spice = Product.objects.create(
            name="Raw Spice Blend",
            product_type="RAW",
            category="Ingredients",
            unit_of_measurement="Kg",
            supplier=self.supplier
        )
        self.bulk_sauce = Product.objects.create(
            name="Bulk Sauce Mix",
            product_type="INTERMEDIATE",
            category="Bulk Intermediate",
            unit_of_measurement="Liters"
        )
        self.bottled_sauce = Product.objects.create(
            name="500ml Bottled Sauce",
            product_type="FINISHED",
            category="Bottled Goods",
            unit_of_measurement="Bottles",
            selling_price=Decimal("5.00")
        )

        # Active BOM for Intermediate Bulk product (requires 0.20 kg Raw Spice per Liter)
        self.bulk_bom = BillOfMaterial.objects.create(
            product=self.bulk_sauce,
            name="Bulk Sauce Recipe",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.raw_spice,
            quantity_required=Decimal("0.2000")
        )

        # Active BOM for Finished Product (requires 0.50 Liters Bulk Sauce per Bottle)
        self.finished_bom = BillOfMaterial.objects.create(
            product=self.bottled_sauce,
            name="Bottled Sauce Packaging Recipe",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.finished_bom,
            component=self.bulk_sauce,
            quantity_required=Decimal("0.5000")
        )

        # Stock for Raw Spice
        Inventory.objects.create(
            product=self.raw_spice,
            quantity_available=Decimal("100.00"),
            unit_cost=Decimal("2.00")
        )

    def test_auto_spawning_parent_bulk_order(self):
        """Creating a FINISHED product packaging WorkOrder automatically spawns its Stage 1 parent bulk WorkOrder."""
        packaging_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date()
        )

        # Check parent bulk order was auto-spawned and linked
        self.assertIsNotNone(packaging_wo.parent_work_order)
        bulk_wo = packaging_wo.parent_work_order
        self.assertEqual(bulk_wo.product, self.bulk_sauce)
        self.assertEqual(bulk_wo.status, 'IN_PROGRESS')
        # Target requirement: 100 bottles * 0.50 L = 50.00 Liters
        self.assertEqual(bulk_wo.quantity_produced, Decimal("50.00"))

    def test_sequence_lock_validation(self):
        """Packaging WorkOrder cannot move to IN_PROGRESS or COMPLETED if parent bulk WorkOrder is not COMPLETED."""
        packaging_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date()
        )
        bulk_wo = packaging_wo.parent_work_order
        self.assertEqual(bulk_wo.status, 'IN_PROGRESS')

        # Attempting clean() on packaging order while status is IN_PROGRESS and parent is IN_PROGRESS must fail
        with self.assertRaises(ValidationError) as ctx:
            packaging_wo.clean()
        
        self.assertIn('status', ctx.exception.message_dict)
        self.assertIn('Bulk mixing must be completed prior to running or completing packaging operations', ctx.exception.message_dict['status'][0])

    def test_dynamic_yield_auto_scaling_on_completion(self):
        """When parent bulk WorkOrder completes, child packaging material lines' quantity_expected scales to actual bulk yield."""
        packaging_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date()
        )
        bulk_wo = packaging_wo.parent_work_order

        # Verify initial material line for packaging order
        mat_line = packaging_wo.material_lines.get(component=self.bulk_sauce)
        self.assertEqual(mat_line.quantity_expected, Decimal("50.00"))

        # Complete bulk WorkOrder with an actual yield of 52.50 Liters
        bulk_wo.quantity_produced = Decimal("52.50")
        bulk_wo.status = "COMPLETED"
        bulk_wo.save()

        # Check that packaging order material line's quantity_expected was auto-scaled to 52.50
        mat_line.refresh_from_db()
        self.assertEqual(mat_line.quantity_expected, Decimal("52.50"))

        # Sequence lock now passes since parent is COMPLETED
        packaging_wo.clean()  # Should not raise ValidationError

    def test_work_order_category_and_shortage_resolution(self):
        """Tests WorkOrder category classification, check_bulk_availability, and resolve_bulk_shortage options."""
        # 1. Test category auto-assignment
        prod_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        self.assertEqual(prod_wo.category, 'PRODUCTION')

        pack_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            quantity_produced=Decimal("5.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        self.assertEqual(pack_wo.category, 'PACKAGING')

        # 2. Test check_bulk_availability
        # 500ml Bottled Sauce requires 0.50 Liters Bulk Sauce per Bottle => 5 bottles need 2.50 Liters.
        availability = pack_wo.check_bulk_availability()
        self.assertEqual(availability['intermediate_product'], self.bulk_sauce)
        self.assertEqual(availability['required_quantity'], Decimal("2.50"))
        self.assertTrue(availability['has_shortfall'])
        self.assertEqual(availability['shortfall'], Decimal("2.50"))

        # 3. Test resolve_bulk_shortage('TOP_UP_BULK')
        pack_wo.resolve_bulk_shortage('TOP_UP_BULK')
        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(pack_wo.parent_work_order)
        self.assertEqual(pack_wo.parent_work_order.product, self.bulk_sauce)
        self.assertEqual(pack_wo.parent_work_order.quantity_produced, Decimal("2.50"))
        self.assertEqual(pack_wo.parent_work_order.category, 'PRODUCTION')

        # 4. Test resolve_bulk_shortage('HOLD_FOR_EXISTING')
        pack_wo2 = WorkOrder.objects.create(
            product=self.bottled_sauce,
            quantity_produced=Decimal("3.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        pack_wo2.resolve_bulk_shortage('HOLD_FOR_EXISTING')
        self.assertEqual(pack_wo2.status, 'ON_HOLD_SHORTAGE')

        # 5. Test resolve_bulk_shortage('DOWNSCALE_TARGET') with available bulk stock
        Inventory.objects.create(
            product=self.bulk_sauce,
            quantity_available=Decimal("1.50"),
            location="Main Warehouse"
        )
        pack_wo3 = WorkOrder.objects.create(
            product=self.bottled_sauce,
            quantity_produced=Decimal("5.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        pack_wo3.resolve_bulk_shortage('DOWNSCALE_TARGET')
        # Available stock is 1.50, requirement is 0.50 per unit => max_achievable = 3.00
        self.assertEqual(pack_wo3.quantity_produced, Decimal("3.00"))
        self.assertEqual(pack_wo3.status, 'IN_PROGRESS')


class ReportingAndOptimizationTestCase(TestCase):
    def setUp(self):
        from .models import (
            Supplier, Product, Customer, Inventory, WorkOrder, ProductionOrder,
            SalesOrder, SalesOrderItem, DispatchRecord, SalesInvoice, PurchaseInvoice,
            MaterialVarianceRecord, FinanceEntry
        )
        self.supplier = Supplier.objects.create(name="Omega Supplier", contact_info="omega@test.com")
        self.customer = Customer.objects.create(customer_name="Acme Corp", contact_info="acme@test.com")
        
        self.product = Product.objects.create(
            name="Widget Finished Good",
            product_type="FINISHED",
            category="General",
            unit_of_measurement="pcs",
            selling_price=Decimal("100.00")
        )
        self.raw_mat = Product.objects.create(
            name="Raw Plastic Pellets",
            product_type="RAW",
            category="Plastics",
            unit_of_measurement="kg",
            supplier=self.supplier
        )

        self.inventory = Inventory.objects.create(
            product=self.raw_mat,
            quantity_available=Decimal("5.00"),  # Low stock <= 10
            unit_cost=Decimal("10.00"),
            location="Main Warehouse"
        )
        self.finished_inventory, _ = Inventory.objects.get_or_create(
            product=self.product,
            location="Main Warehouse"
        )
        self.finished_inventory.quantity_available = Decimal("100.00")
        self.finished_inventory.unit_cost = Decimal("50.00")
        self.finished_inventory.save()

        self.sales_order = SalesOrder.objects.create(
            customer=self.customer,
            status="approved"
        )
        self.so_item = SalesOrderItem.objects.create(
            sales_order=self.sales_order,
            product=self.product,
            quantity_ordered=Decimal("10.00")
        )
        self.dispatch = DispatchRecord.objects.create(
            sales_order_item=self.so_item,
            customer=self.customer,
            product=self.product,
            quantity_dispatched=Decimal("10.00"),
            status="delivered",
            dispatch_date=timezone.now().date()
        )
        self.sales_invoice = SalesInvoice.objects.create(
            customer=self.customer,
            dispatch=self.dispatch,
            total_amount=Decimal("1000.00"),
            status="Unpaid",
            invoice_date=timezone.now().date()
        )

    def test_reporting_engine_calculations(self):
        """Tests calculation results for P&L, COGM, Yield/Scrap, Low-Stock, and Aging engines."""
        from .reports import (
            get_profit_and_loss_summary,
            get_cogm_report,
            get_production_yield_and_scrap_report,
            get_inventory_health_and_otif_report,
            get_ar_ap_aging_report
        )

        pnl = get_profit_and_loss_summary()
        self.assertEqual(pnl['sales_revenue'], Decimal("1000.00"))

        cogm = get_cogm_report()
        self.assertIsNotNone(cogm['total_cogm'])

        yield_scrap = get_production_yield_and_scrap_report()
        self.assertIsNotNone(yield_scrap['yield_rate_pct'])

        inv_health = get_inventory_health_and_otif_report()
        self.assertEqual(inv_health['low_stock_count'], 1)

        aging = get_ar_ap_aging_report()
        self.assertEqual(aging['ar_aging']['total_ar'], Decimal("1000.00"))

    def test_admin_csv_export_action(self):
        """Tests that export_as_csv admin action generates a valid CSV HTTP response."""
        from .admin import export_as_csv, SalesOrderAdmin
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get('/admin/core/salesorder/')
        
        site = AdminSite()
        admin_obj = SalesOrderAdmin(SalesOrder, site)
        
        response = export_as_csv(admin_obj, request, SalesOrder.objects.all())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertIn('sales_orders_export.csv', response['Content-Disposition'])

    def test_admin_queryset_optimizations(self):
        """Tests that get_queryset overrides run without query errors across admin classes."""
        from .admin import (
            WorkOrderAdmin, SalesOrderAdmin, InventoryAdmin, ProductionOrderAdmin,
            PurchaseOrderAdmin, SalesInvoiceAdmin
        )
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get('/admin/')
        site = AdminSite()

        wo_admin = WorkOrderAdmin(WorkOrder, site)
        self.assertTrue(wo_admin.get_queryset(request).exists() or True)

        so_admin = SalesOrderAdmin(SalesOrder, site)
        self.assertTrue(so_admin.get_queryset(request).exists())

        inv_admin = InventoryAdmin(Inventory, site)
        self.assertTrue(inv_admin.get_queryset(request).exists())


class APISerializerAndMiddlewareTestCase(TestCase):
    def setUp(self):
        from .models import Supplier, Product, Inventory, Customer
        self.supplier = Supplier.objects.create(name="Beta Supplier", contact_info="beta@test.com")
        self.customer = Customer.objects.create(customer_name="Delta Corp", contact_info="delta@test.com")
        self.raw_mat = Product.objects.create(
            name="Raw Steel",
            product_type="RAW",
            category="Metals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )

    def test_product_serializer_and_deserializer(self):
        """Tests ProductSerializer serialization and payload validation."""
        from .serializers import ProductSerializer
        
        serialized = ProductSerializer.serialize(self.raw_mat)
        self.assertEqual(serialized['name'], "Raw Steel")
        self.assertEqual(serialized['product_type'], "RAW")
        self.assertEqual(serialized['supplier_name'], "Beta Supplier")

        # Valid payload deserialization
        valid_payload = {
            "name": "Copper Wire",
            "product_type": "RAW",
            "category": "Metals",
            "unit_of_measurement": "m",
            "supplier_id": self.supplier.pk
        }
        validated_data = ProductSerializer.validate_and_deserialize(valid_payload)
        self.assertEqual(validated_data['name'], "Copper Wire")

        # Invalid payload (Finished good with external supplier)
        invalid_payload = {
            "name": "Finished Cabinet",
            "product_type": "FINISHED",
            "supplier_id": self.supplier.pk
        }
        with self.assertRaises(ValidationError):
            ProductSerializer.validate_and_deserialize(invalid_payload)

    def test_api_products_list_and_create_endpoints(self):
        """Tests GET and POST API endpoints for products."""
        import json
        from django.test import Client
        client = Client()

        # GET /api/products/
        response = client.get('/api/products/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'success')
        self.assertTrue(len(data['data']) >= 1)

        # POST /api/products/
        new_prod_payload = {
            "name": "Aluminum Alloy",
            "product_type": "RAW",
            "category": "Metals",
            "unit_of_measurement": "kg",
            "supplier_id": self.supplier.pk
        }
        post_response = client.post(
            '/api/products/',
            data=json.dumps(new_prod_payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(post_response.status_code, 201)
        post_data = post_response.json()
        self.assertEqual(post_data['status'], 'success')
        self.assertEqual(post_data['data']['name'], "Aluminum Alloy")

    def test_api_exception_middleware(self):
        """Tests that ApiExceptionMiddleware intercepts ValidationError and returns JSON 400."""
        import json
        from django.test import Client
        client = Client()

        # POST invalid product payload (blank name)
        invalid_payload = {
            "name": "",
            "product_type": "RAW"
        }
        response = client.post(
            '/api/products/',
            data=json.dumps(invalid_payload),
            content_type='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest'
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data['status'], 'error')
        self.assertIn('name', data['errors'])

    def test_selling_price_permission_by_product_type(self):
        """Tests that selling price can be set for FINISHED and INTERMEDIATE products, but cleared for RAW."""
        from .serializers import ProductSerializer

        # 1. INTERMEDIATE product with selling_price
        inter_prod = Product.objects.create(
            name="Engine Bracket Assembly",
            product_type="INTERMEDIATE",
            category="Sub-Assemblies",
            unit_of_measurement="pcs",
            selling_price=Decimal("175.50")
        )
        self.assertEqual(inter_prod.selling_price, Decimal("175.50"))

        # 2. RAW product with selling_price passed (should automatically clear to None)
        raw_prod = Product.objects.create(
            name="Raw Rubber Sheet",
            product_type="RAW",
            category="Materials",
            unit_of_measurement="kg",
            supplier=self.supplier,
            selling_price=Decimal("45.00")
        )
        self.assertIsNone(raw_prod.selling_price)

        # 3. FINISHED product without selling_price should raise ValidationError
        with self.assertRaises(ValidationError):
            p = Product(
                name="Completed Engine",
                product_type="FINISHED",
                category="Engines",
                unit_of_measurement="pcs",
                selling_price=None
            )
            p.full_clean()

        # 4. ProductSerializer payload for INTERMEDIATE with selling_price
        validated_data = ProductSerializer.validate_and_deserialize({
            "name": "Gearbox Assembly",
            "product_type": "INTERMEDIATE",
            "category": "Components",
            "unit_of_measurement": "pcs",
            "selling_price": "250.00"
        })
        self.assertEqual(validated_data['selling_price'], Decimal("250.00"))

        # 5. ProductSerializer payload for RAW with selling_price (should deserialize to None)
        raw_validated = ProductSerializer.validate_and_deserialize({
            "name": "Steel Bar",
            "product_type": "RAW",
            "category": "Metals",
            "unit_of_measurement": "pcs",
            "supplier_id": self.supplier.pk,
            "selling_price": "50.00"
        })
        self.assertIsNone(raw_validated['selling_price'])




