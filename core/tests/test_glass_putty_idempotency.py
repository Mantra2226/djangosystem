from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from core.models import (
    Supplier, Product, BillOfMaterial, BOMItem, Inventory,
    WorkOrder, ProductionOrder, StockTransaction
)


class GlassPuttyManufacturingIdempotencyTestCase(TestCase):
    """
    Domain-specific automated test suite validating multi-ingredient BOM explosion,
    two-stage packaging, variance reconciliation, and idempotency for Glass Putty manufacturing.
    """

    def setUp(self):
        self.supplier = Supplier.objects.create(
            name="Glass Materials Ltd",
            contact_info="supplier@glassputty.com"
        )

        # 1. Raw Materials
        self.calcium_carb = Product.objects.create(
            name="Calcium Carbonate",
            product_type="RAW",
            category="Powders",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.linseed_oil = Product.objects.create(
            name="Raw Linseed Oil",
            product_type="RAW",
            category="Liquids",
            unit_of_measurement="kg",
            supplier=self.supplier
        )
        self.empty_tin = Product.objects.create(
            name="Empty 5kg Tin",
            product_type="RAW",
            category="Packaging",
            unit_of_measurement="pcs",
            supplier=self.supplier
        )

        # 2. Intermediate Good
        self.bulk_putty = Product.objects.create(
            name="Bulk Putty Base",
            product_type="INTERMEDIATE",
            category="Putty",
            unit_of_measurement="kg"
        )

        # 3. Finished Good
        self.finished_putty_tin = Product.objects.create(
            name="Glass Putty 5kg Tin",
            product_type="FINISHED",
            category="Retail Tins",
            unit_of_measurement="pcs",
            selling_price=Decimal("15.00")
        )

        # 4. Initial Stock Setup
        self.inv_calcium = Inventory.objects.create(
            product=self.calcium_carb,
            quantity_available=Decimal("1000.00"),
            quantity_allocated=Decimal("0.00"),
            location="Raw Material Bay"
        )
        self.inv_oil = Inventory.objects.create(
            product=self.linseed_oil,
            quantity_available=Decimal("200.00"),
            quantity_allocated=Decimal("0.00"),
            location="Liquid Storage"
        )
        self.inv_tin = Inventory.objects.create(
            product=self.empty_tin,
            quantity_available=Decimal("100.00"),
            quantity_allocated=Decimal("0.00"),
            location="Packaging Bay"
        )
        self.inv_bulk = Inventory.objects.create(
            product=self.bulk_putty,
            quantity_available=Decimal("0.00"),
            quantity_allocated=Decimal("0.00"),
            location="Mixing Floor"
        )
        self.inv_finished = Inventory.objects.create(
            product=self.finished_putty_tin,
            quantity_available=Decimal("0.00"),
            quantity_allocated=Decimal("0.00"),
            location="Finished Goods Warehouse"
        )

        # 5. BOMs:
        # Bulk Putty BOM (per 1.00 kg bulk): 0.80 kg Calcium Carbonate + 0.15 kg Linseed Oil (0.05 kg process loss)
        self.bulk_bom = BillOfMaterial.objects.create(
            product=self.bulk_putty,
            name="Bulk Putty Formula",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.calcium_carb,
            quantity_required=Decimal("0.8000")
        )
        BOMItem.objects.create(
            bom=self.bulk_bom,
            component=self.linseed_oil,
            quantity_required=Decimal("0.1500")
        )

        # Packaging BOM (per 1 Tin): 5.00 kg Bulk Putty Base + 1 pc Empty 5kg Tin
        self.pack_bom = BillOfMaterial.objects.create(
            product=self.finished_putty_tin,
            name="5kg Tin Packaging Spec",
            is_active=True
        )
        BOMItem.objects.create(
            bom=self.pack_bom,
            component=self.bulk_putty,
            quantity_required=Decimal("5.0000")
        )
        BOMItem.objects.create(
            bom=self.pack_bom,
            component=self.empty_tin,
            quantity_required=Decimal("1.0000")
        )

    def test_glass_putty_multi_ingredient_allocation_idempotent(self):
        """
        Stage 1 Bulk Mixing: Multi-Ingredient Allocation & Idempotency.
        - Create Stage 1 Bulk WO for 100.00 kg Bulk Putty.
        - Trigger start_production() and call process_inventory() 3 times.
        - Assert Calcium Carbonate allocated = 80.00 kg (Available: 920.00 kg).
        - Assert Linseed Oil allocated = 15.00 kg (Available: 185.00 kg).
        - Assert repeated executions cause zero additional deductions.
        """
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=bulk_wo,
            quantity=Decimal("100.00"),
            status='IN_PROGRESS'
        )

        # 1. Start production (triggers Phase 1 allocation)
        success, msg = bulk_wo.start_production()
        self.assertTrue(success)
        self.assertEqual(bulk_wo.status, 'IN_PROGRESS')

        # 2. Call process_inventory() 3 additional times back-to-back
        for _ in range(3):
            bulk_wo.process_inventory()

        # 3. Assert allocations and available balances
        self.inv_calcium.refresh_from_db()
        self.inv_oil.refresh_from_db()
        bulk_wo.refresh_from_db()

        self.assertTrue(bulk_wo.is_inventory_allocated)

        # Calcium Carbonate: 100 kg * 0.80 kg = 80.00 kg allocated (Available: 1000 - 80 = 920.00 kg)
        self.assertEqual(
            self.inv_calcium.quantity_allocated,
            Decimal("80.00"),
            "Calcium Carbonate allocated quantity must be exactly 80.00 kg and never duplicated"
        )
        self.assertEqual(
            self.inv_calcium.quantity_available,
            Decimal("920.00"),
            "Calcium Carbonate available quantity must be 920.00 kg after single allocation"
        )

        # Raw Linseed Oil: 100 kg * 0.15 kg = 15.00 kg allocated (Available: 200 - 15 = 185.00 kg)
        self.assertEqual(
            self.inv_oil.quantity_allocated,
            Decimal("15.00"),
            "Raw Linseed Oil allocated quantity must be exactly 15.00 kg and never duplicated"
        )
        self.assertEqual(
            self.inv_oil.quantity_available,
            Decimal("185.00"),
            "Raw Linseed Oil available quantity must be 185.00 kg after single allocation"
        )

        # Assert no consumption transactions occurred during allocation phase
        consumption_tx_count = StockTransaction.objects.filter(
            work_order=bulk_wo,
            transaction_type='PRODUCTION_CONSUMPTION'
        ).count()
        self.assertEqual(consumption_tx_count, 0, "No consumption transactions should be posted in Phase 1")

    def test_glass_putty_two_stage_shortage_resolution_idempotent(self):
        """
        Stage 2 Packaging Shortage Resolution & Spam Protection.
        - Create Stage 2 Packaging WO for 20 Tins (requires 20 * 5.00 = 100.00 kg Bulk Putty Base).
        - With 0.00 kg bulk in stock, trigger start_production() -> verifies transition to AWAITING_RESOLUTION.
        - Call resolve_bulk_shortage('TOP_UP_BULK') twice.
        - Assert only ONE parent Bulk WO is spawned for 100.00 kg bulk shortfall.
        - Assert the second call raises ValidationError and does not spawn a second parent WO.
        """
        pack_wo = WorkOrder.objects.create(
            product=self.finished_putty_tin,
            bill_of_material=self.pack_bom,
            quantity_produced=Decimal("20.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.finished_putty_tin,
            work_order=pack_wo,
            quantity=Decimal("20.00"),
            status='IN_PROGRESS'
        )

        # 1. Start packaging order with 0 bulk available -> routes to AWAITING_RESOLUTION
        success, msg = pack_wo.start_production()
        self.assertFalse(success)
        pack_wo.refresh_from_db()
        self.assertEqual(pack_wo.status, 'AWAITING_RESOLUTION')
        self.assertIn("Bulk shortage detected", msg)

        # 2. First call: TOP_UP_BULK resolution
        pack_wo.resolve_bulk_shortage('TOP_UP_BULK')
        pack_wo.refresh_from_db()

        self.assertEqual(pack_wo.status, 'ON_HOLD_SHORTAGE')
        self.assertIsNotNone(pack_wo.parent_work_order)

        parent_bulk_wo = pack_wo.parent_work_order
        self.assertEqual(parent_bulk_wo.product, self.bulk_putty)
        self.assertEqual(parent_bulk_wo.quantity_produced, Decimal("100.00")) # Exact shortfall for 20 tins
        self.assertEqual(parent_bulk_wo.status, 'IN_PROGRESS')
        self.assertTrue(parent_bulk_wo.is_inventory_allocated)

        # 3. Second call: Duplicate TOP_UP_BULK attempt on the same on-hold WorkOrder
        with self.assertRaises(ValidationError) as ctx:
            pack_wo.resolve_bulk_shortage('TOP_UP_BULK')

        self.assertIn("AWAITING_RESOLUTION", str(ctx.exception))

        # 4. Verify exactly ONE parent Bulk Work Order exists in total
        bulk_orders = WorkOrder.objects.filter(product=self.bulk_putty)
        self.assertEqual(bulk_orders.count(), 1, "Exactly one parent Bulk WorkOrder must be created")
        self.assertEqual(bulk_orders.first().pk, parent_bulk_wo.pk)

    def test_glass_putty_actual_variance_reconciliation_idempotent(self):
        """
        Stage 1 Bulk Mixing: Actual Variance Reconciliation & Finished Goods Posting Idempotency.
        - Run Stage 1 Bulk WO (100 kg target).
        - Log actuals: 78.00 kg Calcium Carbonate (under-consumed by 2 kg) and 16.00 kg Linseed Oil (over-consumed by 1 kg).
        - Transition to COMPLETED and invoke process_inventory() twice.
        - Assert unused 2.00 kg Calcium Carbonate allocation is released back to available stock (Available: 922.00 kg).
        - Assert over-consumed 1.00 kg Linseed Oil is deducted from available stock (Available: 184.00 kg).
        - Assert Bulk Putty Base stock increases by exactly +100.00 kg (1x, never 200.00 kg).
        """
        bulk_wo = WorkOrder.objects.create(
            product=self.bulk_putty,
            bill_of_material=self.bulk_bom,
            quantity_produced=Decimal("100.00"),
            production_start_date=timezone.now().date(),
            status='DRAFT'
        )
        ProductionOrder.objects.create(
            product=self.bulk_putty,
            work_order=bulk_wo,
            quantity=Decimal("100.00"),
            status='IN_PROGRESS'
        )

        # 1. Start production (allocates 80.00 kg Calcium Carbonate and 15.00 kg Linseed Oil)
        bulk_wo.start_production()

        # 2. Log actual consumption variance in Phase 2
        line_calcium = bulk_wo.material_lines.get(component=self.calcium_carb)
        line_oil = bulk_wo.material_lines.get(component=self.linseed_oil)

        line_calcium.quantity_actual = Decimal("78.00") # 2 kg under-consumption
        line_calcium.save()

        line_oil.quantity_actual = Decimal("16.00") # 1 kg over-consumption
        line_oil.save()

        bulk_wo.process_inventory()

        # 3. Mark instructions completed and transition to COMPLETED
        bulk_wo.instructions.update(status='COMPLETED')
        bulk_wo.recalculate_status()
        bulk_wo.status = 'COMPLETED'
        bulk_wo.save(update_fields=['status'])

        # First Phase 3 completion execution
        bulk_wo.process_inventory()

        # Second Phase 3 execution (duplicate call)
        bulk_wo.process_inventory()

        # 4. Verify Final Inventory Balances and Reconciliations
        self.inv_calcium.refresh_from_db()
        self.inv_oil.refresh_from_db()
        self.inv_bulk.refresh_from_db()
        bulk_wo.refresh_from_db()

        self.assertTrue(bulk_wo.is_inventory_updated)

        # Calcium Carbonate: Initial 1000.00 kg - 78.00 kg actual consumed = 922.00 kg Available, 0.00 kg Allocated
        self.assertEqual(
            self.inv_calcium.quantity_allocated,
            Decimal("0.00"),
            "All remaining Calcium Carbonate allocations must be returned to available stock upon completion"
        )
        self.assertEqual(
            self.inv_calcium.quantity_available,
            Decimal("922.00"),
            "Calcium Carbonate available balance must be 922.00 kg (1000 - 78 actual consumed)"
        )

        # Linseed Oil: Initial 200.00 kg - 16.00 kg actual consumed = 184.00 kg Available, 0.00 kg Allocated
        self.assertEqual(
            self.inv_oil.quantity_allocated,
            Decimal("0.00"),
            "Linseed oil allocated balance must be 0.00"
        )
        self.assertEqual(
            self.inv_oil.quantity_available,
            Decimal("184.00"),
            "Linseed Oil available balance must be 184.00 kg (200 - 16 actual consumed)"
        )

        # Bulk Putty Base (Intermediate Stage 1 Output): Initial 0.00 + 100.00 = 100.00 kg (1x)
        self.assertEqual(
            self.inv_bulk.quantity_available,
            Decimal("100.00"),
            "Bulk Putty Base output must increase by exactly 100.00 kg and never double on repeated saves"
        )

        # Verify output transactions count
        output_tx_count = StockTransaction.objects.filter(
            work_order=bulk_wo,
            transaction_type='PRODUCTION_OUTPUT'
        ).count()
        self.assertEqual(output_tx_count, 1, "Exactly one PRODUCTION_OUTPUT transaction must be recorded for Bulk Putty Base")
