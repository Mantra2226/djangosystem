from decimal import Decimal
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.utils import timezone

from core.models import (
    Product, BillOfMaterial, BOMItem, Inventory, Supplier,
    WorkOrder, ProductionOrder, ProductionOrderItem, PurchaseOrder, PurchaseOrderItem
)
from core.services import (
    evaluate_mrp_shortages,
    resolve_raw_autodraft_po,
    resolve_raw_hold_inbound,
    resolve_batch_downscale,
    resolve_intermediate_build,
    resolve_intermediate_hold_active,
    resolve_item_override,
    check_and_auto_resume_on_hold_orders
)


class MultiComponentMRPPathwaysTest(TestCase):
    """
    Test suite verifying multi-component MRP resolution matrix across formulations
    with 6+ components, covering mixed pathway resolutions, proportional batch downscaling,
    and the two-tier physical procurement gate.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="mrp_supervisor", password="password123")
        self.supplier = Supplier.objects.create(name="Primary Chemical Supplier", contact_info="supplier@chem.com")

        # Finished Good
        self.finished_putty = Product.objects.create(
            name="Premium Glazing Putty 1000",
            sku="FG-PUTTY-1000",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="kg",
            selling_price=Decimal('50.00')
        )

        # 6 Components (5 Raw Materials, 1 Intermediate WIP)
        self.chalk = Product.objects.create(
            name="Calcium Carbonate (Chalk)",
            sku="RM-CHALK-01",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.oil = Product.objects.create(
            name="Boiled Linseed Oil",
            sku="RM-OIL-01",
            product_type="RAW",
            category="Oils",
            unit_of_measurement="L",
            supplier=self.supplier
        )
        self.resin = Product.objects.create(
            name="Synthetic Resin Binder",
            sku="RM-RESIN-01",
            product_type="RAW",
            category="Resins",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.dryer = Product.objects.create(
            name="Cobalt Liquid Dryer",
            sku="RM-DRYER-01",
            product_type="RAW",
            category="Additives",
            unit_of_measurement="L",
            supplier=self.supplier
        )
        self.pigment = Product.objects.create(
            name="Titanium White Pigment",
            sku="RM-PIGMENT-01",
            product_type="RAW",
            category="Pigments",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.inter_base = Product.objects.create(
            name="Pre-Milled Base Paste (WIP)",
            sku="INT-BASE-01",
            product_type="INTERMEDIATE",
            category="WIP",
            unit_of_measurement="kg"
        )

        # Sub-BOM for Intermediate Base Paste
        self.sub_raw1 = Product.objects.create(
            name="Raw Mineral Filler",
            sku="RM-MINERAL-01",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.sub_bom = BillOfMaterial.objects.create(
            product=self.inter_base,
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.sub_bom,
            component=self.sub_raw1,
            quantity_required=Decimal('1.00')
        )

        # Main 6-Component BOM (per 1 unit of finished putty)
        self.main_bom = BillOfMaterial.objects.create(
            product=self.finished_putty,
            is_active=True
        )
        self.bom_chalk = BOMItem.objects.create(bom=self.main_bom, component=self.chalk, quantity_required=Decimal('10.00'))
        self.bom_oil = BOMItem.objects.create(bom=self.main_bom, component=self.oil, quantity_required=Decimal('2.00'))
        self.bom_resin = BOMItem.objects.create(bom=self.main_bom, component=self.resin, quantity_required=Decimal('1.00'))
        self.bom_dryer = BOMItem.objects.create(bom=self.main_bom, component=self.dryer, quantity_required=Decimal('0.50'))
        self.bom_pigment = BOMItem.objects.create(bom=self.main_bom, component=self.pigment, quantity_required=Decimal('0.50'))
        self.bom_inter = BOMItem.objects.create(bom=self.main_bom, component=self.inter_base, quantity_required=Decimal('3.00'))

    def test_six_component_formulation_with_mixed_resolutions(self):
        """
        Tests a batch of 10 units (requires 100 Chalk, 20 Oil, 10 Resin, 5 Dryer, 5 Pigment, 30 Base Paste).
        Stock state:
        1. Chalk: 150 available (NO_SHORTAGE)
        2. Oil: 0 available (Shortage 20) -> Resolve with Draft PO (PO_DRAFTED)
        3. Resin: 0 available (Shortage 10) -> Resolve with Hold Inbound PO (PO_DRAFTED)
        4. Dryer: 0 available (Shortage 5) -> Resolve with Supervisor Override (OVERRIDDEN)
        5. Base Paste: 0 available (Shortage 30) -> Resolve with Spawn Child WO (CHILD_WO_CREATED)
        6. Pigment: 0 available (Shortage 5) -> Initially UNRESOLVED

        Verifies:
        - Order is PARTIALLY_RESOLVED and blocks start_production.
        - When Pigment is resolved via Draft PO, order transitions to AWAITING_PROCUREMENT.
        - start_production is STILL blocked because physical goods are not unallocated in warehouse.
        - Receiving physical stock enables start_production.
        """
        # Step 1: Set stock
        Inventory.objects.create(product=self.chalk, quantity_available=Decimal('150.00'), unit_cost=Decimal('1.00'))
        Inventory.objects.create(product=self.oil, quantity_available=Decimal('0.00'), unit_cost=Decimal('5.00'))
        Inventory.objects.create(product=self.resin, quantity_available=Decimal('0.00'), unit_cost=Decimal('8.00'))
        Inventory.objects.create(product=self.dryer, quantity_available=Decimal('0.00'), unit_cost=Decimal('12.00'))
        Inventory.objects.create(product=self.pigment, quantity_available=Decimal('0.00'), unit_cost=Decimal('15.00'))
        Inventory.objects.create(product=self.inter_base, quantity_available=Decimal('0.00'), unit_cost=Decimal('6.00'))

        # Existing inbound PO for Resin
        inbound_po = PurchaseOrder.objects.create(
            supplier=self.supplier,
            status='SENT',
            notes="Inbound supply of Resin"
        )
        PurchaseOrderItem.objects.create(
            purchase_order=inbound_po,
            product=self.resin,
            quantity_ordered=Decimal('50.00'),
            price_per_unit=Decimal('8.00')
        )

        # Create WorkOrder & ProductionOrder for 10 units
        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.main_bom,
            quantity_produced=Decimal('10.00'),
            production_start_date=timezone.now().date(),
            status='PENDING'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('10.00'),
            status='PENDING'
        )

        # Initial MRP evaluation
        po.evaluate_mrp()
        po.refresh_from_db()
        self.assertEqual(po.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(po.items.count(), 6)
        self.assertEqual(po.items.get(raw_material=self.chalk).resolution_status, 'NO_SHORTAGE')
        self.assertEqual(po.items.filter(resolution_status='UNRESOLVED').count(), 5)

        # Step 2: Apply mixed resolutions for components 2, 3, 4, 5
        # Component 2 (Oil): Auto-draft PO
        resolve_raw_autodraft_po(po, self.oil.pk, Decimal('20.00'))
        # Component 3 (Resin): Hold inbound PO
        resolve_raw_hold_inbound(po, self.resin.pk, inbound_po=inbound_po)
        # Component 4 (Dryer): Supervisor override
        resolve_item_override(po, self.dryer.pk, user=self.user, notes="Authorized bypass for lab test batch")
        # Component 5 (Base Paste): Trigger Child Work Order
        child_wo, child_po = resolve_intermediate_build(po, self.inter_base.pk, Decimal('30.00'))

        po.refresh_from_db()
        # Component 6 (Pigment) is still UNRESOLVED
        self.assertEqual(po.status, 'PARTIALLY_RESOLVED')
        self.assertFalse(po.is_mrp_resolved)
        self.assertEqual(po.items.get(raw_material=self.oil).resolution_status, 'PO_DRAFTED')
        self.assertEqual(po.items.get(raw_material=self.resin).resolution_status, 'PO_DRAFTED')
        self.assertEqual(po.items.get(raw_material=self.dryer).resolution_status, 'OVERRIDDEN')
        self.assertEqual(po.items.get(raw_material=self.inter_base).resolution_status, 'CHILD_WO_CREATED')
        self.assertEqual(po.items.get(raw_material=self.pigment).resolution_status, 'UNRESOLVED')

        # Verify WorkOrder cannot start while in PARTIALLY_RESOLVED
        with self.assertRaises(ValidationError) as cm:
            wo.start_production()
        self.assertIn("unresolved raw material shortages", str(cm.exception))

        # Step 3: Resolve the final component (Pigment) via Draft PO
        resolve_raw_autodraft_po(po, self.pigment.pk, Decimal('5.00'))
        po.refresh_from_db()

        # All 6 components are now resolved in planning, but physical stock is missing -> AWAITING_PROCUREMENT
        self.assertEqual(po.status, 'AWAITING_PROCUREMENT')
        self.assertFalse(po.is_mrp_resolved)

        # Verify WorkOrder start_production is STILL blocked by Two-Tier Physical Gate
        with self.assertRaises(ValidationError) as cm:
            wo.start_production()
        self.assertIn("goods have not been physically received", str(cm.exception))
        self.assertIn(self.oil.name, str(cm.exception))

        # Step 4: Simulate delivery and receipt of physical goods into warehouse
        oil_inv = Inventory.objects.get(product=self.oil)
        oil_inv.quantity_available = Decimal('20.00')
        oil_inv.save()

        resin_inv = Inventory.objects.get(product=self.resin)
        resin_inv.quantity_available = Decimal('10.00')
        resin_inv.save()

        pigment_inv = Inventory.objects.get(product=self.pigment)
        pigment_inv.quantity_available = Decimal('5.00')
        pigment_inv.save()

        base_inv = Inventory.objects.get(product=self.inter_base)
        base_inv.quantity_available = Decimal('30.00')
        base_inv.save()

        # Trigger auto-resume or state update
        po.update_mrp_resolution_state()
        po.refresh_from_db()
        self.assertEqual(po.status, 'READY_TO_START')
        self.assertTrue(po.is_mrp_resolved)

        # WorkOrder can now start production cleanly
        wo.start_production()
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')

    def test_batch_downscale_recalculates_all_six_components_proportionally(self):
        """
        Tests batch downscaling across a 6-component formulation:
        Target batch: 100 units
        Planned Requirements:
        - Chalk: 1000 kg (Have: 1000)
        - Oil: 200 L (Have: 100 L -> 50% bottleneck!)
        - Resin: 100 kg (Have: 80 kg)
        - Dryer: 50 L (Have: 50 L)
        - Pigment: 50 kg (Have: 50 kg)
        - Base Paste: 300 kg (Have: 300 kg)

        Downscaling on Oil (bottleneck, 100 L available / 2 L per unit = 50 units max yield):
        - Scales production_order.quantity to 50.00.
        - Scales work_order.quantity_produced to 50.00.
        - Recalculates all planned quantities:
          * Chalk: 500 kg (Have 1000 -> NO_SHORTAGE)
          * Oil: 100 L (Marked DOWNSCALED)
          * Resin: 50 kg (Have 80 -> NO_SHORTAGE)
          * Dryer: 25 L (Have 50 -> NO_SHORTAGE)
          * Pigment: 25 kg (Have 50 -> NO_SHORTAGE)
          * Base Paste: 150 kg (Have 300 -> NO_SHORTAGE)
        - ProductionOrder status becomes READY_TO_START / MRP_RESOLVED.
        - WorkOrder starts successfully.
        """
        Inventory.objects.create(product=self.chalk, quantity_available=Decimal('1000.00'), unit_cost=Decimal('1.00'))
        Inventory.objects.create(product=self.oil, quantity_available=Decimal('100.00'), unit_cost=Decimal('5.00'))
        Inventory.objects.create(product=self.resin, quantity_available=Decimal('80.00'), unit_cost=Decimal('8.00'))
        Inventory.objects.create(product=self.dryer, quantity_available=Decimal('50.00'), unit_cost=Decimal('12.00'))
        Inventory.objects.create(product=self.pigment, quantity_available=Decimal('50.00'), unit_cost=Decimal('15.00'))
        Inventory.objects.create(product=self.inter_base, quantity_available=Decimal('300.00'), unit_cost=Decimal('6.00'))

        wo = WorkOrder.objects.create(
            product=self.finished_putty,
            bill_of_material=self.main_bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='PENDING'
        )
        po = ProductionOrder.objects.create(
            product=self.finished_putty,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='PENDING'
        )

        po.evaluate_mrp()
        po.refresh_from_db()
        self.assertEqual(po.status, 'ON_HOLD_SHORTAGE')
        self.assertEqual(po.items.get(raw_material=self.oil).shortage_quantity, Decimal('100.00'))
        self.assertEqual(po.items.get(raw_material=self.resin).shortage_quantity, Decimal('20.00'))

        # Downscale batch on Oil (bottleneck)
        resolve_batch_downscale(po, self.oil.pk)

        po.refresh_from_db()
        wo.refresh_from_db()

        # Batch target updated to 50 units
        self.assertEqual(po.quantity, Decimal('50.00'))
        self.assertEqual(wo.quantity_produced, Decimal('50.00'))

        # Check item planned quantities and statuses
        chalk_item = po.items.get(raw_material=self.chalk)
        self.assertEqual(chalk_item.planned_quantity, Decimal('500.00'))
        self.assertEqual(chalk_item.resolution_status, 'NO_SHORTAGE')

        oil_item = po.items.get(raw_material=self.oil)
        self.assertEqual(oil_item.planned_quantity, Decimal('100.00'))
        self.assertEqual(oil_item.resolution_status, 'DOWNSCALED')
        self.assertEqual(oil_item.shortage_quantity, Decimal('0.00'))

        resin_item = po.items.get(raw_material=self.resin)
        self.assertEqual(resin_item.planned_quantity, Decimal('50.00'))
        self.assertEqual(resin_item.resolution_status, 'NO_SHORTAGE')
        self.assertEqual(resin_item.shortage_quantity, Decimal('0.00'))

        dryer_item = po.items.get(raw_material=self.dryer)
        self.assertEqual(dryer_item.planned_quantity, Decimal('25.00'))
        self.assertEqual(dryer_item.resolution_status, 'NO_SHORTAGE')

        pigment_item = po.items.get(raw_material=self.pigment)
        self.assertEqual(pigment_item.planned_quantity, Decimal('25.00'))
        self.assertEqual(pigment_item.resolution_status, 'NO_SHORTAGE')

        inter_item = po.items.get(raw_material=self.inter_base)
        self.assertEqual(inter_item.planned_quantity, Decimal('150.00'))
        self.assertEqual(inter_item.resolution_status, 'NO_SHORTAGE')

        # Since all components are now physically available at 50 units
        self.assertEqual(po.status, 'MRP_RESOLVED')
        self.assertTrue(po.is_mrp_resolved)

        # WorkOrder can start immediately
        wo.start_production()
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
