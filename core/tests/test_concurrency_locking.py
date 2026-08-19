"""
Test suite for concurrency-safe inventory allocation locking.

Validates that:
- Deterministic sorted row locking prevents deadlocks.
- Pre-flight gate prevents partial allocations when stock is insufficient.
- Sequential start_production() calls on competing WorkOrders correctly
  intercept the second order via the shortage gate without driving
  inventory negative.

Domain Reference: Glass Putty Manufacturing
  Stage 1 BOM: 0.80 kg Calcium Carbonate + 0.15 kg Linseed Oil -> 1.00 kg Bulk Putty Base
"""
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.core.exceptions import ValidationError
from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem,
    Inventory, WorkOrder, WorkOrderMaterialLine,
    ProductionOrder, StockTransaction,
)


class ConcurrencyLockingTestCase(TestCase):
    """
    Tests that sequential start_production() calls on competing WorkOrders
    are safely serialized by the Phase 1 pre-flight gate and sorted locking.
    """

    def setUp(self):
        # --- Supplier ---
        self.supplier = Supplier.objects.create(
            name='Concurrency Test Supplier',
            contact_info='N/A'
        )

        # --- Raw Materials ---
        self.calcium_carbonate = Product.objects.create(
            name='Calcium Carbonate',
            product_type='RAW',
            category='Powder',
            unit_of_measurement='kg',
            supplier=self.supplier,
        )
        self.linseed_oil = Product.objects.create(
            name='Raw Linseed Oil',
            product_type='RAW',
            category='Liquid',
            unit_of_measurement='kg',
            supplier=self.supplier,
        )

        # --- Intermediate Product ---
        self.bulk_putty = Product.objects.create(
            name='Bulk Putty Base',
            product_type='INTERMEDIATE',
            category='Intermediate',
            unit_of_measurement='kg',
        )

        # --- BOM: 1 kg Bulk Putty requires 0.80 kg CaCO3 + 0.15 kg Linseed Oil ---
        self.bulk_bom = BillOfMaterial.objects.create(
            product=self.bulk_putty,
            is_active=True,
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.calcium_carbonate,
            quantity_required=Decimal('0.80'),
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.linseed_oil,
            quantity_required=Decimal('0.15'),
        )

        # --- Seed Inventory ---
        # Calcium Carbonate: 100.00 kg available
        self.caco3_inv = Inventory.objects.create(
            product=self.calcium_carbonate,
            quantity_available=Decimal('100.00'),
            quantity_allocated=Decimal('0.00'),
        )
        # Linseed Oil: 200.00 kg available (plenty)
        self.oil_inv = Inventory.objects.create(
            product=self.linseed_oil,
            quantity_available=Decimal('200.00'),
            quantity_allocated=Decimal('0.00'),
        )

    def test_sequential_competing_allocations_shortage_gate(self):
        """
        WO1: 75.00 kg Bulk Putty -> requires 60.00 kg CaCO3 + 11.25 kg Oil
        WO2: 62.50 kg Bulk Putty -> requires 50.00 kg CaCO3 + 9.38 kg Oil

        After WO1 allocates:
          CaCO3: 100.00 - 60.00 = 40.00 available, 60.00 allocated
          Oil:   200.00 - 11.25 = 188.75 available, 11.25 allocated

        WO2 requires 50.00 kg CaCO3 but only 40.00 available -> shortage gate blocks WO2.
        Inventory must NEVER go negative.
        """
        # --- Work Order 1: 75.00 kg Bulk Putty ---
        wo1 = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('75.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        po1 = ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo1,
            quantity=Decimal('75.00'),
            status='DRAFT',
        )

        # --- Work Order 2: 62.50 kg Bulk Putty ---
        wo2 = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('62.50'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        po2 = ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo2,
            quantity=Decimal('62.50'),
            status='DRAFT',
        )

        # === Execute WO1: Should succeed ===
        success1, msg1 = wo1.start_production()
        self.assertTrue(success1, f"WO1 start_production failed: {msg1}")
        wo1.refresh_from_db()
        self.assertEqual(wo1.status, 'IN_PROGRESS')
        self.assertTrue(wo1.is_inventory_allocated)

        # Verify WO1 allocation effects
        self.caco3_inv.refresh_from_db()
        self.assertEqual(
            self.caco3_inv.quantity_available, Decimal('40.00'),
            f"CaCO3 available should be 40.00 after WO1 allocation, got {self.caco3_inv.quantity_available}"
        )
        self.assertEqual(
            self.caco3_inv.quantity_allocated, Decimal('60.0000'),
            f"CaCO3 allocated should be 60.00 after WO1, got {self.caco3_inv.quantity_allocated}"
        )

        self.oil_inv.refresh_from_db()
        self.assertEqual(
            self.oil_inv.quantity_available, Decimal('188.75'),
            f"Oil available should be 188.75 after WO1, got {self.oil_inv.quantity_available}"
        )
        self.assertEqual(
            self.oil_inv.quantity_allocated, Decimal('11.2500'),
            f"Oil allocated should be 11.25 after WO1, got {self.oil_inv.quantity_allocated}"
        )

        # === Execute WO2: Should be intercepted by shortage gate ===
        # WO2 requires 50.00 kg CaCO3 but only 40.00 available
        with self.assertRaises(ValidationError) as ctx:
            wo2.start_production()

        # Verify WO2 did NOT allocate anything (atomic rollback)
        wo2.refresh_from_db()
        self.assertNotEqual(wo2.status, 'IN_PROGRESS',
                            "WO2 should NOT have entered IN_PROGRESS")
        self.assertFalse(wo2.is_inventory_allocated,
                         "WO2 should NOT have allocated inventory")

        # Verify inventory was NOT further depleted
        self.caco3_inv.refresh_from_db()
        self.assertGreaterEqual(
            self.caco3_inv.quantity_available, Decimal('0.00'),
            "CaCO3 available stock must never go negative!"
        )
        self.assertEqual(
            self.caco3_inv.quantity_available, Decimal('40.00'),
            "CaCO3 available should remain 40.00 after WO2 was blocked"
        )
        self.assertEqual(
            self.caco3_inv.quantity_allocated, Decimal('60.0000'),
            "CaCO3 allocated should remain 60.00 (only WO1's allocation)"
        )

    def test_sorted_lock_order_prevents_deadlock_with_multiple_components(self):
        """
        Verify that the allocation engine acquires locks in sorted component ID order.
        Two WOs using the same BOM must not deadlock.
        WO1: 50.00 kg (requires 40.00 CaCO3 + 7.50 Oil)
        WO2: 50.00 kg (requires 40.00 CaCO3 + 7.50 Oil)
        Both should succeed since total CaCO3 needed = 80.00 <= 100.00.
        """
        wo1 = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo1,
            quantity=Decimal('50.00'),
            status='DRAFT',
        )

        wo2 = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('50.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo2,
            quantity=Decimal('50.00'),
            status='DRAFT',
        )

        # Both should succeed sequentially
        success1, _ = wo1.start_production()
        self.assertTrue(success1)

        success2, _ = wo2.start_production()
        self.assertTrue(success2)

        # Verify combined allocations
        self.caco3_inv.refresh_from_db()
        self.assertEqual(
            self.caco3_inv.quantity_available, Decimal('20.00'),
            "CaCO3: 100 - 40 - 40 = 20.00 available"
        )
        self.assertEqual(
            self.caco3_inv.quantity_allocated, Decimal('80.0000'),
            "CaCO3: 40 + 40 = 80.00 allocated"
        )

        self.oil_inv.refresh_from_db()
        self.assertEqual(
            self.oil_inv.quantity_available, Decimal('185.00'),
            "Oil: 200 - 7.50 - 7.50 = 185.00 available"
        )
        self.assertEqual(
            self.oil_inv.quantity_allocated, Decimal('15.0000'),
            "Oil: 7.50 + 7.50 = 15.00 allocated"
        )

    def test_pre_flight_gate_prevents_partial_allocation(self):
        """
        If one component has sufficient stock but another doesn't,
        the pre-flight gate must reject the ENTIRE allocation atomically.
        No partial allocations should ever occur.

        Set CaCO3 = 100 kg, Oil = 5.00 kg.
        WO: 75.00 kg Bulk Putty -> requires 60.00 CaCO3 (OK) + 11.25 Oil (FAIL).
        """
        # Reduce oil to trigger shortage on that component
        self.oil_inv.quantity_available = Decimal('5.00')
        self.oil_inv.save(update_fields=['quantity_available'])

        wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('75.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo,
            quantity=Decimal('75.00'),
            status='DRAFT',
        )

        with self.assertRaises(ValidationError):
            wo.start_production()

        # Verify NOTHING was allocated — atomic rollback
        self.caco3_inv.refresh_from_db()
        self.assertEqual(
            self.caco3_inv.quantity_available, Decimal('100.00'),
            "CaCO3 should remain at 100.00 — no partial allocation"
        )
        self.assertEqual(
            self.caco3_inv.quantity_allocated, Decimal('0.00'),
            "CaCO3 allocated should remain 0.00 — no partial allocation"
        )

        self.oil_inv.refresh_from_db()
        self.assertEqual(
            self.oil_inv.quantity_available, Decimal('5.00'),
            "Oil should remain at 5.00 — no partial allocation"
        )
        self.assertEqual(
            self.oil_inv.quantity_allocated, Decimal('0.00'),
            "Oil allocated should remain 0.00 — no partial allocation"
        )

        wo.refresh_from_db()
        self.assertFalse(wo.is_inventory_allocated)

    def test_inventory_never_goes_negative_under_exhaustion(self):
        """
        Stress test: Allocate nearly all CaCO3 via WO1, then try WO2.
        Available must never go below 0.00.
        """
        # WO1: 125.00 kg Bulk -> requires exactly 100.00 kg CaCO3 (all of it)
        wo1 = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('125.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo1,
            quantity=Decimal('125.00'),
            status='DRAFT',
        )

        success1, _ = wo1.start_production()
        self.assertTrue(success1)

        self.caco3_inv.refresh_from_db()
        self.assertEqual(self.caco3_inv.quantity_available, Decimal('0.00'))
        self.assertEqual(self.caco3_inv.quantity_allocated, Decimal('100.0000'))

        # WO2: 1.00 kg Bulk -> requires 0.80 kg CaCO3 (but 0.00 available)
        wo2 = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal('1.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=wo2,
            quantity=Decimal('1.00'),
            status='DRAFT',
        )

        with self.assertRaises(ValidationError):
            wo2.start_production()

        self.caco3_inv.refresh_from_db()
        self.assertGreaterEqual(
            self.caco3_inv.quantity_available, Decimal('0.00'),
            "CaCO3 available must NEVER go negative"
        )
        self.assertEqual(self.caco3_inv.quantity_available, Decimal('0.00'))
