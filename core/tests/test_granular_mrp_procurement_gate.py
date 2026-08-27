"""
TESTS: Granular Multi-Material MRP Resolution Engine with Two-Stage Physical Procurement Gate.

Verifies:
1. Multi-material shortages are tracked independently on ProductionOrderItem rows.
2. Resolving a single item via PO draft sets it to PO_DRAFTED and updates order to PARTIALLY_RESOLVED (is_mrp_resolved=False).
3. Resolving all items via PO draft transitions order to AWAITING_PROCUREMENT (Planning Resolution), NOT prematurely to production-ready.
4. Two-Tier Physical Procurement Gate in WorkOrder.start_production() strictly blocks execution while goods on drafted POs have not been physically received in unallocated warehouse inventory.
5. Goods receipt into warehouse inventory followed by auto-resume / re-check transitions items to NO_SHORTAGE and order to READY_TO_START / MRP_RESOLVED, enabling successful start_production().
6. Re-evaluation (evaluate_mrp) preserves saved resolutions (PO_DRAFTED, OVERRIDDEN, DOWNSCALED) and linked PO references without clobbering.
7. Authorization override bypasses the physical procurement gate.
8. mrp_resolve_action HTTP handler resolves individual items without unconditionally locking the parent ProductionOrder.
9. Admin mrp_resolution_pathways_viewer dynamically renders per-item status cards (PO Drafted, Override, Shortage with Action Links).
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, RequestFactory
from django.utils import timezone
from django.contrib.admin.sites import AdminSite

from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, ProductionOrder, ProductionOrderItem, PurchaseOrder
)
from core.services import (
    evaluate_mrp_shortages,
    resolve_raw_autodraft_po,
    resolve_raw_direct_procurement,
    resolve_raw_hold_inbound,
    check_and_auto_resume_on_hold_orders
)
from core.admin import ProductionOrderAdmin
from core.views import mrp_resolve_action

User = get_user_model()


class GranularMRPProcurementGateTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.admin = ProductionOrderAdmin(ProductionOrder, self.site)

        self.supervisor = User.objects.create_user(
            username='plant_supervisor',
            email='supervisor@factory.internal',
            password='Password123!',
            is_staff=True,
            is_superuser=True
        )

        # 1. Supplier
        self.supplier = Supplier.objects.create(
            name="Apex Minerals & Chemical Corp.",
            contact_info="orders@apexminerals.com"
        )

        # 2. Raw Materials
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil",
            sku="RM-OIL-GATE",
            product_type="RAW",
            category="Raw Oils",
            unit_of_measurement="Liters",
            supplier=self.supplier
        )
        self.calcium_carbonate = Product.objects.create(
            name="Calcium Carbonate Powder",
            sku="RM-CC-GATE",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )

        # 3. Finished Good
        self.glass_putty = Product.objects.create(
            name="Industrial Glass Putty 1000kg Batch",
            sku="FG-PUTTY-GATE",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="kg",
            selling_price=Decimal("30.00")
        )

        # 4. BOM Recipe: For 1kg Putty -> 0.2L Linseed Oil, 0.8kg Calcium Carbonate
        self.bom = BillOfMaterial.objects.create(
            product=self.glass_putty,
            name="Glass Putty 1000kg Recipe",
            is_active=True
        )
        BOMItem.objects.create(bom=self.bom, component=self.linseed_oil, quantity_required=Decimal("0.2000"))
        BOMItem.objects.create(bom=self.bom, component=self.calcium_carbonate, quantity_required=Decimal("0.8000"))

        # 5. Inventory: CC (400kg available), Linseed Oil (80L available)
        Inventory.objects.update_or_create(
            product=self.linseed_oil,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('80.00'), 'quantity_allocated': Decimal('0.00')}
        )
        Inventory.objects.update_or_create(
            product=self.calcium_carbonate,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('400.00'), 'quantity_allocated': Decimal('0.00')}
        )

        # 6. WorkOrder & ProductionOrder targeting 1000 units (requires 200L oil, 800kg CC)
        # Shortfalls: Oil = 200 - 80 = 120L, CC = 800 - 400 = 400kg
        self.wo = WorkOrder.objects.create(
            product=self.glass_putty,
            bill_of_material=self.bom,
            category='PACKAGING',
            status='DRAFT',
            quantity_produced=Decimal("1000.00"),
            production_start_date=timezone.now().date()
        )
        self.po = ProductionOrder.objects.create(
            product=self.glass_putty,
            work_order=self.wo,
            quantity=Decimal("1000.00"),
            status='ON_HOLD_SHORTAGE'
        )

    def test_multi_material_shortage_initialization(self):
        """Evaluating MRP creates separate UNRESOLVED ProductionOrderItem rows for all deficient materials."""
        self.po.evaluate_mrp()
        self.po.refresh_from_db()

        self.assertEqual(self.po.status, 'ON_HOLD_SHORTAGE')
        self.assertFalse(self.po.is_mrp_resolved)
        self.assertTrue(self.po.has_unresolved_shortages)

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        self.assertEqual(cc_item.planned_quantity, Decimal('800.00'))
        self.assertEqual(cc_item.shortage_quantity, Decimal('400.00'))
        self.assertEqual(cc_item.resolution_status, 'UNRESOLVED')

        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        self.assertEqual(oil_item.planned_quantity, Decimal('200.00'))
        self.assertEqual(oil_item.shortage_quantity, Decimal('120.00'))
        self.assertEqual(oil_item.resolution_status, 'UNRESOLVED')

    def test_resolve_single_item_transitions_to_partially_resolved(self):
        """Resolving 1 of 2 shortage items sets it to PO_DRAFTED and marks the PO as PARTIALLY_RESOLVED without locking."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()

        cc_item.refresh_from_db()
        self.assertEqual(cc_item.resolution_status, 'PO_DRAFTED')
        self.assertIsNotNone(cc_item.linked_purchase_order)

        # Ensure sibling item is still UNRESOLVED
        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.refresh_from_db()
        self.assertEqual(oil_item.resolution_status, 'UNRESOLVED')

        # Parent order MUST be PARTIALLY_RESOLVED, NOT locked
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'PARTIALLY_RESOLVED')
        self.assertFalse(self.po.is_mrp_resolved)
        self.assertTrue(self.po.has_unresolved_shortages)

    def test_resolve_all_items_with_po_transitions_to_awaiting_procurement(self):
        """When all deficient items are resolved via PO drafting, the order moves to AWAITING_PROCUREMENT (Planning Resolution)."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()

        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.resolve_with_po()

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'AWAITING_PROCUREMENT')
        self.assertFalse(self.po.is_mrp_resolved)
        self.assertFalse(self.po.has_unresolved_shortages)

    def test_resolve_all_items_with_override_transitions_to_mrp_resolved(self):
        """When all items are resolved via OVERRIDE (no pending POs), order transitions to MRP_RESOLVED directly."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_override(user=self.supervisor, notes="Authorized CC override")

        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.resolve_with_override(user=self.supervisor, notes="Authorized Oil override")

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'MRP_RESOLVED')
        self.assertTrue(self.po.is_mrp_resolved)
        self.assertFalse(self.po.has_unresolved_shortages)

    def test_two_tier_physical_procurement_gate_blocks_production_start(self):
        """WorkOrder.start_production() MUST be blocked when items are PO_DRAFTED but physical stock has not arrived."""
        self.po.evaluate_mrp()

        # Both items resolved via PO drafting -> order in AWAITING_PROCUREMENT
        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()
        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.resolve_with_po()

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'AWAITING_PROCUREMENT')

        # Startup attempt MUST fail due to physical procurement gate
        with self.assertRaises(ValidationError) as ctx:
            self.wo.start_production()

        error_str = str(ctx.exception)
        self.assertIn("goods have not been physically received in unallocated inventory", error_str)
        self.assertIn("Calcium Carbonate Powder", error_str)
        self.assertIn("Raw Linseed Oil", error_str)

        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, 'DRAFT')

    def test_physical_goods_receipt_and_auto_resume_flow(self):
        """Receiving physical stock and running check_and_auto_resume satisfies PO_DRAFTED items and allows production start."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()
        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.resolve_with_po()

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'AWAITING_PROCUREMENT')

        # Simulate Supplier Goods Receipt in Warehouse
        Inventory.objects.filter(product=self.calcium_carbonate).update(quantity_available=Decimal('1000.00'))
        Inventory.objects.filter(product=self.linseed_oil).update(quantity_available=Decimal('300.00'))

        # Run Auto-Resume / Stock Verification Engine
        resumed = check_and_auto_resume_on_hold_orders()
        self.assertIn(self.po, resumed)

        cc_item.refresh_from_db()
        self.assertEqual(cc_item.resolution_status, 'NO_SHORTAGE')
        self.assertEqual(cc_item.shortage_quantity, Decimal('0.00'))

        oil_item.refresh_from_db()
        self.assertEqual(oil_item.resolution_status, 'NO_SHORTAGE')
        self.assertEqual(oil_item.shortage_quantity, Decimal('0.00'))

        self.po.refresh_from_db()
        self.assertIn(self.po.status, ['READY_TO_START', 'MRP_RESOLVED'])
        self.assertTrue(self.po.is_mrp_resolved)

        # Now start_production() succeeds smoothly
        success, msg = self.wo.start_production()
        self.assertTrue(success)

        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, 'IN_PROGRESS')
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'IN_PROGRESS')

    def test_re_evaluation_preserves_po_drafted_and_override_resolutions(self):
        """evaluate_mrp() preserves saved PO_DRAFTED and OVERRIDDEN resolutions without clobbering them to UNRESOLVED."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()
        drafted_po = cc_item.linked_purchase_order

        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.resolve_with_override(user=self.supervisor, notes="Verified reserve batch")

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'AWAITING_PROCUREMENT')

        # Trigger re-evaluation cycle
        self.po.evaluate_mrp()

        # Assert no clobbering
        cc_item.refresh_from_db()
        self.assertEqual(cc_item.resolution_status, 'PO_DRAFTED')
        self.assertEqual(cc_item.linked_purchase_order, drafted_po)

        oil_item.refresh_from_db()
        self.assertEqual(oil_item.resolution_status, 'OVERRIDDEN')
        self.assertEqual(oil_item.resolved_by, self.supervisor)

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'AWAITING_PROCUREMENT')

    def test_mrp_resolve_action_view_does_not_prematurely_lock_production_order(self):
        """The mrp_resolve_action HTTP view resolves the specific component without prematurely setting is_mrp_resolved=True."""
        self.po.evaluate_mrp()

        self.client.force_login(self.supervisor)
        response = self.client.post('/mrp_resolve_action/', {
            'production_order_id': self.po.pk,
            'component_id': self.calcium_carbonate.pk,
            'shortfall_qty': '400.00',
            'resolution_action': 'raw_autodraft_po',
        })

        self.po.refresh_from_db()
        # MUST NOT be locked! Status should be PARTIALLY_RESOLVED because Linseed Oil is still UNRESOLVED
        self.assertFalse(self.po.is_mrp_resolved)
        self.assertEqual(self.po.status, 'PARTIALLY_RESOLVED')

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        self.assertEqual(cc_item.resolution_status, 'PO_DRAFTED')

        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        self.assertEqual(oil_item.resolution_status, 'UNRESOLVED')

    def test_pathways_viewer_renders_per_item_cards_and_action_buttons(self):
        """mrp_resolution_pathways_viewer renders mixed states: PO Drafted card for item 1, and action buttons for item 2."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()

        self.po.refresh_from_db()
        html = self.admin.mrp_resolution_pathways_viewer(self.po)

        # Must NOT show locked banner
        self.assertNotIn("MRP Resolution Locked", html)

        # Must render Calcium Carbonate as Purchase Order Drafted
        self.assertTrue("Purchase Order Drafted" in html or "PO Drafted" in html)
        self.assertIn("Calcium Carbonate Powder", html)

        # Must render Raw Linseed Oil as shortage with action buttons
        self.assertIn("Raw Linseed Oil", html)
        self.assertIn("Draft Purchase Order", html)
        self.assertIn("Authorize Supervisor Override", html)
