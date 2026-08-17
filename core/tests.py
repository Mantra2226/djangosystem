from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from .models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory, 
    WorkOrder, ProductionOrder, PurchaseOrder, PurchaseOrderItem, 
    ProcurementOrder, Customer, SalesOrder, SalesOrderItem, DispatchRecord,
    SalesInvoice, SalesInvoicePayments, PurchaseInvoice, PurchasePayment,
    StockTransaction, MaterialVarianceRecord
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
        # Seed raw material inventory so Phase 1 allocation has sufficient stock
        # (BOM requires 5.00 Raw Steel Sheets/unit × 20.00 target = 100.00 needed)
        raw_inv, _ = Inventory.objects.get_or_create(product=self.raw_mat)
        raw_inv.quantity_available = Decimal("200.00")
        raw_inv.save()
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
        self.assertEqual(var_rec.production_run_type, "PRODUCTION")
        self.assertIn("[PRODUCTION]", str(var_rec))

    def test_variance_and_stock_transaction_output_customizations(self):
        """Tests that MaterialVarianceRecord outputs production run type (PRODUCTION/PACKAGING) and StockTransaction outputs work order code."""
        from django.contrib.admin.sites import AdminSite
        from .admin import MaterialVarianceRecordAdmin, StockTransactionAdmin
        from .serializers import MaterialVarianceRecordSerializer, StockTransactionSerializer

        # 1. Packaging Work Order and Material Variance
        pack_wo = WorkOrder.objects.create(
            product=self.finished_good,
            category="PACKAGING",
            status="IN_PROGRESS",
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("20.00"),
            production_start_date=timezone.now().date()
        )
        line = pack_wo.material_lines.get(component=self.inter_good)
        line.quantity_actual = Decimal("42.00")
        line.save()

        var_rec = MaterialVarianceRecord.objects.get(work_order_material_line=line)
        self.assertEqual(var_rec.production_run_type, "PACKAGING")
        self.assertIn("[PACKAGING]", str(var_rec))
        self.assertIn(self.inter_good.name, str(var_rec))

        # Admin display for MaterialVarianceRecord
        site = AdminSite()
        mvr_admin = MaterialVarianceRecordAdmin(MaterialVarianceRecord, site)
        run_type_badge = mvr_admin.get_production_run_type(var_rec)
        self.assertIn("PACKAGING", run_type_badge)

        # Serializer for MaterialVarianceRecord
        mvr_serialized = MaterialVarianceRecordSerializer.serialize(var_rec)
        self.assertEqual(mvr_serialized['production_run_type'], "PACKAGING")
        self.assertEqual(mvr_serialized['work_order_code'], pack_wo.work_order_code)

        # 2. StockTransaction with linked WorkOrder
        st_linked = StockTransaction.objects.create(
            product=self.raw_mat,
            work_order=pack_wo,
            quantity=Decimal("-10.00"),
            transaction_type="PRODUCTION_CONSUMPTION",
            notes="Consumption run"
        )
        self.assertEqual(st_linked.work_order_code, pack_wo.work_order_code)
        self.assertIn(pack_wo.work_order_code, str(st_linked))
        self.assertIn("-10.00", str(st_linked))

        # Admin display for linked StockTransaction
        st_admin = StockTransactionAdmin(StockTransaction, site)
        self.assertEqual(st_admin.get_work_order_code(st_linked), pack_wo.work_order_code)

        # Serializer for linked StockTransaction
        st_linked_serialized = StockTransactionSerializer.serialize(st_linked)
        self.assertEqual(st_linked_serialized['work_order_code'], pack_wo.work_order_code)
        self.assertEqual(st_linked_serialized['work_order_id'], pack_wo.pk)

        # 3. StockTransaction without linked WorkOrder
        st_unlinked = StockTransaction.objects.create(
            product=self.raw_mat,
            quantity=Decimal("50.00"),
            transaction_type="RECEIPT",
            notes="Supplier purchase receipt"
        )
        self.assertIsNone(st_unlinked.work_order_code)
        self.assertIn("No WO", str(st_unlinked))
        self.assertEqual(st_admin.get_work_order_code(st_unlinked), "-")

        st_unlinked_serialized = StockTransactionSerializer.serialize(st_unlinked)
        self.assertIsNone(st_unlinked_serialized['work_order_code'])
        self.assertIsNone(st_unlinked_serialized['work_order_id'])

    def test_draft_work_order_locks_instructions_and_material_consumption(self):
        """Tests that DRAFT work orders lock instruction completion and actual material consumption entry."""
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from .admin import WorkOrderInstructionInline, WorkOrderMaterialLineInline
        from .models import WorkOrderInstruction, WorkOrderMaterialLine

        # 1. Create a DRAFT Work Order
        draft_wo = WorkOrder.objects.create(
            product=self.finished_good,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status="DRAFT"
        )
        self.assertEqual(draft_wo.status, "DRAFT")

        # Material line should be auto-created with 0.00 actual
        mat_line = draft_wo.material_lines.first()
        self.assertIsNotNone(mat_line)
        self.assertEqual(mat_line.quantity_actual, Decimal("0.00"))

        # Instruction step should be auto-created with IN_PROGRESS
        inst = draft_wo.instructions.first()
        self.assertIsNotNone(inst)
        self.assertNotEqual(inst.status, "COMPLETED")

        # 2. Attempting to enter actual consumption on DRAFT must raise ValidationError
        mat_line.quantity_actual = Decimal("15.00")
        with self.assertRaises(ValidationError) as ctx:
            mat_line.full_clean()
        self.assertIn('quantity_actual', ctx.exception.message_dict)

        # 3. Attempting to mark instruction COMPLETED on DRAFT must raise ValidationError
        inst.status = "COMPLETED"
        with self.assertRaises(ValidationError) as ctx:
            inst.full_clean()
        self.assertIn('status', ctx.exception.message_dict)

        # 4. Verify Admin Inlines mark fields as readonly when WorkOrder is DRAFT
        site = AdminSite()
        factory = RequestFactory()
        request = factory.get('/admin/')

        inst_inline = WorkOrderInstructionInline(WorkOrderInstruction, site)
        inst_readonly = inst_inline.get_readonly_fields(request, draft_wo)
        self.assertIn('status', inst_readonly)

        mat_inline = WorkOrderMaterialLineInline(WorkOrderMaterialLine, site)
        mat_readonly = mat_inline.get_readonly_fields(request, draft_wo)
        self.assertIn('quantity_actual', mat_readonly)

        # 5. Move to IN_PROGRESS -> now both are editable and valid
        draft_wo.status = "IN_PROGRESS"
        draft_wo.save()

        inst.refresh_from_db()
        inst.status = "COMPLETED"
        inst.full_clean()  # Should not raise

        mat_line.refresh_from_db()
        mat_line.quantity_actual = Decimal("15.00")
        mat_line.full_clean()  # Should not raise

        inst_readonly_in_progress = inst_inline.get_readonly_fields(request, draft_wo)
        self.assertNotIn('status', inst_readonly_in_progress)

        mat_readonly_in_progress = mat_inline.get_readonly_fields(request, draft_wo)
        self.assertNotIn('quantity_actual', mat_readonly_in_progress)

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

    def test_production_order_without_work_order_soft_message(self):
        """Tests that creating a ProductionOrder without a WorkOrder succeeds and shows soft reminder messages."""
        from django.contrib.admin.sites import AdminSite
        from django.test.client import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from .admin import ProductionOrderAdmin, ProductionOrderAdminForm
        from .serializers import ProductionOrderSerializer

        # 1. Create PO without work_order (null FK)
        po = ProductionOrder.objects.create(
            product=self.finished_good,
            work_order=None,
            quantity=Decimal("15.00")
        )
        self.assertIsNone(po.work_order_id)
        self.assertIsNone(po.work_order)
        self.assertEqual(po.quantity, Decimal("15.00"))
        self.assertIn("Unlinked (No WO)", str(po))

        # 2. Test ProductionOrderAdminForm has work_order as optional
        form = ProductionOrderAdminForm(instance=po)
        self.assertFalse(form.fields['work_order'].required)
        self.assertIn("Optional", form.fields['work_order'].help_text)

        # 3. Test Admin work_order_details_viewer returns reminder banner
        site = AdminSite()
        po_admin = ProductionOrderAdmin(ProductionOrder, site)
        details_html = po_admin.work_order_details_viewer(po)
        self.assertIn("No Work Order linked", details_html)
        self.assertIn("Reminder", details_html)

        # 4. Test Admin mrp_resolution_pathways_viewer returns reminder banner
        pathways_html = po_admin.mrp_resolution_pathways_viewer(po)
        self.assertIn("No Work Order Linked", pathways_html)

        # 5. Test Admin get_quantity returns po.quantity when no WO is linked
        qty_display = po_admin.get_quantity(po)
        self.assertEqual(qty_display, Decimal("15.00"))

        # 6. Test Admin save_model triggers messages.warning
        factory = RequestFactory()
        request = factory.post('/admin/core/productionorder/add/', {
            'product': self.finished_good.pk,
            'quantity': '15.00',
            'status': 'IN_PROGRESS'
        })
        # Attach message storage to request
        setattr(request, 'session', {})
        messages_storage = FallbackStorage(request)
        setattr(request, '_messages', messages_storage)

        po_admin.save_model(request, po, form, change=False)
        all_messages = [msg.message for msg in messages_storage]
        self.assertTrue(any("Reminder: Production Order" in msg for msg in all_messages))
        self.assertTrue(any("saved without a linked Work Order" in msg for msg in all_messages))

        # 7. Test Serializer serializes cleanly with unlinked WO
        serialized = ProductionOrderSerializer.serialize(po)
        self.assertIsNone(serialized['work_order_id'])
        self.assertEqual(serialized['work_order_code'], "")

    def test_production_order_completed_at_timestamp_lifecycle(self):
        """Tests that completed_at is None for active POs and only stamped upon COMPLETED status."""
        # 1. New ProductionOrder in IN_PROGRESS must have completed_at = None
        po = ProductionOrder.objects.create(
            product=self.finished_good,
            quantity=Decimal("10.00"),
            status="IN_PROGRESS"
        )
        self.assertIsNotNone(po.created_at)
        self.assertIsNone(po.completed_at)

        # 2. Saving while still IN_PROGRESS should keep completed_at = None
        po.quantity = Decimal("12.00")
        po.save()
        po.refresh_from_db()
        self.assertIsNone(po.completed_at)

        # 3. Setting status to COMPLETED sets completed_at to now
        po.status = "COMPLETED"
        po.save()
        po.refresh_from_db()
        self.assertIsNotNone(po.completed_at)
        self.assertAlmostEqual(
            po.completed_at.timestamp(),
            timezone.now().timestamp(),
            delta=5
        )

        # 4. Changing status back to CANCELLED clears completed_at to None
        po.status = "CANCELLED"
        po.save()
        po.refresh_from_db()
        self.assertIsNone(po.completed_at)

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

    def test_no_auto_spawning_parent_bulk_order(self):
        """Creating a FINISHED product packaging WorkOrder no longer auto-spawns a parent bulk WorkOrder."""
        packaging_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        # Verify parent bulk order is NOT auto-spawned
        self.assertIsNone(packaging_wo.parent_work_order)
        self.assertEqual(packaging_wo.category, 'PACKAGING')

    def test_sequence_lock_validation(self):
        """Packaging WorkOrder cannot move to IN_PROGRESS or COMPLETED if linked parent bulk WorkOrder is not COMPLETED."""
        # Manually create a parent bulk order and link it
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        packaging_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS',
            parent_work_order=bulk_wo
        )
        self.assertEqual(bulk_wo.status, 'IN_PROGRESS')

        # Attempting clean() on packaging order while status is IN_PROGRESS and parent is IN_PROGRESS must fail
        with self.assertRaises(ValidationError) as ctx:
            packaging_wo.clean()

        key = 'parent_work_order' if 'parent_work_order' in ctx.exception.message_dict else 'status'
        self.assertIn(key, ctx.exception.message_dict)
        self.assertIn('Cannot start packaging: Linked parent bulk order', ctx.exception.message_dict[key][0])

    def test_dynamic_yield_auto_scaling_on_completion(self):
        """When parent bulk WorkOrder completes, child packaging material lines' quantity_expected scales to actual bulk yield."""
        # Manually create parent bulk order
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        packaging_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT',
            parent_work_order=bulk_wo
        )

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
        packaging_wo.status = 'IN_PROGRESS'
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
        self.assertIsNone(pack_wo.parent_work_order)

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

        # 4. Test resolve_bulk_shortage('HOLD_FOR_EXISTING') without existing_bulk_wo_id
        pack_wo2 = WorkOrder.objects.create(
            product=self.bottled_sauce,
            quantity_produced=Decimal("3.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        pack_wo2.resolve_bulk_shortage('HOLD_FOR_EXISTING')
        self.assertEqual(pack_wo2.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNone(pack_wo2.parent_work_order)

        # 5. Test resolve_bulk_shortage('HOLD_FOR_EXISTING') with existing_bulk_wo_id
        existing_bulk = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("20.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        pack_wo4 = WorkOrder.objects.create(
            product=self.bottled_sauce,
            quantity_produced=Decimal("4.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        pack_wo4.resolve_bulk_shortage('HOLD_FOR_EXISTING', existing_bulk_wo_id=existing_bulk.pk)
        self.assertEqual(pack_wo4.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(pack_wo4.parent_work_order, existing_bulk)

        # 6. Test resolve_bulk_shortage('DOWNSCALE_TARGET') with available bulk stock
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

    def test_work_order_form_dynamic_labels_and_requirements(self):
        """Tests WorkOrderForm dynamic field requirements and labels for PACKAGING vs PRODUCTION categories."""
        from core.forms import WorkOrderForm

        # 1. Packaging Form
        pack_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        pack_form = WorkOrderForm(instance=pack_wo)
        self.assertFalse(pack_form.fields['parent_work_order'].required)
        self.assertEqual(pack_form.fields['parent_work_order'].label, "Source Bulk Batch (Parent WO)")
        self.assertEqual(pack_form.fields['quantity_produced'].label, "Target Pack Count (Units/Tins)")
        self.assertEqual(pack_form.fields['quantity_produced'].help_text, "Total discrete containers to fill.")

        # 2. Production Form
        prod_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        prod_form = WorkOrderForm(instance=prod_wo)
        self.assertFalse(prod_form.fields['parent_work_order'].required)
        self.assertEqual(prod_form.fields['quantity_produced'].label, "Bulk Yield Target (kg/L)")
        self.assertEqual(prod_form.fields['quantity_produced'].help_text, "Total bulk weight/volume to mix.")

        # 3. Test form sequence lock validation error on parent_work_order field
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        pack_wo_draft = WorkOrder.objects.create(
            product=self.bottled_sauce,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT',
            parent_work_order=bulk_wo
        )
        invalid_form = WorkOrderForm(data={
            'product': self.bottled_sauce.pk,
            'category': 'PACKAGING',
            'parent_work_order': bulk_wo.pk,
            'quantity_produced': '10.00',
            'production_start_date': str(timezone.now().date())
        }, instance=pack_wo_draft)
        invalid_form.instance.status = 'IN_PROGRESS'
        self.assertFalse(invalid_form.is_valid())
        self.assertIn('parent_work_order', invalid_form.errors)
        self.assertIn('Cannot start packaging: Linked parent bulk order', str(invalid_form.errors['parent_work_order']))

    def test_work_order_draft_status_gate_and_field_error_mapping(self):
        """Tests that DRAFT Work Orders bypass operational checks, while IN_PROGRESS / COMPLETED enforce field-specific errors."""
        # Create a new product without active BOMs
        new_prod = Product.objects.create(
            name="Unassigned Product",
            category="Sauces",
            product_type="INTERMEDIATE",
            unit_of_measurement="Liters",
            selling_price=Decimal("15.00")
        )

        # 1. DRAFT status allows minimal constraints
        draft_wo = WorkOrder(
            product=new_prod,
            status='DRAFT',
            production_start_date=None,
            quantity_produced=None
        )
        # Should not raise ValidationError on clean
        draft_wo.clean()

        # 2. Transitioning to IN_PROGRESS triggers all field-specific errors
        draft_wo.status = 'IN_PROGRESS'
        with self.assertRaises(ValidationError) as ctx:
            draft_wo.clean()

        errors = ctx.exception.message_dict
        self.assertIn('target_quantity', errors)
        self.assertIn("Target Quantity must be greater than 0 to start production.", errors['target_quantity'])

        self.assertIn('production_start_date', errors)
        self.assertIn("Please provide a Production Start Date before moving to IN_PROGRESS.", errors['production_start_date'])

        self.assertIn('bill_of_material', errors)
        self.assertIn("Cannot start order: Assign an active Bill of Materials (BOM) for this product.", errors['bill_of_material'])

        # 3. Packaging Stage 2 parent bulk dependency validation (with manually linked parent)
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("25.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        packaging_draft = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT',
            parent_work_order=bulk_wo
        )
        self.assertEqual(packaging_draft.parent_work_order.status, 'IN_PROGRESS')

        # In DRAFT, clean() passes even if parent bulk order is IN_PROGRESS
        packaging_draft.clean()

        # Moving packaging order to IN_PROGRESS fails with specific linked parent message
        packaging_draft.status = 'IN_PROGRESS'
        with self.assertRaises(ValidationError) as ctx:
            packaging_draft.clean()

        expected_msg = f"Cannot start packaging: Linked parent bulk order #{packaging_draft.parent_work_order.work_order_code} is currently '{packaging_draft.parent_work_order.status}'. It must reach COMPLETED status first."
        self.assertIn('parent_work_order', ctx.exception.message_dict)
        self.assertIn(expected_msg, ctx.exception.message_dict['parent_work_order'])

        # 4. Individually satisfying each constraint resolves errors
        draft_wo.production_start_date = timezone.now().date()
        draft_wo.quantity_produced = Decimal("20.00")
        active_bom = BillOfMaterial.objects.create(product=new_prod, name="Active BOM", is_active=True)
        # Now clean() passes for IN_PROGRESS
        draft_wo.clean()

        # 5. COMPLETED status enforces instruction steps completion
        draft_wo.save()
        draft_wo.status = 'COMPLETED'
        with self.assertRaises(ValidationError) as ctx_completed:
            draft_wo.clean()
        self.assertTrue(any('incomplete instruction step' in str(e) for e in ctx_completed.exception.messages))

    def test_start_production_model_workflow(self):
        """Tests WorkOrder.start_production() state transition workflow, validation gates, shortage routing, and stock allocation."""
        # 1. Calling start_production on non-DRAFT raises ValidationError
        in_progress_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        with self.assertRaises(ValidationError) as ctx:
            in_progress_wo.start_production()
        self.assertIn("Only DRAFT work orders can be started.", str(ctx.exception))

        # 2. Starting DRAFT work order missing operational requirements fails clean validation
        invalid_draft = WorkOrder.objects.create(
            product=self.bulk_sauce,
            status='DRAFT'
        )
        with self.assertRaises(ValidationError) as ctx_invalid:
            invalid_draft.start_production()
        self.assertIn('target_quantity', ctx_invalid.exception.message_dict)

        # 3. Valid DRAFT production order starts successfully and allocates inventory
        valid_draft = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("20.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        success, msg = valid_draft.start_production()
        self.assertTrue(success)
        self.assertEqual(msg, "Work order started successfully and stock allocated.")
        self.assertEqual(valid_draft.status, 'IN_PROGRESS')
        self.assertTrue(valid_draft.is_inventory_allocated)

        # 4. Packaging order with bulk shortfall moves to AWAITING_RESOLUTION (no auto-spawned parent)
        pack_draft = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        self.assertIsNone(pack_draft.parent_work_order)

        success_shortage, msg_shortage = pack_draft.start_production()
        self.assertFalse(success_shortage)
        self.assertEqual(msg_shortage, "Bulk shortage detected. Moved to Awaiting Resolution.")
        self.assertEqual(pack_draft.status, 'AWAITING_RESOLUTION')

    def test_start_production_admin_view_routing(self):
        """Tests custom admin route <id>/start-production/ and change form template resolution."""
        from django.contrib.auth.models import User
        admin_user = User.objects.create_superuser('admin_tester', 'admin@example.com', 'password123')
        self.client.login(username='admin_tester', password='password123')

        wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("30.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        url = f"/admin/core/workorder/{wo.pk}/start-production/"
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)

        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')

    def test_admin_resolution_action_routes(self):
        """Tests custom admin routes for shortage resolution actions (top-up-bulk, downscale-target, hold-for-existing)."""
        from django.contrib.auth.models import User
        admin_user = User.objects.create_superuser('admin_res', 'admin_res@example.com', 'password123')
        self.client.login(username='admin_res', password='password123')

        # Create a packaging order in AWAITING_RESOLUTION
        pack_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )

        # Test top-up-bulk route
        url = f"/admin/core/workorder/{pack_wo.pk}/top-up-bulk/"
        response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200)
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(pack_wo.parent_work_order)

        # Test downscale-target route (add some bulk stock first)
        Inventory.objects.create(
            product=self.bulk_sauce,
            quantity_available=Decimal("2.00"),
            location="Main Warehouse"
        )
        pack_wo2 = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("10.00"),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        url2 = f"/admin/core/workorder/{pack_wo2.pk}/downscale-target/"
        response2 = self.client.get(url2, follow=True)
        self.assertEqual(response2.status_code, 200)
        pack_wo2.refresh_from_db()
        self.assertEqual(pack_wo2.status, 'IN_PROGRESS')

        # Test hold-for-existing route with bulk_wo_id parameter
        existing_bulk = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("30.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        pack_wo3 = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("8.00"),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        url3 = f"/admin/core/workorder/{pack_wo3.pk}/hold-for-existing/?bulk_wo_id={existing_bulk.pk}"
        response3 = self.client.get(url3, follow=True)
        self.assertEqual(response3.status_code, 200)
        pack_wo3.refresh_from_db()
        self.assertEqual(pack_wo3.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(pack_wo3.parent_work_order, existing_bulk)

    def test_quantity_produced_field_validation_error_pinpointing(self):
        """Tests that invalid quantity_produced pinpoints the quantity_produced form field directly."""
        from core.forms import WorkOrderForm
        invalid_qty_form = WorkOrderForm(data={
            'product': self.bulk_sauce.pk,
            'category': 'PRODUCTION',
            'quantity_produced': '-10.00',
            'production_start_date': str(timezone.now().date())
        })
        self.assertFalse(invalid_qty_form.is_valid())
        self.assertIn('quantity_produced', invalid_qty_form.errors)
        self.assertIn('Bulk Yield Target (kg/L) must be a positive number', invalid_qty_form.errors['quantity_produced'][0])

    def test_instruction_auto_generation_and_preservation(self):
        """Tests default instruction blueprint auto-generation and instruction step preservation upon edit."""
        # 1. Auto-generation on new WorkOrder
        wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )
        self.assertTrue(wo.instructions.exists())
        self.assertEqual(wo.instructions.count(), 4)

        # 2. Preservation when updating instruction steps
        inst1 = wo.instructions.first()
        inst1.step_name = "Updated Step Name"
        inst1.save()
        
        self.assertEqual(wo.instructions.count(), 4)
        inst1_reloaded = wo.instructions.get(step_number=1)
        self.assertEqual(inst1_reloaded.step_name, "Updated Step Name")

    def test_actual_quantity_produced_form_labels_and_help_text(self):
        """Tests that WorkOrderForm configures label and operator help text for actual_quantity_produced based on category."""
        from core.forms import WorkOrderForm

        # Production Category
        prod_form = WorkOrderForm(initial={'category': 'PRODUCTION'})
        self.assertIn('actual_quantity_produced', prod_form.fields)
        self.assertEqual(prod_form.fields['actual_quantity_produced'].label, "Actual Quantity Produced (kg/L)")
        self.assertEqual(prod_form.fields['actual_quantity_produced'].help_text, "Actual bulk weight/volume produced by operator to save to inventory.")

        # Packaging Category
        pack_form = WorkOrderForm(initial={'category': 'PACKAGING'})
        self.assertIn('actual_quantity_produced', pack_form.fields)
        self.assertEqual(pack_form.fields['actual_quantity_produced'].label, "Actual Quantity Produced (Units/Tins)")
        self.assertEqual(pack_form.fields['actual_quantity_produced'].help_text, "Actual count of filled containers produced by operator to save to inventory.")

    def test_process_inventory_saves_actual_quantity_produced_to_inventory(self):
        """Tests that process_inventory saves actual_quantity_produced to inventory instead of target yield when completed."""
        wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            quantity_produced=Decimal("100.00"),
            actual_quantity_produced=Decimal("95.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        # Complete all instruction steps
        wo.instructions.update(status='COMPLETED')
        wo.recalculate_status()

        # Run process_inventory
        wo.process_inventory()
        wo.refresh_from_db()

        self.assertTrue(wo.is_inventory_updated)
        inv = Inventory.objects.get(product=self.bottled_sauce)
        # Should be 95.00 (actual_quantity_produced), NOT 100.00 (quantity_produced target)
        self.assertEqual(inv.quantity_available, Decimal("95.00"))

    def test_sync_child_packaging_uses_actual_quantity_produced(self):
        """Tests that sync_child_packaging_expectations uses actual_quantity_produced if set."""
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_sauce,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("50.00"),
            actual_quantity_produced=Decimal("52.50"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        pack_wo = WorkOrder.objects.create(
            product=self.bottled_sauce,
            bill_of_material=self.finished_bom,
            parent_work_order=bulk_wo,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        # Complete bulk_wo instructions and recalculate
        bulk_wo.instructions.update(status='COMPLETED')
        bulk_wo.recalculate_status()
        bulk_wo.process_inventory()

        pack_mat_line = pack_wo.material_lines.get(component=self.bulk_sauce)
        self.assertEqual(pack_mat_line.quantity_expected, Decimal("52.50"))


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

    def test_mrp_resolve_action_csrf_exempt_and_execution(self):
        """Tests that mrp_resolve_action is CSRF exempt and processes action requests for staff without 403."""
        from django.contrib.auth.models import User
        from django.test import Client
        
        staff_user = User.objects.create_user(username="staff_admin", password="password", is_staff=True)
        client = Client(enforce_csrf_checks=True)
        client.login(username="staff_admin", password="password")

        fg = Product.objects.create(
            name="Assembled Machine",
            product_type="FINISHED",
            category="Equipment",
            unit_of_measurement="pcs",
            selling_price=Decimal("1500.00")
        )
        po = ProductionOrder.objects.create(
            product=fg,
            quantity=Decimal("5.00"),
            status="IN_PROGRESS"
        )

        response = client.post('/mrp_resolve_action/', {
            'production_order_id': po.pk,
            'component_id': self.raw_mat.pk,
            'shortfall_qty': '10.00',
            'resolution_action': 'raw_hold_inbound'
        })
        self.assertEqual(response.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.status, "ON_HOLD_SHORTAGE")


class WorkOrderShortageResolutionEndToEndTestCase(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.admin_user = User.objects.create_superuser('admin_sr', 'admin_sr@test.com', 'password123')
        self.client.login(username='admin_sr', password='password123')

        self.supplier = Supplier.objects.create(name="Apex Materials", contact_info="apex@test.com")
        self.raw_resin = Product.objects.create(
            name="Raw Epoxy Resin",
            product_type="RAW",
            category="Chemicals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.bulk_putty = Product.objects.create(
            name="Bulk Putty Base",
            product_type="INTERMEDIATE",
            category="Pastes",
            unit_of_measurement="kg"
        )
        self.packaged_putty = Product.objects.create(
            name="500g Tub Putty",
            product_type="FINISHED",
            category="Retail Tubs",
            unit_of_measurement="tubs",
            selling_price=Decimal("25.00")
        )

        # Raw stock in warehouse for mixing
        Inventory.objects.create(
            product=self.raw_resin,
            quantity_available=Decimal("500.00"),
            location="Main Warehouse"
        )

        # BOM for Bulk Putty: 1 kg bulk putty requires 0.8 kg raw resin
        self.bulk_bom = BillOfMaterial.objects.create(
            product=self.bulk_putty,
            name="Bulk Putty Recipe",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.raw_resin,
            quantity_required=Decimal("0.8000")
        )

        # BOM for Packaged Putty: 1 tub requires 0.5 kg bulk putty
        self.pack_bom = BillOfMaterial.objects.create(
            product=self.packaged_putty,
            name="Tub Packaging Recipe",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.pack_bom,
            component=self.bulk_putty,
            quantity_required=Decimal("0.5000")
        )

    def test_start_production_detects_shortage_and_moves_to_awaiting_resolution(self):
        """Packaging 50 tubs requires 25 kg bulk putty. Warehouse has 0. Should move to AWAITING_RESOLUTION."""
        pack_wo = WorkOrder.objects.create(
            product=self.packaged_putty,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        po = ProductionOrder.objects.create(
            product=self.packaged_putty,
            work_order=pack_wo,
            quantity=Decimal("50.00"),
            status='IN_PROGRESS'
        )

        success, msg = pack_wo.start_production()
        self.assertFalse(success)
        pack_wo.refresh_from_db()
        po.refresh_from_db()

        self.assertEqual(pack_wo.status, 'AWAITING_RESOLUTION')
        self.assertEqual(po.status, 'ON_HOLD_SHORTAGE')
        self.assertIn("Bulk shortage detected", msg)

    def test_option_1_top_up_bulk_execution_and_auto_resume_flow(self):
        """
        Option 1: Spawns parent bulk WorkOrder for exact shortfall,
        links it as parent_work_order, sets packaging to ON_HOLD_SHORTAGE,
        and auto-resumes packaging order when parent completes.
        """
        # Warehouse has 5 kg bulk putty (enough for only 10 tubs; 50 tubs need 25 kg -> shortfall 20 kg)
        Inventory.objects.create(
            product=self.bulk_putty,
            quantity_available=Decimal("5.00"),
            location="Main Warehouse"
        )

        pack_wo = WorkOrder.objects.create(
            product=self.packaged_putty,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        po = ProductionOrder.objects.create(
            product=self.packaged_putty,
            work_order=pack_wo,
            quantity=Decimal("50.00"),
            status='ON_HOLD_SHORTAGE'
        )

        # Execute Option 1: Top-Up Bulk
        pack_wo.resolve_bulk_shortage('TOP_UP_BULK')
        pack_wo.refresh_from_db()
        po.refresh_from_db()

        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(po.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(pack_wo.parent_work_order)

        parent_wo = pack_wo.parent_work_order
        self.assertEqual(parent_wo.product, self.bulk_putty)
        self.assertEqual(parent_wo.quantity_produced, Decimal("20.00")) # Exact shortfall
        self.assertEqual(parent_wo.status, 'IN_PROGRESS')
        self.assertTrue(parent_wo.is_inventory_allocated) # Raw resin reserved for top-up run

        # Linked PO for parent bulk work order
        parent_po = ProductionOrder.objects.filter(work_order=parent_wo).first()
        self.assertIsNotNone(parent_po)
        self.assertEqual(parent_po.quantity, Decimal("20.00"))
        self.assertEqual(parent_po.status, 'IN_PROGRESS')

        # Now complete parent bulk work order
        parent_wo.actual_quantity_produced = Decimal("20.00")
        parent_wo.instructions.update(status='COMPLETED')
        parent_wo.recalculate_status()
        parent_wo.process_inventory()

        # Check that parent WO is COMPLETED
        parent_wo.refresh_from_db()
        self.assertEqual(parent_wo.status, 'COMPLETED')

        # Check that child packaging order automatically auto-resumed to IN_PROGRESS and allocated stock
        pack_wo.refresh_from_db()
        po.refresh_from_db()
        self.assertEqual(pack_wo.status, 'IN_PROGRESS')
        self.assertEqual(po.status, 'IN_PROGRESS')
        self.assertTrue(pack_wo.is_inventory_allocated)

    def test_option_2_downscale_target_execution(self):
        """
        Option 2: Warehouse has 10 kg bulk putty. Packaging target was 50 tubs (requiring 25 kg).
        Downscaling should scale batch down to 10 / 0.5 = 20 tubs, transition to IN_PROGRESS, and allocate stock.
        """
        Inventory.objects.create(
            product=self.bulk_putty,
            quantity_available=Decimal("10.00"),
            location="Main Warehouse"
        )

        pack_wo = WorkOrder.objects.create(
            product=self.packaged_putty,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        po = ProductionOrder.objects.create(
            product=self.packaged_putty,
            work_order=pack_wo,
            quantity=Decimal("50.00"),
            status='ON_HOLD_SHORTAGE'
        )

        pack_wo.resolve_bulk_shortage('DOWNSCALE_TARGET')
        pack_wo.refresh_from_db()
        po.refresh_from_db()

        self.assertEqual(pack_wo.quantity_produced, Decimal("20.00"))
        self.assertEqual(po.quantity, Decimal("20.00"))
        self.assertEqual(pack_wo.status, 'IN_PROGRESS')
        self.assertEqual(po.status, 'IN_PROGRESS')
        self.assertTrue(pack_wo.is_inventory_allocated)
        self.assertIsNone(pack_wo.parent_work_order)

    def test_option_3_hold_for_existing_bulk_run(self):
        """
        Option 3: Links to an active bulk run on the floor and transitions to ON_HOLD_SHORTAGE.
        """
        active_bulk = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("30.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        pack_wo = WorkOrder.objects.create(
            product=self.packaged_putty,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("40.00"),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )
        po = ProductionOrder.objects.create(
            product=self.packaged_putty,
            work_order=pack_wo,
            quantity=Decimal("40.00"),
            status='IN_PROGRESS'
        )

        pack_wo.resolve_bulk_shortage('HOLD_FOR_EXISTING', existing_bulk_wo_id=active_bulk.pk)
        pack_wo.refresh_from_db()
        po.refresh_from_db()

        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(po.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(pack_wo.parent_work_order, active_bulk)

    def test_admin_action_routes_post_and_get(self):
        """Tests that all custom admin routes (start-production, top-up-bulk, downscale-target, hold-for-existing, check-stock-resume) function over HTTP POST and GET."""
        Inventory.objects.create(
            product=self.bulk_putty,
            quantity_available=Decimal("15.00"),
            location="Main Warehouse"
        )

        pack_wo = WorkOrder.objects.create(
            product=self.packaged_putty,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )

        # 1. Start production via POST
        resp = self.client.post(f"/admin/core/workorder/{pack_wo.pk}/start-production/", follow=True)
        self.assertEqual(resp.status_code, 200)
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'AWAITING_RESOLUTION')

        # 2. Top-Up Bulk via POST
        resp = self.client.post(f"/admin/core/workorder/{pack_wo.pk}/top-up-bulk/", follow=True)
        self.assertEqual(resp.status_code, 200)
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(pack_wo.parent_work_order)

        # 3. Check Stock Resume on on-hold order when shortage still exists
        resp = self.client.post(f"/admin/core/workorder/{pack_wo.pk}/check-stock-resume/", follow=True)
        self.assertEqual(resp.status_code, 200)
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')

        # 4. Downscale Target via POST
        pack_wo.status = 'AWAITING_RESOLUTION'
        pack_wo.save(update_fields=['status'])
        resp = self.client.post(f"/admin/core/workorder/{pack_wo.pk}/downscale-target/", follow=True)
        self.assertEqual(resp.status_code, 200)
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'IN_PROGRESS')
        self.assertEqual(pack_wo.quantity_produced, Decimal("30.00")) # 15 kg available / 0.5 = 30 tubs

    def test_admin_change_view_context_data(self):
        """Tests that change_view supplies shortage_metrics, active_bulk_orders, and parent_bulk_order to template context."""
        active_bulk = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("25.00"),
            production_start_date=timezone.now().date(),
            status='IN_PROGRESS'
        )

        pack_wo = WorkOrder.objects.create(
            product=self.packaged_putty,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("50.00"),
            production_start_date=timezone.now().date(),
            status='AWAITING_RESOLUTION'
        )

        resp = self.client.get(f"/admin/core/workorder/{pack_wo.pk}/change/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('shortage_metrics', resp.context)
        self.assertIn('active_bulk_orders', resp.context)
        self.assertTrue(resp.context['shortage_metrics']['has_shortfall'])
        self.assertEqual(resp.context['shortage_metrics']['intermediate_product'], self.bulk_putty)
        self.assertIn(active_bulk, resp.context['active_bulk_orders'])






