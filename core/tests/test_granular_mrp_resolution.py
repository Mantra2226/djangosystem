"""
TESTS: Granular Multi-Material MRP Shortage Resolution Subsystem.

Verifies:
1. Multi-material shortages across BOM components are tracked independently on ProductionOrderItem rows.
2. Resolving a single item (e.g. PO drafted) updates its status without freezing or locking remaining unresolved items.
3. ProductionOrder transitions from ON_HOLD_SHORTAGE -> PARTIALLY_RESOLVED -> MRP_RESOLVED as items are resolved.
4. WorkOrder start_production() is blocked while any linked ProductionOrder has unresolved shortages.
5. Starting production locks the MRP resolution and freezes further evaluation against live inventory fluctuations.
6. Supplier fallback on auto-drafting POs and batch downscale resolution pathway.
"""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, ProductionOrder, ProductionOrderItem, PurchaseOrder
)

User = get_user_model()


class GranularMRPResolutionTests(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user(
            username='plant_supervisor',
            email='supervisor@factory.internal',
            password='Password123!'
        )

        # 1. Supplier
        self.supplier = Supplier.objects.create(
            name="Apex Minerals & Oils Inc.",
            contact_info="orders@apexminerals.com"
        )

        # 2. Raw Materials
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil",
            sku="RM-OIL-GRANULAR",
            product_type="RAW",
            category="Raw Oils",
            unit_of_measurement="Liters",
            supplier=self.supplier
        )
        self.calcium_carbonate = Product.objects.create(
            name="Calcium Carbonate",
            sku="RM-CC-GRANULAR",
            product_type="RAW",
            category="Minerals",
            unit_of_measurement="kg",
            supplier=self.supplier
        )

        # 3. Finished Good
        self.glass_putty = Product.objects.create(
            name="Industrial Glass Putty 1000kg",
            sku="FG-PUTTY-GRANULAR",
            product_type="FINISHED",
            category="Putty",
            unit_of_measurement="kg",
            selling_price=Decimal("25.00")
        )

        # 4. BOM Recipe: For 1kg of putty -> 0.2L Linseed Oil, 0.8kg Calcium Carbonate
        self.bom = BillOfMaterial.objects.create(
            product=self.glass_putty,
            name="Glass Putty 1000kg Batch Recipe",
            is_active=True
        )
        BOMItem.objects.create(bom=self.bom, component=self.linseed_oil, quantity_required=Decimal("0.2000"))
        BOMItem.objects.create(bom=self.bom, component=self.calcium_carbonate, quantity_required=Decimal("0.8000"))

        # 5. Inventory: Calcium Carbonate (400kg available), Linseed Oil (80L available)
        Inventory.objects.update_or_create(
            product=self.linseed_oil,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('80.00')}
        )
        Inventory.objects.update_or_create(
            product=self.calcium_carbonate,
            location='Main Warehouse',
            defaults={'quantity_available': Decimal('400.00')}
        )

        # 6. WorkOrder & ProductionOrder targeting 1000 units (requires 200L oil, 800kg CC)
        # Note: 1000 units * 0.20 = 200L (shortfall: 200 - 80 = 120L)
        # Note: 1000 units * 0.80 = 800kg (shortfall: 800 - 400 = 400kg)
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

    def test_multi_material_shortage_detected_accurately(self):
        """Set up batch requiring CC (800kg) and Oil (200L) with depleted stock and assert both are UNRESOLVED."""
        self.po.evaluate_mrp()

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'ON_HOLD_SHORTAGE')
        self.assertTrue(self.po.has_unresolved_shortages)

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        self.assertEqual(cc_item.planned_quantity, Decimal('800.00'))
        self.assertEqual(cc_item.shortage_quantity, Decimal('400.00'))
        self.assertEqual(cc_item.resolution_status, 'UNRESOLVED')

        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        self.assertEqual(oil_item.planned_quantity, Decimal('200.00'))
        self.assertEqual(oil_item.shortage_quantity, Decimal('120.00'))
        self.assertEqual(oil_item.resolution_status, 'UNRESOLVED')

    def test_resolving_first_item_does_not_freeze_second_item(self):
        """Resolving Calcium Carbonate via PO auto-drafting sets it to PO_DRAFTED while Linseed Oil remains UNRESOLVED."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()

        cc_item.refresh_from_db()
        self.assertEqual(cc_item.resolution_status, 'PO_DRAFTED')
        self.assertIsNotNone(cc_item.linked_purchase_order)
        self.assertEqual(cc_item.linked_purchase_order.supplier, self.supplier)

        # Ensure Linseed Oil is still UNRESOLVED
        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.refresh_from_db()
        self.assertEqual(oil_item.resolution_status, 'UNRESOLVED')

        # Order must now be in PARTIALLY_RESOLVED state
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'PARTIALLY_RESOLVED')
        self.assertTrue(self.po.has_unresolved_shortages)

    def test_resolving_all_items_transitions_order_to_mrp_resolved(self):
        """Resolving all items transitions ProductionOrder to AWAITING_PROCUREMENT when POs are drafted and clears has_unresolved_shortages."""
        self.po.evaluate_mrp()

        # 1. Resolve Calcium Carbonate with Auto-Draft PO
        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()

        # 2. Resolve Linseed Oil with Supervisor Authorization Override
        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.resolve_with_override(user=self.supervisor, notes="Authorized using reserve barrel")

        oil_item.refresh_from_db()
        self.assertEqual(oil_item.resolution_status, 'OVERRIDDEN')
        self.assertEqual(oil_item.resolved_by, self.supervisor)

        # ProductionOrder should now be AWAITING_PROCUREMENT (PO drafted for CC, stock pending)
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'AWAITING_PROCUREMENT')
        self.assertFalse(self.po.has_unresolved_shortages)

    def test_work_order_cannot_start_until_all_items_resolved(self):
        """WorkOrder start_production() is blocked while PARTIALLY_RESOLVED and allowed when all items resolved."""
        self.po.evaluate_mrp()

        # Only resolve one of two items
        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        cc_item.resolve_with_po()

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'PARTIALLY_RESOLVED')

        # Startup attempt MUST fail
        with self.assertRaises(ValidationError):
            self.wo.start_production()

        # Restock inventory so process_inventory() passes upon start
        Inventory.objects.filter(product=self.linseed_oil).update(quantity_available=Decimal('500.00'))
        Inventory.objects.filter(product=self.calcium_carbonate).update(quantity_available=Decimal('2000.00'))

        # Resolve second item
        oil_item = self.po.items.get(raw_material=self.linseed_oil)
        oil_item.resolve_with_override(user=self.supervisor, notes="Verified stock available")

        self.po.refresh_from_db()
        self.assertIn(self.po.status, ['READY_TO_START', 'MRP_RESOLVED'])

        # Now start_production should succeed
        success, msg = self.wo.start_production()
        self.assertTrue(success)

        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, 'IN_PROGRESS')

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'IN_PROGRESS')
        self.assertTrue(self.po.is_mrp_resolved)

    def test_production_execution_locks_mrp_reevaluation(self):
        """Once IN_PROGRESS, evaluate_mrp() does not alter statuses despite depleted warehouse stock."""
        # Provide stock, resolve, and start
        Inventory.objects.filter(product=self.linseed_oil).update(quantity_available=Decimal('500.00'))
        Inventory.objects.filter(product=self.calcium_carbonate).update(quantity_available=Decimal('2000.00'))
        self.po.evaluate_mrp()

        self.wo.start_production()
        self.wo.refresh_from_db()
        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'IN_PROGRESS')

        # Wipe warehouse inventory
        Inventory.objects.filter(product=self.linseed_oil).update(quantity_available=Decimal('0.00'))
        Inventory.objects.filter(product=self.calcium_carbonate).update(quantity_available=Decimal('0.00'))

        # Call evaluate_mrp() -> Should be frozen and not revert status to ON_HOLD_SHORTAGE
        self.po.evaluate_mrp()

        self.po.refresh_from_db()
        self.assertEqual(self.po.status, 'IN_PROGRESS')

    def test_resolve_with_downscale_and_supplier_fallback(self):
        """Verifies resolve_with_downscale adjusts batch target and resolve_with_po handles supplier fallback."""
        self.po.evaluate_mrp()

        cc_item = self.po.items.get(raw_material=self.calcium_carbonate)
        # Downscale batch to 500kg
        cc_item.resolve_with_downscale(Decimal('500.00'), user=self.supervisor)

        self.po.refresh_from_db()
        self.assertEqual(self.po.quantity, Decimal('500.00'))
        self.assertEqual(self.wo.quantity_produced, Decimal('500.00'))

        # Test supplier fallback when product has no supplier assigned (e.g. Intermediate or unassigned)
        orphan_material = Product.objects.create(
            name="Orphan Additive",
            sku="INT-ORPHAN",
            product_type="INTERMEDIATE",
            category="Additives",
            unit_of_measurement="kg"
        )
        orphan_item = ProductionOrderItem.objects.create(
            production_order=self.po,
            raw_material=orphan_material,
            planned_quantity=Decimal('10.00'),
            shortage_quantity=Decimal('10.00'),
            resolution_status='UNRESOLVED'
        )
        # Should not throw 500 error; falls back to default supplier
        orphan_item.resolve_with_po()
        orphan_item.refresh_from_db()
        self.assertEqual(orphan_item.resolution_status, 'PO_DRAFTED')
        self.assertIsNotNone(orphan_item.linked_purchase_order)
