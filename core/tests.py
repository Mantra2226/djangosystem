from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from .models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory, 
    WorkOrder, ProductionOrder, PurchaseOrder, PurchaseOrderItem, 
    ProcurementOrder
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
        self.assertEqual(draft_po.status, "DRAFT")
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
