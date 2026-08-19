"""
Test suite for Phase 3 stock reconciliation and unused allocation release.

Validates that:
- Remaining consumption deltas are deducted before releasing allocations.
- Residual allocated stock is correctly returned to quantity_available.
- Finished goods inventory is updated with the correct quantity.
- Inventory.quantity_allocated zeroes out on completion.
"""
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from core.models import (
    Product, Supplier, BillOfMaterial, BOMItem,
    Inventory, WorkOrder, WorkOrderMaterialLine,
    ProductionOrder, StockTransaction,
)


class Phase3ReconciliationTestCase(TestCase):
    """
    Scenario: A PRODUCTION work order allocates 50.00 kg of a raw material,
    but only 42.00 kg are actually consumed. On completion, Phase 3 must:
      1. Deduct the remaining 42.00 kg delta (if Phase 2 didn't run fully).
      2. Release the residual 8.00 kg back from quantity_allocated to quantity_available.
      3. Post finished goods output.
      4. Zero out quantity_allocated for the raw material.
    """

    def setUp(self):
        # --- Supplier ---
        self.supplier = Supplier.objects.create(
            name='Reconciliation Test Supplier',
            contact_info='N/A'
        )

        # --- Raw Material ---
        self.raw_material = Product.objects.create(
            name='Reconciliation Test Powder',
            product_type='RAW',
            category='Powder',
            unit_of_measurement='kg',
            supplier=self.supplier,
        )

        # --- Intermediate Product (output of this WO) ---
        self.intermediate_product = Product.objects.create(
            name='Reconciliation Test Intermediate',
            product_type='INTERMEDIATE',
            category='Intermediate',
            unit_of_measurement='kg',
        )

        # --- BOM: 1 kg intermediate requires 0.50 kg raw material ---
        self.bom = BillOfMaterial.objects.create(
            product=self.intermediate_product,
            is_active=True,
        )
        BOMItem.objects.create(
            bom=self.bom,
            component=self.raw_material,
            quantity_required=Decimal('0.50'),
        )

        # --- Seed inventory: 200.00 kg available, 0.00 allocated ---
        self.raw_inv = Inventory.objects.create(
            product=self.raw_material,
            quantity_available=Decimal('200.00'),
            quantity_allocated=Decimal('0.00'),
        )

        # --- Finished/intermediate goods inventory (start at zero) ---
        self.finished_inv = Inventory.objects.create(
            product=self.intermediate_product,
            quantity_available=Decimal('0.00'),
            quantity_allocated=Decimal('0.00'),
        )

    def test_phase3_releases_residual_allocation_on_under_consumption(self):
        """
        Allocate 50.00 kg (100 units * 0.50 BOM req), consume only 42.00 kg,
        complete WO, and verify the 8.00 kg residual is released back.
        """
        # 1. Create WorkOrder for 100 units of intermediate product
        wo = WorkOrder.objects.create(
            product=self.intermediate_product,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )

        # Create linked ProductionOrder (target_quantity source)
        po = ProductionOrder.objects.create(
            product=self.intermediate_product,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='DRAFT',
        )

        # 2. Start production -> Phase 1 allocates 50.00 kg (0.50 * 100)
        success, msg = wo.start_production()
        self.assertTrue(success)
        wo.refresh_from_db()
        self.assertEqual(wo.status, 'IN_PROGRESS')
        self.assertTrue(wo.is_inventory_allocated)

        # Verify Phase 1 allocation
        self.raw_inv.refresh_from_db()
        self.assertEqual(self.raw_inv.quantity_allocated, Decimal('50.0000'))
        self.assertEqual(self.raw_inv.quantity_available, Decimal('150.00'))

        # 3. Set actual consumption = 42.00 kg and run Phase 2
        mat_line = wo.material_lines.get(component=self.raw_material)
        mat_line.quantity_actual = Decimal('42.00')
        mat_line.save(update_fields=['quantity_actual'])
        wo.process_inventory()

        # Verify Phase 2 incremental deduction
        mat_line.refresh_from_db()
        self.assertEqual(mat_line.deducted_quantity, Decimal('42.00'))

        self.raw_inv.refresh_from_db()
        # 50 allocated - 42 deducted = 8.00 remaining in allocation
        self.assertEqual(self.raw_inv.quantity_allocated, Decimal('8.0000'))

        # 4. Complete the work order
        wo.status = 'COMPLETED'
        wo.actual_quantity_produced = Decimal('100.00')
        wo.production_end_date = timezone.now()
        super(WorkOrder, wo).save(update_fields=['status', 'actual_quantity_produced', 'production_end_date'])

        # 5. Run Phase 3 reconciliation
        wo.process_inventory()

        # 6. Assertions
        self.raw_inv.refresh_from_db()

        # quantity_allocated must be 0.00 after full reconciliation
        self.assertEqual(
            self.raw_inv.quantity_allocated,
            Decimal('0.0000'),
            f"Expected quantity_allocated=0.00, got {self.raw_inv.quantity_allocated}. "
            f"Residual allocation was not fully released."
        )

        # quantity_available should have received the +8.00 kg returned residual
        # Starting: 200.00 - 50.00 (Phase 1) = 150.00, then +8.00 (released) = 158.00
        self.assertEqual(
            self.raw_inv.quantity_available,
            Decimal('158.00'),
            f"Expected quantity_available=158.00, got {self.raw_inv.quantity_available}. "
            f"Residual 8.00 kg was not returned to available stock."
        )

        # Finished goods inventory should have increased by 100.00
        self.finished_inv.refresh_from_db()
        self.assertEqual(
            self.finished_inv.quantity_available,
            Decimal('100.00'),
            f"Expected finished goods stock=100.00, got {self.finished_inv.quantity_available}."
        )

        # is_inventory_updated flag must be True
        wo.refresh_from_db()
        self.assertTrue(wo.is_inventory_updated)

    def test_phase3_handles_zero_consumption(self):
        """
        Allocate 50.00 kg, consume 0.00 kg (operator didn't use any),
        complete WO -> all 50.00 kg released back, quantity_allocated=0.
        """
        wo = WorkOrder.objects.create(
            product=self.intermediate_product,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.intermediate_product,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='DRAFT',
        )

        success, _ = wo.start_production()
        self.assertTrue(success)

        self.raw_inv.refresh_from_db()
        self.assertEqual(self.raw_inv.quantity_allocated, Decimal('50.0000'))
        self.assertEqual(self.raw_inv.quantity_available, Decimal('150.00'))

        # Leave quantity_actual at 0.00 (no consumption)
        # Complete the WO
        wo.status = 'COMPLETED'
        wo.actual_quantity_produced = Decimal('100.00')
        wo.production_end_date = timezone.now()
        super(WorkOrder, wo).save(update_fields=['status', 'actual_quantity_produced', 'production_end_date'])

        wo.process_inventory()

        self.raw_inv.refresh_from_db()
        self.assertEqual(
            self.raw_inv.quantity_allocated,
            Decimal('0.0000'),
            "All allocated stock should be released when actual consumption is zero."
        )
        self.assertEqual(
            self.raw_inv.quantity_available,
            Decimal('200.00'),
            "Full allocation should be returned to available stock."
        )

        wo.refresh_from_db()
        self.assertTrue(wo.is_inventory_updated)

    def test_phase3_skips_when_actual_equals_allocated(self):
        """
        Allocate 50.00 kg, consume exactly 50.00 kg.
        Phase 3 should NOT release anything (residual = 0).
        """
        wo = WorkOrder.objects.create(
            product=self.intermediate_product,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.intermediate_product,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='DRAFT',
        )

        success, _ = wo.start_production()
        self.assertTrue(success)

        # Consume exactly 50.00 kg
        mat_line = wo.material_lines.get(component=self.raw_material)
        mat_line.quantity_actual = Decimal('50.00')
        mat_line.save(update_fields=['quantity_actual'])
        wo.process_inventory()

        mat_line.refresh_from_db()
        self.assertEqual(mat_line.deducted_quantity, Decimal('50.00'))

        self.raw_inv.refresh_from_db()
        self.assertEqual(self.raw_inv.quantity_allocated, Decimal('0.0000'))

        # Complete WO
        wo.status = 'COMPLETED'
        wo.actual_quantity_produced = Decimal('100.00')
        wo.production_end_date = timezone.now()
        super(WorkOrder, wo).save(update_fields=['status', 'actual_quantity_produced', 'production_end_date'])

        wo.process_inventory()

        self.raw_inv.refresh_from_db()
        self.assertEqual(
            self.raw_inv.quantity_allocated,
            Decimal('0.0000'),
        )
        # Available = 200 - 50 (Phase 1 alloc) = 150; no release since fully consumed
        self.assertEqual(
            self.raw_inv.quantity_available,
            Decimal('150.00'),
        )

        wo.refresh_from_db()
        self.assertTrue(wo.is_inventory_updated)

    def test_phase3_processes_remaining_delta_before_release(self):
        """
        Partially consume via Phase 2 (25.00 kg), then increase actual to 42.00 kg
        WITHOUT running Phase 2 again, then complete WO.
        Phase 3 must deduct the remaining 17.00 kg delta first, THEN release residual.
        """
        wo = WorkOrder.objects.create(
            product=self.intermediate_product,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.intermediate_product,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='DRAFT',
        )

        success, _ = wo.start_production()
        self.assertTrue(success)

        # Phase 2: Deduct 25.00 kg
        mat_line = wo.material_lines.get(component=self.raw_material)
        mat_line.quantity_actual = Decimal('25.00')
        mat_line.save(update_fields=['quantity_actual'])
        wo.process_inventory()

        mat_line.refresh_from_db()
        self.assertEqual(mat_line.deducted_quantity, Decimal('25.00'))

        self.raw_inv.refresh_from_db()
        # Allocated: 50 - 25 = 25, Available: 150 (unchanged by Phase 2 since it deducts from allocated)
        self.assertEqual(self.raw_inv.quantity_allocated, Decimal('25.0000'))

        # Now increase actual to 42.00 but DO NOT run process_inventory for Phase 2
        mat_line.quantity_actual = Decimal('42.00')
        mat_line.save(update_fields=['quantity_actual'])

        # Complete WO directly — Phase 3 must handle the 17.00 kg remaining delta
        wo.status = 'COMPLETED'
        wo.actual_quantity_produced = Decimal('100.00')
        wo.production_end_date = timezone.now()
        super(WorkOrder, wo).save(update_fields=['status', 'actual_quantity_produced', 'production_end_date'])

        wo.process_inventory()

        mat_line.refresh_from_db()
        self.assertEqual(mat_line.deducted_quantity, Decimal('42.00'))

        self.raw_inv.refresh_from_db()
        # After Phase 3:
        # Step 3a deducts remaining delta 17.00 from allocated (25 - 17 = 8)
        # Step 3b releases residual = max(0, 50 - 42) = 8 from allocated (8 - 8 = 0) to available (150 + 8 = 158)
        self.assertEqual(
            self.raw_inv.quantity_allocated,
            Decimal('0.0000'),
            f"Expected quantity_allocated=0.00, got {self.raw_inv.quantity_allocated}."
        )
        self.assertEqual(
            self.raw_inv.quantity_available,
            Decimal('158.00'),
            f"Expected quantity_available=158.00, got {self.raw_inv.quantity_available}. "
            f"Phase 3 should have deducted remaining delta and released residual."
        )

        wo.refresh_from_db()
        self.assertTrue(wo.is_inventory_updated)

    def test_phase3_idempotent_on_double_call(self):
        """
        Calling process_inventory() twice after COMPLETED must not double-post
        finished goods or double-release allocations.
        """
        wo = WorkOrder.objects.create(
            product=self.intermediate_product,
            bill_of_material=self.bom,
            quantity_produced=Decimal('100.00'),
            production_start_date=timezone.now().date(),
            status='DRAFT',
        )
        ProductionOrder.objects.create(
            product=self.intermediate_product,
            work_order=wo,
            quantity=Decimal('100.00'),
            status='DRAFT',
        )

        success, _ = wo.start_production()
        self.assertTrue(success)

        mat_line = wo.material_lines.get(component=self.raw_material)
        mat_line.quantity_actual = Decimal('42.00')
        mat_line.save(update_fields=['quantity_actual'])
        wo.process_inventory()

        wo.status = 'COMPLETED'
        wo.actual_quantity_produced = Decimal('100.00')
        wo.production_end_date = timezone.now()
        super(WorkOrder, wo).save(update_fields=['status', 'actual_quantity_produced', 'production_end_date'])

        # First Phase 3 call
        wo.process_inventory()

        # Snapshot state after first call
        self.raw_inv.refresh_from_db()
        alloc_after_first = self.raw_inv.quantity_allocated
        avail_after_first = self.raw_inv.quantity_available
        self.finished_inv.refresh_from_db()
        finished_after_first = self.finished_inv.quantity_available

        # Second Phase 3 call (should be no-op due to is_inventory_updated=True)
        wo.process_inventory()

        self.raw_inv.refresh_from_db()
        self.assertEqual(self.raw_inv.quantity_allocated, alloc_after_first)
        self.assertEqual(self.raw_inv.quantity_available, avail_after_first)
        self.finished_inv.refresh_from_db()
        self.assertEqual(self.finished_inv.quantity_available, finished_after_first)
